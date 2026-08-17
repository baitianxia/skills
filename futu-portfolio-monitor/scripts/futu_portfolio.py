#!/usr/bin/env python3
"""Read-only Futu OpenD portfolio analysis and monitoring CLI."""

from __future__ import print_function

import argparse
import contextlib
import importlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
DISCLAIMER = "Descriptive monitoring only; not investment advice or a trading instruction."
MISSING_TEXT = {"", "--", "N/A", "NA", "NAN", "NONE", "NULL"}


class PortfolioError(RuntimeError):
    """Expected configuration, input, or API failure."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_builtin(value):
    """Convert pandas/numpy/Futu values into JSON-compatible values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_builtin(item) for item in value]
    if hasattr(value, "item"):
        try:
            return to_builtin(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def number(value):
    value = to_builtin(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value).strip()
    if text.upper() in MISSING_TEXT:
        return None
    try:
        result = float(text.replace(",", ""))
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def boolean(value):
    value = to_builtin(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def text_value(value, default=""):
    value = to_builtin(value)
    if value is None:
        return default
    text = str(value).strip()
    return default if text.upper() in MISSING_TEXT else text


def enum_text(value, default=""):
    text = text_value(value, default)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


def pick(record, *names):
    for name in names:
        if name in record:
            return record[name]
    return None


def frame_records(data):
    if hasattr(data, "to_dict"):
        try:
            records = data.to_dict(orient="records")
        except TypeError:
            records = data.to_dict("records")
    elif isinstance(data, list):
        records = data
    else:
        raise PortfolioError("Futu returned an unsupported table type: %s" % type(data).__name__)
    return [to_builtin(record) for record in records]


def snapshot_map(snapshots):
    if snapshots is None:
        return {}
    if isinstance(snapshots, dict):
        if "code" in snapshots:
            snapshots = [snapshots]
        else:
            snapshots = list(snapshots.values())
    result = {}
    for raw in snapshots:
        record = to_builtin(raw)
        if not isinstance(record, dict):
            continue
        code = text_value(pick(record, "code", "symbol"))
        if code:
            result[code] = record
    return result


def field_is_valid(record, flag_name, value):
    if flag_name not in record:
        return value is not None
    flag = boolean(record.get(flag_name))
    return value is not None if flag is None else flag and value is not None


def normalize_position(raw, quote=None):
    record = to_builtin(raw)
    if not isinstance(record, dict):
        raise PortfolioError("Each position must be a JSON object")
    quote = to_builtin(quote or {})

    code = text_value(pick(record, "code", "symbol"))
    if not code:
        raise PortfolioError("A position is missing its code")

    market_value = number(pick(record, "market_value", "market_val"))
    quantity = number(pick(record, "quantity", "qty"))
    can_sell = number(pick(record, "can_sell_quantity", "can_sell_qty"))

    diluted_cost = number(pick(record, "diluted_cost"))
    average_cost = number(pick(record, "average_cost"))
    legacy_cost = number(pick(record, "cost_price"))
    cost_price = diluted_cost if diluted_cost is not None else average_cost
    if cost_price is None:
        cost_price = legacy_cost
    cost_valid = field_is_valid(record, "cost_price_valid", cost_price)

    pl_ratio = number(pick(record, "pl_ratio_pct", "pl_ratio"))
    if not field_is_valid(record, "pl_ratio_valid", pl_ratio):
        pl_ratio = None
    if pl_ratio is None:
        pl_ratio = number(pick(record, "pl_ratio_avg_cost"))

    pl_value = number(pick(record, "pl_value", "pl_val", "unrealized_pl"))
    if not field_is_valid(record, "pl_val_valid", pl_value):
        pl_value = None

    position_price = number(pick(record, "last_price", "nominal_price", "price"))
    quote_price = number(pick(quote, "last_price", "price"))
    last_price = quote_price if quote_price is not None else position_price
    previous_close = number(pick(quote, "prev_close_price", "previous_close"))

    daily_change_pct = number(pick(record, "daily_change_pct"))
    if daily_change_pct is None:
        daily_change_pct = number(pick(quote, "change_rate"))
    if quote_price is not None and previous_close not in (None, 0):
        daily_change_pct = (quote_price / previous_close - 1.0) * 100.0

    currency = enum_text(pick(record, "currency"), "UNKNOWN")
    market = enum_text(pick(record, "position_market", "market"))
    if not market and "." in code:
        market = code.split(".", 1)[0].upper()

    return {
        "code": code,
        "name": text_value(pick(record, "name", "stock_name"), text_value(quote.get("name"), code)),
        "market": market or "UNKNOWN",
        "currency": currency or "UNKNOWN",
        "side": enum_text(pick(record, "side", "position_side"), "UNKNOWN"),
        "quantity": quantity,
        "can_sell_quantity": can_sell,
        "cost_price": cost_price,
        "cost_price_valid": bool(cost_valid),
        "last_price": last_price,
        "price_source": "market_snapshot" if quote_price is not None else "position_cache",
        "market_value": market_value,
        "pl_ratio_pct": pl_ratio,
        "pl_value": pl_value,
        "today_pl_value": number(pick(record, "today_pl_value", "today_pl_val")),
        "daily_change_pct": daily_change_pct,
        "quote_time": text_value(pick(quote, "update_time", "timestamp")),
        "position_id": text_value(pick(record, "position_id")),
        "weight_pct": None,
    }


def thresholds_from_args(args):
    return {
        "concentration_pct": float(args.concentration_pct),
        "loss_pct": float(args.loss_pct),
        "daily_move_pct": float(args.daily_move_pct),
    }


def validate_thresholds(thresholds):
    concentration = thresholds["concentration_pct"]
    daily_move = thresholds["daily_move_pct"]
    if not 0 < concentration <= 100:
        raise PortfolioError("--concentration-pct must be greater than 0 and at most 100")
    if daily_move < 0:
        raise PortfolioError("--daily-move-pct must be non-negative")


def analyze_positions(positions, snapshots=None, thresholds=None, source="offline", warnings=None):
    thresholds = thresholds or {
        "concentration_pct": 25.0,
        "loss_pct": -10.0,
        "daily_move_pct": 3.0,
    }
    validate_thresholds(thresholds)
    quotes = snapshot_map(snapshots)
    normalized = [normalize_position(item, quotes.get(text_value(pick(item, "code", "symbol")))) for item in positions]

    buckets = {}
    for position in normalized:
        bucket = buckets.setdefault(
            position["currency"],
            {
                "currency": position["currency"],
                "position_count": 0,
                "net_market_value": 0.0,
                "gross_market_value": 0.0,
                "pl_value": 0.0,
                "pl_value_complete": True,
                "today_pl_value": 0.0,
                "today_pl_value_complete": True,
            },
        )
        bucket["position_count"] += 1
        market_value = position["market_value"]
        if market_value is not None:
            bucket["net_market_value"] += market_value
            bucket["gross_market_value"] += abs(market_value)
        if position["pl_value"] is None:
            bucket["pl_value_complete"] = False
        else:
            bucket["pl_value"] += position["pl_value"]
        if position["today_pl_value"] is None:
            bucket["today_pl_value_complete"] = False
        else:
            bucket["today_pl_value"] += position["today_pl_value"]

    for position in normalized:
        market_value = position["market_value"]
        gross = buckets[position["currency"]]["gross_market_value"]
        if market_value is not None and gross > 0:
            position["weight_pct"] = abs(market_value) / gross * 100.0

    alerts = []
    for position in normalized:
        weight = position["weight_pct"]
        if weight is not None and weight >= thresholds["concentration_pct"]:
            alerts.append(
                {
                    "severity": "warning",
                    "type": "concentration",
                    "code": position["code"],
                    "message": "%s is %.2f%% of gross %s exposure" % (position["code"], weight, position["currency"]),
                    "value": weight,
                    "threshold": thresholds["concentration_pct"],
                }
            )
        pl_ratio = position["pl_ratio_pct"]
        if pl_ratio is not None and pl_ratio <= thresholds["loss_pct"]:
            alerts.append(
                {
                    "severity": "warning",
                    "type": "loss_threshold",
                    "code": position["code"],
                    "message": "%s P/L is %.2f%%" % (position["code"], pl_ratio),
                    "value": pl_ratio,
                    "threshold": thresholds["loss_pct"],
                }
            )
        daily_change = position["daily_change_pct"]
        if daily_change is not None and abs(daily_change) >= thresholds["daily_move_pct"]:
            alerts.append(
                {
                    "severity": "warning",
                    "type": "daily_move",
                    "code": position["code"],
                    "message": "%s daily move is %+.2f%%" % (position["code"], daily_change),
                    "value": daily_change,
                    "threshold": thresholds["daily_move_pct"],
                }
            )
        if position["market_value"] is None:
            alerts.append(
                {
                    "severity": "info",
                    "type": "missing_market_value",
                    "code": position["code"],
                    "message": "%s has no usable market value" % position["code"],
                }
            )
        if not position["cost_price_valid"]:
            alerts.append(
                {
                    "severity": "info",
                    "type": "invalid_cost",
                    "code": position["code"],
                    "message": "%s has no valid cost price" % position["code"],
                }
            )

    normalized.sort(key=lambda item: (item["currency"], -(abs(item["market_value"]) if item["market_value"] is not None else -1), item["code"]))
    bucket_list = [buckets[key] for key in sorted(buckets)]
    for bucket in bucket_list:
        if not bucket["pl_value_complete"]:
            bucket["pl_value"] = None
        if not bucket["today_pl_value_complete"]:
            bucket["today_pl_value"] = None

    report_warnings = list(warnings or [])
    currency_warning = "Multiple currencies detected; totals and weights are calculated per currency only."
    if len(bucket_list) > 1 and currency_warning not in report_warnings:
        report_warnings.append(currency_warning)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source": source,
        "thresholds": thresholds,
        "currency_buckets": bucket_list,
        "positions": normalized,
        "alerts": sorted(alerts, key=lambda item: (item.get("code", ""), item["type"])),
        "warnings": report_warnings,
        "disclaimer": DISCLAIMER,
    }


def position_key(position):
    return (text_value(position.get("code")), enum_text(position.get("side"), "UNKNOWN"))


def compare_reports(before, after, price_change_pct=2.0, loss_pct=-10.0):
    if price_change_pct < 0:
        raise PortfolioError("--price-change-pct must be non-negative")
    old = {position_key(item): item for item in before.get("positions", [])}
    new = {position_key(item): item for item in after.get("positions", [])}
    changes = []

    for key in sorted(set(old) | set(new)):
        previous = old.get(key)
        current = new.get(key)
        code = key[0]
        if previous is None:
            changes.append({"severity": "info", "type": "new_position", "code": code, "message": "%s is a new position" % code})
            continue
        if current is None:
            changes.append({"severity": "info", "type": "closed_position", "code": code, "message": "%s is no longer held" % code})
            continue

        old_quantity = number(previous.get("quantity"))
        new_quantity = number(current.get("quantity"))
        if old_quantity is not None and new_quantity is not None and not math.isclose(old_quantity, new_quantity, rel_tol=1e-12, abs_tol=1e-12):
            changes.append(
                {
                    "severity": "warning",
                    "type": "quantity_change",
                    "code": code,
                    "message": "%s quantity changed from %s to %s" % (code, compact_number(old_quantity), compact_number(new_quantity)),
                    "before": old_quantity,
                    "after": new_quantity,
                }
            )

        old_price = number(previous.get("last_price"))
        new_price = number(current.get("last_price"))
        if old_price not in (None, 0) and new_price is not None:
            move = (new_price / old_price - 1.0) * 100.0
            if abs(move) >= price_change_pct:
                changes.append(
                    {
                        "severity": "warning",
                        "type": "sample_price_move",
                        "code": code,
                        "message": "%s moved %+.2f%% between samples" % (code, move),
                        "before": old_price,
                        "after": new_price,
                        "value": move,
                        "threshold": price_change_pct,
                    }
                )

        old_pl = number(previous.get("pl_ratio_pct"))
        new_pl = number(current.get("pl_ratio_pct"))
        if new_pl is not None and new_pl <= loss_pct and (old_pl is None or old_pl > loss_pct):
            changes.append(
                {
                    "severity": "warning",
                    "type": "loss_threshold_crossed",
                    "code": code,
                    "message": "%s crossed the P/L threshold at %.2f%%" % (code, new_pl),
                    "value": new_pl,
                    "threshold": loss_pct,
                }
            )

    return changes


def compact_number(value):
    value = number(value)
    if value is None:
        return "N/A"
    if value.is_integer():
        return str(int(value))
    return ("%.6f" % value).rstrip("0").rstrip(".")


def display_number(value, digits=2, signed=False):
    value = number(value)
    if value is None:
        return "—"
    specification = ("+" if signed else "") + ",." + str(digits) + "f"
    return format(value, specification)


def escape_markdown(value):
    return text_value(value, "—").replace("|", "\\|").replace("\n", " ")


def render_report(report):
    lines = ["# 富途持仓分析", "", "生成时间：`%s`" % report["generated_at"], ""]
    if report.get("warnings"):
        lines.extend(["## 数据提示", ""])
        lines.extend(["- %s" % escape_markdown(item) for item in report["warnings"]])
        lines.append("")

    lines.extend(
        [
            "## 分币种汇总",
            "",
            "| 币种 | 持仓数 | 净市值 | 总敞口 | 浮动盈亏 | 今日盈亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if report["currency_buckets"]:
        for item in report["currency_buckets"]:
            lines.append(
                "| %s | %d | %s | %s | %s | %s |"
                % (
                    escape_markdown(item["currency"]),
                    item["position_count"],
                    display_number(item["net_market_value"]),
                    display_number(item["gross_market_value"]),
                    display_number(item["pl_value"], signed=True),
                    display_number(item["today_pl_value"], signed=True),
                )
            )
    else:
        lines.append("| — | 0 | — | — | — | — |")

    lines.extend(
        [
            "",
            "## 持仓",
            "",
            "| 代码 | 名称 | 币种 | 方向 | 数量 | 最新价 | 市值 | 盈亏% | 日变动% | 币种内权重 |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if report["positions"]:
        for item in report["positions"]:
            lines.append(
                "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    escape_markdown(item["code"]),
                    escape_markdown(item["name"]),
                    escape_markdown(item["currency"]),
                    escape_markdown(item["side"]),
                    display_number(item["quantity"], 3),
                    display_number(item["last_price"], 3),
                    display_number(item["market_value"]),
                    display_number(item["pl_ratio_pct"], 2, signed=True),
                    display_number(item["daily_change_pct"], 2, signed=True),
                    display_number(item["weight_pct"], 2),
                )
            )
    else:
        lines.append("| — | 当前查询未返回持仓 | — | — | — | — | — | — | — | — |")

    lines.extend(["", "## 告警", ""])
    if report["alerts"]:
        lines.extend(["- [%s] %s" % (item["type"], escape_markdown(item["message"])) for item in report["alerts"]])
    else:
        lines.append("- 当前阈值下无告警。")
    lines.extend(["", "说明：%s" % DISCLAIMER])
    return "\n".join(lines)


def render_changes(changes):
    lines = ["## 与上一样本的变化", ""]
    if changes:
        lines.extend(["- [%s] %s" % (item["type"], escape_markdown(item["message"])) for item in changes])
    else:
        lines.append("- 未检测到达到阈值的变化。")
    return "\n".join(lines)


def mask_suffix(value):
    value = text_value(value)
    return "***%s" % value[-4:] if value else ""


def normalize_account(record):
    record = to_builtin(record)
    auth = pick(record, "trdmarket_auth", "trd_market_auth") or []
    if not isinstance(auth, list):
        auth = [auth]
    return {
        "acc_id": text_value(pick(record, "acc_id")),
        "trd_env": enum_text(pick(record, "trd_env")),
        "acc_type": enum_text(pick(record, "acc_type")),
        "security_firm": enum_text(pick(record, "security_firm")),
        "sim_acc_type": enum_text(pick(record, "sim_acc_type")),
        "authorized_markets": [enum_text(item) for item in auth],
        "account_status": enum_text(pick(record, "acc_status")),
        "universal_card_suffix": mask_suffix(pick(record, "uni_card_num")),
        "card_suffix": mask_suffix(pick(record, "card_num")),
    }


def render_accounts(accounts):
    lines = [
        "# 富途账户列表",
        "",
        "| acc_id | 环境 | 类型 | 券商 | 市场权限 | 卡号后四位 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if accounts:
        for item in accounts:
            suffix = item["universal_card_suffix"] or item["card_suffix"] or "—"
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    escape_markdown(item["acc_id"]),
                    escape_markdown(item["trd_env"]),
                    escape_markdown(item["acc_type"]),
                    escape_markdown(item["security_firm"]),
                    escape_markdown(", ".join(item["authorized_markets"])),
                    escape_markdown(suffix),
                )
            )
    else:
        lines.append("| — | — | — | — | — | 未返回账户 |")
    return "\n".join(lines)


class FutuProvider(object):
    """Thin adapter over the official SDK's read-only calls."""

    def __init__(self, args):
        self.args = args
        self._module = None

    def futu(self):
        if self._module is None:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    self._module = importlib.import_module("futu")
            except ImportError as exc:
                raise PortfolioError(
                    "Python module 'futu' is not installed. Install the official 'futu-api' package in this interpreter."
                ) from exc
            except PermissionError as exc:
                raise PortfolioError(
                    "The Futu SDK could not create its user log directory. Run the live command with filesystem permission or in a local terminal."
                ) from exc
        return self._module

    def enum(self, enum_name, member):
        module = self.futu()
        enum_class = getattr(module, enum_name, None)
        if enum_class is None or not hasattr(enum_class, member):
            raise PortfolioError("Unsupported Futu %s value: %s" % (enum_name, member))
        return getattr(enum_class, member)

    def encryption(self):
        setting = self.args.encrypt.lower()
        if setting not in {"auto", "true", "false"}:
            raise PortfolioError("--encrypt must be auto, true, or false")
        if setting == "auto":
            return None
        return setting == "true"

    def trade_context(self):
        module = self.futu()
        if not 1 <= self.args.port <= 65535:
            raise PortfolioError("--port must be between 1 and 65535")
        if self.args.account_type == "crypto" and self.args.security_firm not in {
            "FUTUSECURITIES",
            "FUTUINC",
            "FUTUSG",
        }:
            raise PortfolioError(
                "Crypto contexts require --security-firm FUTUSECURITIES, FUTUINC, or FUTUSG"
            )
        common = {
            "host": self.args.host,
            "port": self.args.port,
            "is_encrypt": self.encryption(),
            "security_firm": self.enum("SecurityFirm", self.args.security_firm),
        }
        with contextlib.redirect_stdout(sys.stderr):
            if self.args.account_type == "security":
                return module.OpenSecTradeContext(
                    filter_trdmarket=self.enum("TrdMarket", self.args.market), **common
                )
            if self.args.account_type == "future":
                return module.OpenFutureTradeContext(**common)
            if self.args.account_type == "crypto":
                constructor = getattr(module, "OpenCryptoTradeContext", None)
                if constructor is None:
                    raise PortfolioError("The installed Futu SDK does not support crypto trade contexts")
                return constructor(**common)
        raise PortfolioError("Unsupported account type: %s" % self.args.account_type)

    def check_result(self, operation, ret, data):
        if ret != self.futu().RET_OK:
            raise PortfolioError("Futu %s failed: %s" % (operation, text_value(data, "unknown error")))
        return frame_records(data)

    def get_accounts(self):
        context = self.trade_context()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                ret, data = context.get_acc_list()
            return self.check_result("get_acc_list", ret, data)
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                context.close()

    def get_positions(self):
        if not self.args.account_id:
            raise PortfolioError("An explicit --account-id (or FUTU_ACCOUNT_ID) is required")
        try:
            account_id = int(self.args.account_id)
        except ValueError as exc:
            raise PortfolioError("--account-id must contain digits only") from exc
        context = self.trade_context()
        try:
            params = {
                "trd_env": self.enum("TrdEnv", self.args.environment.upper()),
                "acc_id": account_id,
                "refresh_cache": bool(self.args.refresh_cache),
                "currency": self.enum("Currency", self.args.currency),
                "show_option_strategy_view": bool(self.args.option_strategy_view),
            }
            with contextlib.redirect_stdout(sys.stderr):
                ret, data = context.position_list_query(**params)
            return self.check_result("position_list_query", ret, data)
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                context.close()

    def get_snapshots(self, codes):
        if not codes:
            return [], []
        if not 1 <= self.args.port <= 65535:
            raise PortfolioError("--port must be between 1 and 65535")
        module = self.futu()
        size = self.args.snapshot_batch_size
        if not 1 <= size <= 400:
            raise PortfolioError("--snapshot-batch-size must be between 1 and 400")
        kwargs = {
            "host": self.args.host,
            "port": self.args.port,
            "is_encrypt": self.encryption(),
        }
        if self.args.account_type == "crypto":
            kwargs["security_firm"] = self.enum("SecurityFirm", self.args.security_firm)
        with contextlib.redirect_stdout(sys.stderr):
            context = module.OpenQuoteContext(**kwargs)
        records = []
        warnings = []
        try:
            for start in range(0, len(codes), size):
                batch = codes[start : start + size]
                with contextlib.redirect_stdout(sys.stderr):
                    ret, data = context.get_market_snapshot(batch)
                if ret == module.RET_OK:
                    records.extend(frame_records(data))
                else:
                    warnings.append("Futu get_market_snapshot failed for %s: %s" % (", ".join(batch), text_value(data)))
                if start + size < len(codes):
                    time.sleep(0.55)
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                context.close()
        return records, warnings


def read_json(path_text):
    try:
        if path_text == "-":
            payload = json.load(sys.stdin)
        else:
            with Path(path_text).expanduser().open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise PortfolioError("Could not read JSON from %s: %s" % (path_text, exc)) from exc
    return to_builtin(payload)


def payload_parts(payload):
    if isinstance(payload, list):
        return payload, [], []
    if not isinstance(payload, dict):
        raise PortfolioError("Portfolio JSON must be a position list or an object")
    if "report" in payload and isinstance(payload["report"], dict):
        payload = payload["report"]
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise PortfolioError("Portfolio JSON object must contain a positions list")
    snapshots = payload.get("snapshots") or []
    warnings = payload.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return positions, snapshots, warnings


def report_from_payload(payload, thresholds, source):
    positions, snapshots, warnings = payload_parts(payload)
    return analyze_positions(positions, snapshots, thresholds, source=source, warnings=warnings)


def fetch_live_report(args):
    provider = FutuProvider(args)
    positions = provider.get_positions()
    snapshots = []
    warnings = []
    if not args.no_quotes:
        codes = sorted({text_value(item.get("code")) for item in positions if text_value(item.get("code"))})
        try:
            snapshots, warnings = provider.get_snapshots(codes)
        except PortfolioError as exc:
            warnings.append(str(exc))
    return analyze_positions(
        positions,
        snapshots,
        thresholds_from_args(args),
        source="futu-opend",
        warnings=warnings,
    )


def atomic_write_json(path_text, payload):
    target = Path(path_text).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(to_builtin(payload), handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def emit_json(payload, compact=False):
    if compact:
        print(json.dumps(to_builtin(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)
    else:
        print(json.dumps(to_builtin(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def add_connection_arguments(parser):
    parser.add_argument("--host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"), help="OpenD host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111")), help="OpenD port")
    parser.add_argument("--market", default=os.environ.get("FUTU_TRD_MARKET", "NONE").upper(), help="Futu TrdMarket member")
    parser.add_argument(
        "--security-firm",
        default=os.environ.get("FUTU_SECURITY_FIRM", "NONE").upper(),
        help="Futu SecurityFirm member",
    )
    parser.add_argument(
        "--account-type",
        choices=("security", "future", "crypto"),
        default=os.environ.get("FUTU_ACCOUNT_TYPE", "security").lower(),
        help="Trade context type",
    )
    parser.add_argument(
        "--encrypt",
        choices=("auto", "true", "false"),
        default=os.environ.get("FUTU_OPEND_ENCRYPT", "auto").lower(),
        help="OpenD protocol encryption override",
    )


def add_query_arguments(parser):
    add_connection_arguments(parser)
    parser.add_argument("--account-id", default=os.environ.get("FUTU_ACCOUNT_ID"), help="Explicit Futu trading account ID")
    parser.add_argument(
        "--environment",
        choices=("real", "simulate"),
        default=os.environ.get("FUTU_TRD_ENV", "real").lower(),
        help="Trading environment to query",
    )
    parser.add_argument("--currency", default=os.environ.get("FUTU_CURRENCY", "USD").upper(), help="Crypto position currency")
    parser.add_argument("--refresh-cache", action="store_true", help="Force a server refresh of positions")
    parser.add_argument("--option-strategy-view", action="store_true", help="Return option strategy view positions")
    parser.add_argument("--no-quotes", action="store_true", help="Skip market snapshot enrichment")
    parser.add_argument("--snapshot-batch-size", type=int, default=20, help="Symbols per snapshot call (1-400)")


def add_threshold_arguments(parser):
    parser.add_argument("--concentration-pct", type=float, default=25.0, help="Per-currency concentration alert threshold")
    parser.add_argument("--loss-pct", type=float, default=-10.0, help="P/L percentage alert threshold")
    parser.add_argument("--daily-move-pct", type=float, default=3.0, help="Absolute daily price move alert threshold")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    accounts = subparsers.add_parser("accounts", help="List accounts with card numbers masked")
    add_connection_arguments(accounts)
    accounts.add_argument("--format", choices=("markdown", "json"), default="markdown")

    analyze = subparsers.add_parser("analyze", help="Analyze live holdings or an offline JSON export")
    add_query_arguments(analyze)
    add_threshold_arguments(analyze)
    analyze.add_argument("--input", help="Offline JSON path, or - for stdin; bypasses OpenD")
    analyze.add_argument("--format", choices=("markdown", "json"), default="markdown")

    compare = subparsers.add_parser("compare", help="Compare two saved reports or exports")
    add_threshold_arguments(compare)
    compare.add_argument("--before", required=True, help="Earlier JSON report/export")
    compare.add_argument("--after", required=True, help="Later JSON report/export")
    compare.add_argument("--price-change-pct", type=float, default=2.0, help="Between-sample price alert threshold")
    compare.add_argument("--format", choices=("markdown", "json"), default="markdown")

    watch = subparsers.add_parser("watch", help="Poll OpenD and compare portfolio samples")
    add_query_arguments(watch)
    add_threshold_arguments(watch)
    watch.add_argument("--price-change-pct", type=float, default=2.0, help="Between-sample price alert threshold")
    watch.add_argument("--interval", type=float, default=float(os.environ.get("FUTU_WATCH_INTERVAL", "60")), help="Seconds between samples")
    watch.add_argument("--max-iterations", type=int, default=0, help="Stop after N samples; 0 watches until interrupted")
    watch.add_argument("--state-file", help="Optional JSON baseline persisted with user-only permissions")
    watch.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def command_accounts(args):
    accounts = [normalize_account(item) for item in FutuProvider(args).get_accounts()]
    if args.format == "json":
        emit_json({"generated_at": utc_now(), "accounts": accounts})
    else:
        print(render_accounts(accounts))
    return 0


def command_analyze(args):
    thresholds = thresholds_from_args(args)
    if args.input:
        report = report_from_payload(read_json(args.input), thresholds, "file:%s" % args.input)
    else:
        report = fetch_live_report(args)
    if args.format == "json":
        emit_json(report)
    else:
        print(render_report(report))
    return 0


def command_compare(args):
    thresholds = thresholds_from_args(args)
    before = report_from_payload(read_json(args.before), thresholds, "file:%s" % args.before)
    after = report_from_payload(read_json(args.after), thresholds, "file:%s" % args.after)
    changes = compare_reports(before, after, args.price_change_pct, args.loss_pct)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "before": args.before,
        "after": args.after,
        "changes": changes,
        "disclaimer": DISCLAIMER,
    }
    if args.format == "json":
        emit_json(payload)
    else:
        print("# 持仓样本比较\n\n%s\n\n说明：%s" % (render_changes(changes), DISCLAIMER))
    return 0


def command_watch(args):
    if args.interval <= 0:
        raise PortfolioError("--interval must be greater than zero")
    if args.max_iterations < 0:
        raise PortfolioError("--max-iterations must be non-negative")
    if args.price_change_pct < 0:
        raise PortfolioError("--price-change-pct must be non-negative")

    previous = None
    if args.state_file and Path(args.state_file).expanduser().exists():
        previous_payload = read_json(args.state_file)
        previous = report_from_payload(previous_payload, thresholds_from_args(args), "state:%s" % args.state_file)

    iteration = 0
    while True:
        iteration += 1
        report = fetch_live_report(args)
        changes = compare_reports(previous, report, args.price_change_pct, args.loss_pct) if previous else []
        sample = {"iteration": iteration, "report": report, "changes": changes}
        if args.format == "json":
            emit_json(sample, compact=True)
        else:
            if iteration > 1:
                print("\n---\n")
            print("%s\n\n%s" % (render_report(report), render_changes(changes)), flush=True)
        if args.state_file:
            try:
                atomic_write_json(args.state_file, report)
            except OSError as exc:
                raise PortfolioError("Could not write state file %s: %s" % (args.state_file, exc)) from exc
        previous = report
        if args.max_iterations and iteration >= args.max_iterations:
            break
        time.sleep(args.interval)
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "accounts":
            return command_accounts(args)
        if args.command == "analyze":
            return command_analyze(args)
        if args.command == "compare":
            return command_compare(args)
        if args.command == "watch":
            return command_watch(args)
        parser.error("unknown command")
    except PortfolioError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("monitor stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
