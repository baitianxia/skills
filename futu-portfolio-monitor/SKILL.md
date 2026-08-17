---
name: futu-portfolio-monitor
description: Read-only Futu OpenAPI portfolio inspection, analysis, and monitoring through a local OpenD gateway. Use when ChatGPT or Codex needs to list Futu or Futubull accounts, inspect real or simulated holdings, analyze position concentration and profit/loss by currency, compare portfolio snapshots, or watch holdings and price changes without placing trades.
---

# Futu Portfolio Monitor

Use the bundled CLI to query Futu OpenD and turn holdings into structured, auditable portfolio reports. Keep every workflow read-only.

## Safety invariants

- Never add or invoke `unlock_trade`, `place_order`, `modify_order`, or any other trading operation.
- Never request, store, print, or pass a trading password.
- Require an explicit `acc_id` for portfolio queries. Run `accounts` first and let the user confirm the account when it is not already known.
- Keep real and simulated environments explicit. Do not silently switch between them.
- Analyze each currency independently. Never add HKD, USD, CNY, or other currency values without explicit FX conversion supplied by the user.
- Treat output as monitoring and descriptive analysis, not investment advice or a buy/sell instruction.

## Prepare the connection

Read [references/futu-openapi.md](references/futu-openapi.md) when setting up OpenD, selecting a broker/market/account type, or diagnosing API failures.

Resolve all relative paths against the directory containing this `SKILL.md`. Prefer that directory's `.venv/bin/python` when it exists; this keeps the installed skill on the tested Futu SDK version. Otherwise use a Python environment that can import `futu`. If the module is absent, explain that the official package is `futu-api`; obtain approval before installing anything. Start and sign in to OpenD before making live API calls. In the examples below, replace `python3` with `.venv/bin/python` when the bundled environment is present.

Set connection values with CLI flags or these environment variables: `FUTU_OPEND_HOST`, `FUTU_OPEND_PORT`, `FUTU_SECURITY_FIRM`, `FUTU_TRD_MARKET`, `FUTU_TRD_ENV`, `FUTU_ACCOUNT_TYPE`, `FUTU_ACCOUNT_ID`, and `FUTU_CURRENCY`.

## Select the account

Run:

```bash
python3 scripts/futu_portfolio.py accounts \
  --market HK \
  --security-firm FUTUSECURITIES \
  --format markdown
```

Present the returned account ID, environment, account type, authorized markets, and masked card suffix. Ask the user to choose if more than one account is plausible. Prefer `acc_id` over account index because account indices can change.

## Analyze holdings

Run a live analysis with an explicit account and environment:

```bash
python3 scripts/futu_portfolio.py analyze \
  --account-id ACCOUNT_ID \
  --environment real \
  --market HK \
  --security-firm FUTUSECURITIES \
  --format json
```

Use `--no-quotes` if quote permissions are unavailable. Use `--refresh-cache` only when stale positions are suspected; the refreshed positions endpoint is rate-limited. Adjust `--concentration-pct`, `--loss-pct`, and `--daily-move-pct` only when the user supplies different thresholds.

For offline exports or reproducible analysis, pass a JSON file containing either a position list or an object with `positions` and optional `snapshots`:

```bash
python3 scripts/futu_portfolio.py analyze --input portfolio.json --format json
```

Report facts separately from interpretation. Call out quote/API warnings, invalid cost or P/L fields, missing valuations, currency boundaries, and the report timestamp.

## Compare snapshots

Compare two saved reports or exported position payloads:

```bash
python3 scripts/futu_portfolio.py compare \
  --before previous.json \
  --after current.json \
  --price-change-pct 2 \
  --format markdown
```

Summarize new and closed positions, quantity changes, threshold-crossing losses, and price moves between samples. Do not infer a trade when only a price or market value changed.

## Watch holdings

Start a bounded check before any long-running monitor:

```bash
python3 scripts/futu_portfolio.py watch \
  --account-id ACCOUNT_ID \
  --environment real \
  --market HK \
  --security-firm FUTUSECURITIES \
  --interval 60 \
  --max-iterations 1 \
  --state-file .futu-portfolio-state.json \
  --format json
```

Only omit `--max-iterations` when the user explicitly asks to keep monitoring. Explain that the foreground process must remain running and that alerts are printed to stdout; this skill does not send external notifications. The state file is written atomically with user-only permissions where supported.

Keep `--interval` at 60 seconds or longer for ordinary monitoring. Avoid `--refresh-cache` in a fast loop. Stop cleanly on `Ctrl-C`.

## Verify changes

Run the offline unit suite after editing the script:

```bash
python3 -m unittest -v scripts/test_futu_portfolio.py
```

Run a real smoke test only when OpenD is running, the `futu` module is installed, and the user has authorized access to the specified account. Never claim live connectivity was verified from offline tests.
