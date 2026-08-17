import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("futu_portfolio.py")
SPEC = importlib.util.spec_from_file_location("futu_portfolio", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PortfolioAnalysisTests(unittest.TestCase):
    def test_analysis_groups_currencies_and_emits_threshold_alerts(self):
        positions = [
            {
                "code": "US.AAA",
                "stock_name": "Alpha",
                "qty": 50,
                "currency": "USD",
                "market_val": 6000,
                "nominal_price": 120,
                "pl_ratio": 5,
                "pl_val": 300,
                "position_side": "LONG",
                "cost_price": 114,
                "cost_price_valid": True,
            },
            {
                "code": "US.BBB",
                "stock_name": "Beta",
                "qty": 40,
                "currency": "USD",
                "market_val": 4000,
                "nominal_price": 100,
                "pl_ratio": -15,
                "pl_val": -700,
                "position_side": "LONG",
                "cost_price": 117.5,
                "cost_price_valid": True,
            },
            {
                "code": "HK.00388",
                "stock_name": "HKEX",
                "qty": 25,
                "currency": "HKD",
                "market_val": 10000,
                "nominal_price": 400,
                "pl_ratio": 1,
                "pl_val": 100,
                "position_side": "LONG",
                "cost_price": 396,
                "cost_price_valid": True,
            },
        ]
        snapshots = [
            {"code": "US.AAA", "last_price": 120, "prev_close_price": 100, "update_time": "2026-08-15 16:00:00"}
        ]
        report = MODULE.analyze_positions(positions, snapshots)

        self.assertEqual([item["currency"] for item in report["currency_buckets"]], ["HKD", "USD"])
        usd = next(item for item in report["currency_buckets"] if item["currency"] == "USD")
        self.assertEqual(usd["gross_market_value"], 10000)
        alpha = next(item for item in report["positions"] if item["code"] == "US.AAA")
        self.assertAlmostEqual(alpha["weight_pct"], 60)
        alert_types = {(item["code"], item["type"]) for item in report["alerts"]}
        self.assertIn(("US.AAA", "daily_move"), alert_types)
        self.assertIn(("US.BBB", "loss_threshold"), alert_types)
        self.assertIn("Multiple currencies detected; totals and weights are calculated per currency only.", report["warnings"])

    def test_invalid_and_na_values_are_missing_not_zero(self):
        report = MODULE.analyze_positions(
            [
                {
                    "code": "US.BAD",
                    "stock_name": "Missing fields",
                    "qty": "N/A",
                    "currency": "USD",
                    "market_val": "N/A",
                    "cost_price": 12,
                    "cost_price_valid": False,
                    "pl_ratio": -99,
                    "pl_ratio_valid": False,
                    "pl_val": float("nan"),
                }
            ]
        )
        position = report["positions"][0]
        self.assertIsNone(position["quantity"])
        self.assertIsNone(position["market_value"])
        self.assertIsNone(position["pl_ratio_pct"])
        self.assertFalse(position["cost_price_valid"])
        self.assertEqual({item["type"] for item in report["alerts"]}, {"invalid_cost", "missing_market_value"})
        json.dumps(report, allow_nan=False)

    def test_compare_detects_position_and_market_changes(self):
        before = MODULE.analyze_positions(
            [
                {"code": "US.AAA", "qty": 10, "currency": "USD", "market_val": 1000, "nominal_price": 100, "pl_ratio": -5},
                {"code": "US.OLD", "qty": 1, "currency": "USD", "market_val": 10, "nominal_price": 10, "pl_ratio": 0},
            ]
        )
        after = MODULE.analyze_positions(
            [
                {"code": "US.AAA", "qty": 12, "currency": "USD", "market_val": 1224, "nominal_price": 102, "pl_ratio": -12},
                {"code": "US.NEW", "qty": 1, "currency": "USD", "market_val": 20, "nominal_price": 20, "pl_ratio": 0},
            ]
        )
        changes = MODULE.compare_reports(before, after, price_change_pct=1, loss_pct=-10)
        change_types = {(item["code"], item["type"]) for item in changes}
        self.assertIn(("US.AAA", "quantity_change"), change_types)
        self.assertIn(("US.AAA", "sample_price_move"), change_types)
        self.assertIn(("US.AAA", "loss_threshold_crossed"), change_types)
        self.assertIn(("US.NEW", "new_position"), change_types)
        self.assertIn(("US.OLD", "closed_position"), change_types)

    def test_accounts_mask_full_card_numbers(self):
        account = MODULE.normalize_account(
            {
                "acc_id": 123456789,
                "trd_env": "REAL",
                "uni_card_num": "1000123412345678",
                "card_num": "90008765",
                "trdmarket_auth": ["HK", "US"],
            }
        )
        self.assertEqual(account["acc_id"], "123456789")
        self.assertEqual(account["universal_card_suffix"], "***5678")
        self.assertEqual(account["card_suffix"], "***8765")
        serialized = json.dumps(account)
        self.assertNotIn("1000123412345678", serialized)
        self.assertNotIn("90008765", serialized)

    def test_offline_cli_outputs_valid_json_without_futu_dependency(self):
        payload = {
            "positions": [
                {
                    "code": "HK.00700",
                    "stock_name": "Tencent",
                    "qty": 10,
                    "currency": "HKD",
                    "market_val": 5000,
                    "nominal_price": 500,
                    "pl_ratio": 2,
                    "pl_val": 100,
                    "cost_price": 490,
                    "cost_price_valid": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "analyze", "--input", str(path), "--format", "json"],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["positions"][0]["code"], "HK.00700")
        self.assertEqual(report["source"], "file:%s" % path)

    def test_saved_report_round_trip_preserves_pl_ratio_and_markdown(self):
        original = MODULE.analyze_positions(
            [
                {
                    "code": "US.ROUND",
                    "stock_name": "Round Trip",
                    "qty": 5,
                    "currency": "USD",
                    "market_val": 12345.67,
                    "nominal_price": 2469.134,
                    "pl_ratio": -12.5,
                    "pl_val": -1000,
                    "cost_price": 2600,
                    "cost_price_valid": True,
                }
            ]
        )
        rebuilt = MODULE.report_from_payload(original, original["thresholds"], "round-trip")
        self.assertEqual(rebuilt["positions"][0]["pl_ratio_pct"], -12.5)
        markdown = MODULE.render_report(rebuilt)
        self.assertIn("12,345.67", markdown)
        self.assertIn("-12.50", markdown)

    def test_atomic_state_file_is_private(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.atomic_write_json(str(path), {"positions": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"positions": []})

    def test_read_only_provider_calls_expected_futu_methods_and_closes_contexts(self):
        class FakeFrame:
            def __init__(self, records):
                self.records = records

            def to_dict(self, orient="dict"):
                self.assert_orient = orient
                return self.records

        class FakeTradeContext:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                self.position_params = None

            def get_acc_list(self):
                return 0, FakeFrame([{"acc_id": 42, "trd_env": "REAL"}])

            def position_list_query(self, **kwargs):
                self.position_params = kwargs
                return 0, FakeFrame([{"code": "US.AAA", "qty": 1, "currency": "USD", "market_val": 10}])

            def close(self):
                self.closed = True

        class FakeQuoteContext:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                self.codes = None

            def get_market_snapshot(self, codes):
                self.codes = codes
                return 0, FakeFrame([{"code": "US.AAA", "last_price": 10}])

            def close(self):
                self.closed = True

        trade_contexts = []
        quote_contexts = []

        def make_trade_context(**kwargs):
            context = FakeTradeContext(**kwargs)
            trade_contexts.append(context)
            return context

        def make_quote_context(**kwargs):
            context = FakeQuoteContext(**kwargs)
            quote_contexts.append(context)
            return context

        fake_futu = types.SimpleNamespace(
            RET_OK=0,
            SecurityFirm=types.SimpleNamespace(NONE="NONE"),
            TrdMarket=types.SimpleNamespace(US="US"),
            TrdEnv=types.SimpleNamespace(REAL="REAL"),
            Currency=types.SimpleNamespace(USD="USD"),
            OpenSecTradeContext=make_trade_context,
            OpenFutureTradeContext=make_trade_context,
            OpenCryptoTradeContext=make_trade_context,
            OpenQuoteContext=make_quote_context,
        )
        args = types.SimpleNamespace(
            host="127.0.0.1",
            port=11111,
            encrypt="auto",
            security_firm="NONE",
            market="US",
            account_type="security",
            account_id="42",
            environment="real",
            refresh_cache=False,
            currency="USD",
            option_strategy_view=False,
            snapshot_batch_size=20,
        )
        provider = MODULE.FutuProvider(args)
        provider._module = fake_futu

        self.assertEqual(provider.get_accounts()[0]["acc_id"], 42)
        self.assertEqual(provider.get_positions()[0]["code"], "US.AAA")
        snapshots, warnings = provider.get_snapshots(["US.AAA"])

        self.assertEqual(snapshots[0]["last_price"], 10)
        self.assertEqual(warnings, [])
        self.assertTrue(all(context.closed for context in trade_contexts + quote_contexts))
        self.assertEqual(trade_contexts[-1].position_params["acc_id"], 42)
        self.assertEqual(trade_contexts[-1].position_params["trd_env"], "REAL")
        self.assertFalse(hasattr(FakeTradeContext, "place_order"))

    def test_crypto_requires_supported_security_firm(self):
        args = types.SimpleNamespace(
            host="127.0.0.1",
            port=11111,
            encrypt="auto",
            security_firm="NONE",
            market="NONE",
            account_type="crypto",
        )
        provider = MODULE.FutuProvider(args)
        provider._module = types.SimpleNamespace(
            SecurityFirm=types.SimpleNamespace(NONE="N/A"),
        )
        with self.assertRaisesRegex(MODULE.PortfolioError, "Crypto contexts require"):
            provider.trade_context()

    def test_sdk_log_permission_error_is_actionable(self):
        provider = MODULE.FutuProvider(types.SimpleNamespace())
        with mock.patch.object(MODULE.importlib, "import_module", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(MODULE.PortfolioError, "user log directory"):
                provider.futu()


if __name__ == "__main__":
    unittest.main()
