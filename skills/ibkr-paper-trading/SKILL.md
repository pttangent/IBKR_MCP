---
name: ibkr-paper-trading
description: Place and monitor tightly guarded IBKR paper stock limit orders after account, data, risk, idempotency, and explicit-confirmation checks.
---

# IBKR Guarded Paper Trading

## Hard prerequisites

1. Server policy reports `agent_mode=paper`.
2. `ibkr_connect` positively identifies the configured paper account.
3. `ibkr_probe_capabilities` reports `LIVE_FULL`.
4. The exact symbol has an active operational stream reporting fresh `LIVE` data.
5. The user has explicitly approved the order details or supplied the workflow's confirmation token.

Do not use this skill under delayed/frozen/unknown data. Do not substitute a successful paper fill for evidence of live fill quality.

## Order workflow

1. Read account values, positions, open orders and daily P&L.
2. Verify no duplicate open order already expresses the same symbol and side.
3. Determine a limit price from the current fresh bid/ask; do not submit a market order.
4. Call `ibkr_validate_order_intent` with account, symbol, side, quantity, estimated price and confirmation token.
5. Stop if any blocker is returned.
6. Call `ibkr_place_guarded_stock_order` with the same immutable details.
7. Record order ID, orderRef, quote timestamp, bid, ask, limit price and rationale.
8. Monitor status and use `ibkr_cancel_guarded_order` when the thesis invalidates, the quote becomes stale, or the time-in-force policy expires.

## Risk rules

- Respect `IBKR_MAX_ORDER_NOTIONAL_USD` and `IBKR_MAX_ORDERS_PER_DAY`.
- A confirmation token is single-use; never retry a submission with the same token after an ambiguous response until order state is reconciled.
- Guarded orders currently support USD stocks only.
- Short sales are blocked; a SELL quantity cannot exceed the visible long position.
- Do not increase quantity after confirmation without a new confirmation token.
- Do not place a new ordinary entry after 15:30 ET unless the user's strategy explicitly allows it.
- Suspend all new orders on connectivity loss, `10197`, stale data or account-mode ambiguity.
- The default legacy unguarded stock/option order tools must remain disabled.

## Paper execution caveat

Report that paper fills are top-of-book simulations without queue or deep-liquidity realism. Evaluate the strategy using observed spread, theoretical fill rules and slippage scenarios, not paper fill price alone.
