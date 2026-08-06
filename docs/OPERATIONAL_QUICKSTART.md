# Operational Quick Start

## 1. Configure safe mode

Copy `.env.operational.example` into your environment configuration.

For data exploration only:

```env
IBKR_AGENT_MODE=read_only
IBKR_ENABLE_LEGACY_ORDER_TOOLS=false
SERVER_HOST=127.0.0.1
```

For guarded paper limit orders:

```env
IBKR_AGENT_MODE=paper
TWS_PAPER_ACCOUNT=DU1234567
IBKR_ENABLE_LEGACY_ORDER_TOOLS=false
IBKR_REQUIRE_ORDER_CONFIRMATION=true
```

Do not set `IBKR_AGENT_MODE=live` until paper validation is complete. Live mode also requires `IBKR_ALLOW_LIVE_TRADING=true`.

## 2. Start TWS or IB Gateway

Log in with the intended username, enable socket API access and set a unique client ID. Use an explicit paper username for paper trading.

## 3. Start the MCP

```bash
uv sync
uv run python main.py
```

The server now binds to `127.0.0.1` by default. Configure `IBKR_CORS_ORIGINS` rather than exposing wildcard CORS.

## 4. Mandatory preflight

Call in this order:

1. `ibkr_connect`
2. `ibkr_get_api_limits`
3. `ibkr_probe_capabilities(symbol="SPY")`
4. Inspect `operating_profile`, `account_mode` and the returned news providers.

Do not assume a paper account has live data. Do not infer data type from the presence of a price; use the returned `market_data_type`.

## 5. Build a radar

1. Run scanner tools sequentially.
2. Deduplicate symbols.
3. Pull daily or intraday bars with `ibkr_get_operational_historical_data`; this path applies cache and historical pacing checks.
4. Call `ibkr_plan_watchlist`.
5. Start `ibkr_start_operational_market_data` for accepted symbols.
6. Read `ibkr://operational-market-data/{symbol}` and accept only `trade_eligible=true` for intraday action.
7. Stop discarded streams to release quote lines.

## 6. Guarded paper order

Before placing a paper order:

1. Confirm the connection returned `account_mode=paper`.
2. Run capability preflight.
3. Start the exact symbol's operational market-data stream.
4. Confirm its resource reports fresh `LIVE` data.
5. Call `ibkr_validate_order_intent`.
6. Call `ibkr_place_guarded_stock_order` with a USD stock limit price and a single-use confirmation token.

The guarded tool rejects delayed/frozen/stale data, unknown accounts, excessive notional, reused confirmation tokens, duplicate same-side orders, short sales and missing confirmation.
