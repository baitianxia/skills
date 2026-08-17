# Futu OpenAPI reference

Use this reference for connection setup and API-specific constraints. The bundled CLI deliberately exposes only read operations.

## Prerequisites

1. Install, start, and sign in to Futu OpenD.
2. Use the OpenD API listening host and port. Local defaults are `127.0.0.1:11111`.
3. Install the official Python SDK in the same interpreter that runs the script:

   ```bash
   python3 -m pip install futu-api
   ```

4. Confirm the interpreter with `python3 -c "import sys; print(sys.executable)"` if `import futu` fails.

Official setup documentation:

- OpenAPI architecture: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html
- OpenD setup: https://openapi.futunn.com/futu-api-doc/en/quick/opend-base.html
- Python setup: https://openapi.futunn.com/futu-api-doc/en/quick/env.html
- Python SDK installation example: https://openapi.futunn.com/futu-api-doc/en/quick/demo.html

The official documentation states Python 3.6+ and recommends Python 3.8. A much newer interpreter may still be incompatible with the currently published SDK or its dependencies; use a separate compatible virtual environment if installation fails.

The current Futu SDK creates its own log directory under the user's home directory during import. In a restricted Codex sandbox this may produce a permission error before any OpenD connection is attempted. Request approval for the live command or have the user run it in their local terminal; do not redirect or replace `HOME` to bypass the sandbox.

## Connection and account selection

`OpenSecTradeContext` accepts a market filter, OpenD host/port, optional encryption setting, and `SecurityFirm`. The script also supports `OpenFutureTradeContext` and `OpenCryptoTradeContext` through `--account-type future|crypto`.

Common `SecurityFirm` values:

| CLI value | Broker |
| --- | --- |
| `FUTUSECURITIES` | Futu HK |
| `FUTUINC` | Moomoo US |
| `FUTUSG` | Moomoo SG |
| `FUTUAU` | Moomoo AU |
| `FUTUCA` | Moomoo CA |
| `FUTUMY` | Moomoo MY |
| `FUTUJP` | Moomoo JP |
| `NONE` | Let OpenD/SDK select when supported |

Use `accounts` to call `get_acc_list()` first. The script prints full `acc_id` values because they are required for reliable queries, but only the final four characters of card numbers. Futu recommends `acc_id` rather than `acc_index`, because indices can change when accounts are opened or closed.

Official references:

- Trade context objects: https://openapi.futunn.com/futu-api-doc/en/trade/base.html
- Account list: https://openapi.futunn.com/futu-api-doc/en/trade/get-acc-list.html
- Trading definitions: https://openapi.futunn.com/futu-api-doc/en/trade/trade.html

## APIs used

The script calls only:

- `get_acc_list()` to enumerate accounts.
- `position_list_query()` to read holdings for an explicit account and environment.
- `get_market_snapshot()` to enrich held symbols with current/last available quote data.

It does not call trade unlocking, order placement, modification, cancellation, or execution endpoints.

Position fields used include `code`, `stock_name`, `position_market`, `qty`, `can_sell_qty`, `currency`, `nominal_price`, `market_val`, cost fields, P/L fields, and `position_side`. Futu returns `pl_ratio` in percentage points: `20` means 20%, not 0.20.

Official references:

- Positions: https://openapi.futunn.com/futu-api-doc/en/trade/get-position-list.html
- Market snapshot: https://openapi.futunn.com/futu-api-doc/en/quote/get-market-snapshot.html

## Rate limits and data quality

- A positions query that forces `refresh_cache=True` is limited to 10 requests per 30 seconds per account ID. Cached queries normally do not trigger that limit.
- Market snapshots are limited to 60 requests per 30 seconds and at most 400 symbols per request. Under Hong Kong BMP permissions, several product classes are limited to 20 symbols per request; the CLI therefore defaults to batches of 20.
- Quote availability, delay, and fields depend on market-data entitlements. A missing snapshot is not proof that a holding is absent.
- `market_val`, P/L, and quote timestamps come from Futu/OpenD. Treat invalidity flags and `N/A` values as missing, not zero.
- Aggregate market value, P/L, and concentration only inside a currency bucket. FX-normalized totals require explicit rates and valuation timestamps; this skill does not invent them.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FUTU_OPEND_HOST` | `127.0.0.1` | OpenD API host |
| `FUTU_OPEND_PORT` | `11111` | OpenD API port |
| `FUTU_SECURITY_FIRM` | `NONE` | Futu/Moomoo broker enum |
| `FUTU_TRD_MARKET` | `NONE` | Account market filter |
| `FUTU_TRD_ENV` | `real` | `real` or `simulate` |
| `FUTU_ACCOUNT_TYPE` | `security` | `security`, `future`, or `crypto` |
| `FUTU_ACCOUNT_ID` | unset | Explicit trading account ID |
| `FUTU_CURRENCY` | `USD` | Crypto position currency; ignored for other accounts |
| `FUTU_OPEND_ENCRYPT` | `auto` | `auto`, `true`, or `false` |
| `FUTU_WATCH_INTERVAL` | `60` | Poll interval in seconds |

Prefer environment variables for recurring local monitoring, but do not put them or portfolio state into source control. Bind OpenD to localhost unless remote access is deliberately secured according to Futu's OpenD documentation.
