# IBKR Tick-by-Tick Live Radar

## Purpose

This module turns IBKR stock Time & Sales (`reqTickByTickData`, `Last` or `AllLast`) plus ordinary Level-1 quotes into deterministic, auditable intraday signals. Raw ticks never go through an LLM. The Agent reads only state changes, threshold alerts, decay scales, and calibrated forward outcomes.

Dashboard: `http://127.0.0.1:8000/radar`

JSON snapshot: `http://127.0.0.1:8000/api/v1/radar/snapshot`

## IBKR constraints

- Real-time tick-by-tick requires corresponding LIVE Level-1 permissions. Delayed data cannot substitute.
- The default 100 market-data-line account normally permits about 5 simultaneous tick-by-tick subscriptions.
- IBKR permits no more than one tick-by-tick request for the same instrument within 15 seconds.
- `AllLast` contains additional trade classes, including unreported/average-price/derivative-related prints. The radar stores their count but excludes `unreported` ticks from primary signals by default.
- Options do not have real-time tick-by-tick through this API.

Recommended topology:

- ordinary Level-1 quote streams for the whole watchlist;
- continuous `AllLast` for the highest-priority 3-5 stocks;
- explicit promotion/demotion rather than rapid automatic rotation.

For the current universe, one practical allocation is:

- tick-by-tick core: `NVDA`, `MU`, `SNDK`, `GEV`, `AMD`;
- quote-only: `AAPL`, `SKHY`, `XE`.

## Signals

All scores are normalized to `[-100, 100]`; positive means buy-side/upward pressure and negative means sell-side/downward pressure. `activity` is unsigned.

### Large trade

A trade is compared against the symbol's recent log-size distribution using a robust median/MAD z-score and against an absolute dollar-notional threshold. Each event creates an impulse that decays exponentially. Default half-life: 75 seconds.

### Volume burst

Five-second volume per second is compared with the preceding 120-second baseline, excluding the current short window. Direction comes from signed flow, then price impulse, then large-trade direction. Default half-life: 180 seconds.

### Trade intensity

Five-second trade-arrival rate is compared with the preceding baseline. Default half-life: 120 seconds.

### Signed flow

Trades are classified with the quote rule first, then midpoint, then tick-rule fallback. The signal uses rolling buy/sell volume imbalance and discounts confidence when many trades remain unclassified. Default half-life: 90 seconds.

### Price impulse

The latest price is compared with the price 30 seconds earlier and standardized by recent one-second realized volatility. Default half-life: 120 seconds.

### Quote pressure

Top-of-book bid/ask size imbalance. It has low weight because Level-1 size is fragile and does not represent the full order book. Default half-life: 30 seconds.

### Composite

Weights: large trade 20%, volume burst 20%, trade intensity 10%, signed flow 25%, price impulse 20%, quote pressure 5%.

## Decay scale

For each signal the dashboard displays current score, half-life, effective horizon, and projected score after 1, 3, and 5 minutes assuming no confirming ticks arrive.

`projected_score(h) = current_score * exp(-ln(2) * h / half_life)`

This is a persistence model, not a return guarantee.

## Online calibration

When a signal crosses the alert threshold, the engine records its direction and start price. It evaluates signed forward returns after 1, 3, and 5 minutes and maintains sample count, hit rate, mean signed return, EWMA signed return, and EWMA absolute return. A horizon is labelled calibrated only after the configured minimum sample count.

## Tools

```text
ibkr_plan_tick_radar(
  tickSymbols=["NVDA", "MU", "SNDK", "GEV", "AMD"],
  quoteOnlySymbols=["AAPL", "SKHY", "XE"]
)

ibkr_start_tick_radar(
  tickSymbols=["NVDA", "MU", "SNDK", "GEV", "AMD"],
  quoteOnlySymbols=["AAPL", "SKHY", "XE"],
  tickType="AllLast",
  replaceExisting=true
)

ibkr_get_tick_radar_snapshot()
ibkr_get_tick_radar_alerts(limit=30, minimumAbsoluteScore=55)
ibkr_stop_tick_radar()
```

## Environment

```env
IBKR_TICK_RADAR_MAX_STREAMS=5
IBKR_TICK_RADAR_RETENTION_SECONDS=900
IBKR_TICK_RADAR_BASELINE_SECONDS=120
IBKR_TICK_RADAR_RECENT_SECONDS=5
IBKR_TICK_RADAR_FLOW_SECONDS=15
IBKR_TICK_RADAR_PRICE_SECONDS=30
IBKR_TICK_RADAR_MIN_BASELINE_SECONDS=30
IBKR_TICK_RADAR_LARGE_NOTIONAL_USD=100000
IBKR_TICK_RADAR_LARGE_Z=3.5
IBKR_TICK_RADAR_ALERT_SCORE=55
IBKR_TICK_RADAR_REARM_SCORE=35
IBKR_TICK_RADAR_ALERT_COOLDOWN_SECONDS=30
IBKR_TICK_RADAR_DECAY_FLOOR=20
IBKR_TICK_RADAR_INCLUDE_UNREPORTED=false
IBKR_TICK_RADAR_MIN_CALIBRATION_SAMPLES=20
IBKR_TICK_RADAR_STALE_AFTER_SECONDS=10
```

## Execution boundary

The radar emits information and alerts. It never places an order. Any paper order must separately pass the guarded paper-trading tools.
