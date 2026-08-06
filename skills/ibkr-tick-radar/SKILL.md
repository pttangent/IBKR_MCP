---
name: ibkr-tick-radar
description: Operate the IBKR real-time stock Time & Sales radar, interpret deterministic microstructure signals, decay scales, and calibrated 1/3/5-minute outcomes without sending raw ticks to the language model.
---

# IBKR Tick Radar

## Preconditions

1. Run `ibkr-preflight`.
2. Require `LIVE_FULL` for every stock promoted to tick-by-tick.
3. Confirm the account has the relevant Network A/B/C Level-1 permissions.
4. Never treat delayed quotes, stale ticks, or a quote-only symbol as trade-eligible.

## Capacity plan

Call `ibkr_plan_tick_radar` before starting. With the default 100 market-data lines, keep at most five continuous tick-by-tick stocks. Use ordinary quote streams for the rest.

Prefer stable core coverage over rapid rotation because IBKR allows only one tick-by-tick request for the same instrument within 15 seconds and rotation creates blind intervals.

For the current watchlist, default to:

- continuous ticks: NVDA, MU, SNDK, GEV, AMD;
- quote-only: AAPL, SKHY, XE.

Change this allocation when the user explicitly changes priorities or a quote-only symbol becomes the focus.

## Start sequence

1. Call `ibkr_start_tick_radar` with `tickType="AllLast"`.
2. Open or report the local dashboard path `/radar`.
3. Read `ibkr_get_tick_radar_snapshot` after enough baseline data has accumulated.
4. Do not summarize signals as statistically calibrated until their empirical horizon reports `calibrated=true`.

## Agent cadence

- Raw tick ingestion and one-second calculations remain deterministic in the MCP process.
- Read alerts only when `ibkr_get_tick_radar_alerts` returns a new threshold crossing.
- Read the full snapshot every 15 minutes, or when an alert changes the watchlist ranking.
- Do not invoke the language model for each tick or every one-second refresh.

## Signal interpretation

- `large_trade`: abnormal trade size/notional impulse; verify direction and special conditions.
- `volume_burst`: current five-second volume rate versus prior baseline.
- `trade_intensity`: print-arrival acceleration.
- `signed_flow`: buy/sell volume pressure using quote rule plus tick-rule fallback.
- `price_impulse`: volatility-adjusted short-horizon return.
- `quote_pressure`: low-weight top-of-book size imbalance.
- `composite`: weighted directional state.
- `activity`: unsigned urgency.

Score range is -100 to +100. Use confidence and data quality alongside score; never rank by score alone.

## Decay scale

Report half-life, effective horizon, and 1/3/5-minute projected score. Phrase it as signal persistence if no new confirmation arrives, not as a return promise.

When calibrated forward performance is available, report both decay projection and empirical signed return/hit rate with sample count. Do not hide a small sample count.

## Alert response

For a new alert:

1. Confirm `trade_eligible=true`, `halted=false`, and data is LIVE/fresh.
2. State symbol, direction, signal type, score, confidence, half-life, effective horizon, and 1/3/5-minute projected scores.
3. Check whether volume, flow, price impulse, and large-trade direction agree.
4. Flag disagreement as a mixed signal rather than averaging it away.
5. Do not place an order from this skill. Hand off to `ibkr-paper-trading` only after explicit user intent.

## Safety and data caveats

- Exclude unreported `AllLast` prints from primary signals by default.
- Treat Level-1 quote pressure as fragile; it is not Level-2 order-book pressure.
- A large print alone is not a buy/sell instruction.
- Paper fills do not validate real slippage or queue position.
- Stop radar streams when no longer needed to release market-data capacity.
