# Agent Operations Plan

This plan intentionally excludes NFF. It uses only TWS API scanners, historical bars, labelled quote streams, news, account state and paper execution.

## Operating contract

1. The scheduler uses `America/New_York`, an exchange calendar, holidays and early closes.
2. The LLM never processes every tick. Deterministic code aggregates bars and calculates signals; the Agent wakes on state transitions or scheduled summaries.
3. `ibkr_probe_capabilities` is mandatory after every connection and reconnection.
4. `DELAYED_ONLY` and `ACCOUNT_ONLY` profiles never produce actionable intraday orders.
5. A symbol must have a fresh `LIVE` operational stream before `ibkr_place_guarded_stock_order` can submit a limit order.
6. The original unguarded order tools remain disabled unless `IBKR_ENABLE_LEGACY_ORDER_TOOLS=true` is explicitly set.

## Daily schedule in US Eastern Time

| Window | Agent behavior |
|---|---|
| 06:45–07:00 | Connect, obtain accounts/server time, run capability probe, read API limits and system bulletins. |
| 07:00–08:30 | Run sequential scanners: MOST_ACTIVE, HOT_BY_VOLUME, TOP_PERC_GAIN, TOP_PERC_LOSE. Pull cached daily/5-minute history for the union. |
| 08:30–09:15 | Score liquidity, historical volatility, gap, relative volume and benchmark-relative strength. Reduce to 20–30 symbols. |
| 09:15–09:25 | Call `ibkr_plan_watchlist`; start operational streams for benchmarks, holdings and accepted candidates. |
| 09:30–09:45 | Observation only. Build 1/5/15-minute opening ranges, VWAP, spread and relative-volume state. |
| 09:45–11:30 | Calculate signals each closed 1-minute bar; rank every 5 minutes; produce Agent summary every 15 minutes or on material events. |
| 11:30–13:30 | Reduce ranking to every 10 minutes and Agent summaries to every 30 minutes. Keep risk monitoring continuous. |
| 13:30–15:30 | Restore 5-minute ranking and 15-minute summaries. Focus on VWAP reclaim/reject, morning-range breaks and volume reacceleration. |
| 15:30–16:00 | No ordinary new entries after 15:30 by default. Review open orders and overnight permissions. |
| 16:00–17:00 | Reconcile bars, orders and fills; calculate signal outcomes and paper slippage; stop streams; write the daily report. |

## Radar state machine

```text
DISCOVERED → WATCHING → SETUP_FORMING → TRIGGERED
           → CONFIRMED → PAPER_ACTIONABLE → ACTIVE_POSITION
           → EXITED or INVALIDATED
```

A state transition, not every quote, is the event sent to the Agent.

## Minimum deterministic signals

- Opening-range breakout/rejection.
- Price relative to session VWAP.
- 1/5/15-minute momentum.
- Relative volume versus prior sessions at the same clock time.
- Spread in basis points.
- Bid/ask size imbalance as an auxiliary top-of-book feature.
- Realized volatility and ATR-normalized movement.
- Relative strength versus SPY or QQQ.
- Data quality: market-data type, transport freshness, missing fields and reconnect status.

## Recommended event payload

```json
{
  "symbol": "AAPL",
  "state_from": "SETUP_FORMING",
  "state_to": "TRIGGERED",
  "market_data_type": "LIVE",
  "trade_eligible": true,
  "bar_end_utc": "2026-08-06T14:45:00+00:00",
  "conditions": {
    "above_or_high": true,
    "above_vwap": true,
    "relative_volume": 2.1,
    "spread_bps": 3.4,
    "relative_strength_vs_qqq": 0.006
  },
  "invalidation": ["close below opening range", "live stream becomes stale"]
}
```

## Reconnection behavior

- On connectivity loss: mark every signal non-actionable and stop new orders.
- On `1101`: recreate all operational market-data subscriptions from the registry, then require fresh updates before restoring actionability.
- On `1102`: keep subscriptions but re-check timestamps and market-data type.
- On `10197`: treat the paper feed as unavailable until the competing shared-data session is resolved.

The current branch exposes the registry and labelled streams needed for this behavior. Full automatic replay after `1101` remains a later improvement; the skill instructs the Agent to rerun preflight and rebuild streams.
