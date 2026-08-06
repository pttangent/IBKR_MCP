---
name: ibkr-operational-radar
description: Run a scanner-driven intraday market radar using only IBKR MCP scanners, historical bars, labelled market-data resources, news, and deterministic signal calculations.
---

# IBKR Operational Radar

## Prerequisite

Run the `ibkr-preflight` skill. Continue with intraday radar only under `LIVE_FULL`; under `DELAYED_ONLY`, explicitly relabel the workflow as delayed research and suppress actionable trade language.

## Universe construction

1. Run scans sequentially, not as permanent concurrent subscriptions:
   - `MOST_ACTIVE`
   - `HOT_BY_VOLUME`
   - `TOP_PERC_GAIN`
   - `TOP_PERC_LOSE`
2. Keep at most 50 rows per scan and deduplicate by conId/symbol.
3. Pull historical bars through cached/batched requests. Respect the historical governor and never repeat an identical request within 15 seconds.
4. Calculate deterministic filters: liquidity, ATR, gap, average volume, recent trend and relative strength versus SPY/QQQ.
5. Call `ibkr_plan_watchlist` with the reduced candidate list.

## Streaming

1. Start operational streams for benchmarks, positions and accepted candidates.
2. Read `ibkr://operational-market-data/{symbol}`.
3. Treat a symbol as actionable only when:
   - `market_data_type=LIVE`
   - `trade_eligible=true`
   - `is_transport_stale=false`
4. Stop streams for dropped candidates before adding replacements.

## Calculation cadence

- Aggregate deterministic 1-minute bars continuously.
- Recalculate symbol signals on each closed 1-minute bar.
- Re-rank the watchlist every 5 minutes during active windows and every 10 minutes at midday.
- Wake the language model every 15 minutes, every 30 minutes at midday, or immediately on a state transition, news headline, stale stream, disconnect or risk event.
- Never send every tick to the language model.

## Signal state machine

Use:

`DISCOVERED → WATCHING → SETUP_FORMING → TRIGGERED → CONFIRMED → PAPER_ACTIONABLE → ACTIVE_POSITION → EXITED/INVALIDATED`

Every transition must record the exact conditions and invalidation rule. A delayed/frozen/stale transition automatically invalidates `PAPER_ACTIONABLE`.

## Required data-quality fields

Always include market-data type, source timestamp, received timestamp, transport age and trade eligibility in radar output. If any are missing, label the signal non-actionable.

## News

Use `ibkr_get_news_providers` first. Distinguish company/news-provider headlines from `ibkr://news-bulletins`, which contains IBKR operational messages. Do not invent sentiment from unavailable article text.
