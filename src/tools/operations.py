"""Operational MCP tools for capability discovery, planning, and guarded orders."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from ..models import AppContext
from ..operational import api_limits_payload, plan_watchlist, probe_capabilities


def register_operational_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def ibkr_get_agent_policy(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Return the active read-only, paper, or live operating policy."""
        return ctx.request_context.lifespan_context.runtime.policy.as_dict()

    @mcp.tool()
    async def ibkr_get_api_limits(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Return documented TWS API and unsubscribed-paper constraints."""
        runtime = ctx.request_context.lifespan_context.runtime
        return api_limits_payload(runtime.policy)

    @mcp.tool()
    async def ibkr_probe_capabilities(
        ctx: Context[ServerSession, AppContext],
        symbol: str = "SPY",
        timeoutSeconds: float = 4.0,
        includeHistorical: bool = True,
    ) -> Dict[str, Any]:
        """Probe the actual account, quote, historical-data, and news permissions."""
        app_context = ctx.request_context.lifespan_context
        return await probe_capabilities(
            app_context.tws,
            app_context.runtime,
            symbol=symbol,
            timeout_seconds=timeoutSeconds,
            include_historical=includeHistorical,
        )

    @mcp.tool()
    async def ibkr_get_operational_status(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Return active labelled streams, pacing usage, errors, and last preflight."""
        return ctx.request_context.lifespan_context.runtime.status()

    @mcp.tool()
    async def ibkr_plan_watchlist(
        ctx: Context[ServerSession, AppContext],
        symbols: List[str],
        benchmarks: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Fit a candidate universe into the configured market-data-line budget."""
        runtime = ctx.request_context.lifespan_context.runtime
        return plan_watchlist(
            symbols,
            runtime.policy,
            active_streams=len(runtime.operational_streams),
            benchmarks=benchmarks,
        )

    @mcp.tool()
    async def ibkr_get_operational_historical_data(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        duration: str = "5 D",
        barSize: str = "5 mins",
        whatToShow: str = "TRADES",
        useRTH: bool = True,
        exchange: str = "SMART",
        currency: str = "USD",
        maxAgeSeconds: float = 60.0,
    ) -> Dict[str, Any]:
        """Get cached historical bars through the conservative pacing governor."""
        from ib_async import Stock

        app_context = ctx.request_context.lifespan_context
        tws = app_context.tws
        runtime = app_context.runtime
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        symbol = symbol.strip().upper()
        what_to_show = whatToShow.strip().upper()
        cache_key = "|".join(
            [symbol, exchange, currency, duration, barSize, what_to_show, str(useRTH)]
        )
        historical_cache = getattr(runtime, "historical_cache", None)
        if historical_cache is None:
            historical_cache = {}
            runtime.historical_cache = historical_cache
        cached = historical_cache.get(cache_key)
        if cached and maxAgeSeconds > 0:
            age = time.monotonic() - float(cached["stored_at"])
            if age <= maxAgeSeconds:
                result = dict(cached["payload"])
                result.update({"cache_hit": True, "cache_age_seconds": round(age, 3)})
                return result

        general = await runtime.governor.check_general()
        if not general.allowed:
            return {
                "error": "pacing guard rejected contract qualification",
                "pacing": general.as_dict(),
            }
        qualified = await tws.ib.qualifyContractsAsync(
            Stock(symbol, exchange, currency.upper())
        )
        if not qualified:
            return {"error": f"could not qualify {symbol}"}
        contract = qualified[0]
        con_id = getattr(contract, "conId", symbol)
        historical = await runtime.governor.check_historical(
            request_key=f"{con_id}|{duration}|{barSize}|{what_to_show}|{useRTH}",
            contract_key=f"{con_id}|{exchange}|{what_to_show}",
            bid_ask=what_to_show == "BID_ASK",
        )
        if not historical.allowed:
            return {
                "error": "pacing guard rejected historical request",
                "pacing": historical.as_dict(),
            }

        bars = await tws.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=barSize,
            whatToShow=what_to_show,
            useRTH=useRTH,
            formatDate=1,
        )
        payload = {
            "symbol": symbol,
            "conId": con_id,
            "duration": duration,
            "barSize": barSize,
            "whatToShow": what_to_show,
            "useRTH": useRTH,
            "bars": [
                {
                    "date": (
                        bar.date.isoformat()
                        if hasattr(bar.date, "isoformat")
                        else str(bar.date)
                    ),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "average": getattr(bar, "average", None),
                    "barCount": getattr(bar, "barCount", None),
                }
                for bar in bars
            ],
            "count": len(bars),
            "cache_hit": False,
        }
        historical_cache[cache_key] = {
            "stored_at": time.monotonic(),
            "payload": payload,
        }
        return payload

    @mcp.tool()
    async def ibkr_validate_order_intent(
        ctx: Context[ServerSession, AppContext],
        account: str,
        symbol: str,
        action: str,
        quantity: float,
        estimatedPrice: float,
        confirmationToken: str = "",
    ) -> Dict[str, Any]:
        """Validate an intended order without placing it."""
        runtime = ctx.request_context.lifespan_context.runtime
        action = action.strip().upper()
        result = runtime.validate_order_intent(
            account=account,
            quantity=quantity,
            estimated_price=estimatedPrice,
            confirmation_token=confirmationToken,
        )
        if action not in {"BUY", "SELL"}:
            result["blockers"].append("action must be BUY or SELL")
            result["allowed"] = False
        result.update(
            {
                "symbol": symbol.strip().upper(),
                "action": action,
                "quantity": quantity,
            }
        )
        return result

    @mcp.tool()
    async def ibkr_place_guarded_stock_order(
        ctx: Context[ServerSession, AppContext],
        account: str,
        symbol: str,
        action: str,
        quantity: int,
        limitPrice: float,
        confirmationToken: str,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Dict[str, Any]:
        """Place a guarded USD stock limit order after account, data, and risk checks.

        The exact symbol must already have a fresh LIVE operational stream. Market
        orders are intentionally unsupported. The default server mode prevents live
        submission and disables the original unguarded order tools.
        """
        from ib_async import LimitOrder, Stock

        app_context = ctx.request_context.lifespan_context
        tws = app_context.tws
        runtime = app_context.runtime
        if not tws or not tws.is_connected():
            return {"allowed": False, "error": "TWS client not connected"}

        symbol = symbol.strip().upper()
        action = action.strip().upper()
        currency = currency.strip().upper()
        if action not in {"BUY", "SELL"}:
            return {"allowed": False, "error": "action must be BUY or SELL"}
        if quantity <= 0 or limitPrice <= 0:
            return {"allowed": False, "error": "quantity and limitPrice must be positive"}

        validation = runtime.validate_order_intent(
            account=account,
            quantity=quantity,
            estimated_price=limitPrice,
            confirmation_token=confirmationToken,
        )
        blockers = list(validation["blockers"])
        accounts = list(tws.ib.managedAccounts())
        if account not in accounts:
            blockers.append("account is not visible in managedAccounts")
        if currency != "USD":
            blockers.append("guarded order notional is USD-only; non-USD orders are blocked")

        order_ref = f"mcp-guarded:{confirmationToken[:48]}"
        visible_trades = list(tws.ib.trades())
        if any(getattr(item.order, "orderRef", "") == order_ref for item in visible_trades):
            blockers.append("confirmation token was already used for an order")
        for item in tws.ib.openTrades():
            if (
                getattr(item.contract, "symbol", "").upper() == symbol
                and getattr(item.order, "action", "").upper() == action
                and getattr(item.orderStatus, "remaining", 0) > 0
            ):
                blockers.append("a same-symbol same-side open order already exists")
                break

        if action == "SELL":
            long_position = sum(
                float(position.position)
                for position in tws.ib.positions()
                if position.account == account
                and getattr(position.contract, "symbol", "").upper() == symbol
            )
            if long_position < quantity:
                blockers.append("short sales are disabled and the long position is insufficient")

        stream = runtime.operational_streams.get(symbol)
        if not stream:
            blockers.append("start an operational market-data stream for this symbol first")
        else:
            received_at = stream.get("received_at")
            age = None
            if isinstance(received_at, datetime):
                age = (datetime.now(timezone.utc) - received_at).total_seconds()
            if stream.get("market_data_type") != "LIVE":
                blockers.append("the symbol is not receiving LIVE market data")
            if age is None or age > runtime.policy.live_stale_after_seconds:
                blockers.append("the symbol's LIVE stream is stale")

        if blockers:
            validation.update({"allowed": False, "blockers": blockers, "symbol": symbol})
            return validation

        decision = await runtime.governor.check_general()
        if not decision.allowed:
            return {
                "allowed": False,
                "error": "pacing guard rejected order request",
                "pacing": decision.as_dict(),
            }

        qualified = await tws.ib.qualifyContractsAsync(Stock(symbol, exchange, currency))
        if not qualified:
            return {"allowed": False, "error": f"could not qualify {symbol}"}
        contract = qualified[0]
        order = LimitOrder(action, quantity, limitPrice, account=account)
        order.orderRef = order_ref
        trade = tws.ib.placeOrder(contract, order)
        await asyncio.sleep(0.5)

        today = datetime.now(timezone.utc).date().isoformat()
        if runtime.order_count_date != today:
            runtime.order_count_date = today
            runtime.order_count = 0
        runtime.order_count += 1

        return {
            "allowed": True,
            "submitted": True,
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status if trade.orderStatus else "Submitted",
            "account": account,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "orderType": "LMT",
            "limitPrice": limitPrice,
            "orderRef": order.orderRef,
            "daily_guarded_order_count": runtime.order_count,
        }

    @mcp.tool()
    async def ibkr_cancel_guarded_order(
        ctx: Context[ServerSession, AppContext],
        orderId: int,
    ) -> Dict[str, Any]:
        """Cancel an order visible to this TWS client."""
        app_context = ctx.request_context.lifespan_context
        if not app_context.runtime.policy.order_tools_enabled:
            return {"error": "order actions are disabled by policy"}
        trade = next(
            (
                item
                for item in app_context.tws.ib.trades()
                if item.order.orderId == orderId
            ),
            None,
        )
        if trade is None:
            return {"error": f"order {orderId} not found"}
        app_context.tws.ib.cancelOrder(trade.order)
        return {"orderId": orderId, "status": "Cancellation requested"}
