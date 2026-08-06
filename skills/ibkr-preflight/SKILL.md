---
name: ibkr-preflight
description: Establish the real IBKR account mode, market-data type, news providers, pacing limits, and safe operating profile before any radar or paper-trading workflow.
---

# IBKR Preflight

## Trigger

Use before every new TWS/IB Gateway session, after a reconnect, after a market-data error, or whenever the user asks what their paper account can actually access.

## Required sequence

1. Call `ibkr_connect` with the configured host, port and client ID.
2. Stop immediately if paper mode returns `status=rejected`.
3. Call `ibkr_get_api_limits` and retain the configured stream/request budgets.
4. Call `ibkr_probe_capabilities` with a representative liquid symbol, normally SPY.
5. Read and report:
   - `account_mode` and its reason.
   - `operating_profile`.
   - actual `market_data.market_data_type`.
   - whether historical bars succeeded.
   - available news provider codes.
   - `trade_actions_allowed`.
6. Do not generalize one symbol's exchange permissions to every symbol. Require a symbol-specific operational stream before order action.

## Decision rules

- `LIVE_FULL`: radar may use fresh operational streams. Orders still require policy and symbol-specific checks.
- `DELAYED_ONLY`: allow research, scanner ranking and delayed observation; prohibit intraday execution and time-sensitive alerts.
- `ACCOUNT_ONLY`: use account, order-status and P&L tools only. Explain that usable market data was not detected.
- Market-data type `UNKNOWN`: treat as non-actionable.
- Error `10197`: explain that shared paper/live data is in a competing session and rerun preflight only after it is resolved.

## Paper user explanation

State that a paper account does not receive a separate free live-data package. Without paid or shared subscriptions, stock/option prices are generally delayed, tick-by-tick and depth are unavailable, and option Greeks are incomplete. Free API news may include BRFUPDN, BRFG and DJNL when enabled in TWS API News Configuration.

## Output

Return a compact capability matrix and one of: `LIVE_FULL`, `DELAYED_ONLY`, or `ACCOUNT_ONLY`. Never say “real-time” merely because a price was returned.
