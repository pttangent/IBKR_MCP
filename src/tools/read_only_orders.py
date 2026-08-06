"""Read-only order and execution tools safe to expose in every server mode."""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from ..models import AppContext


def _trade_payload(trade: Any) -> Dict[str, Any]:
    status = trade.orderStatus
    return {
        "orderId": trade.order.orderId,
        "contract": {
            "conId": getattr(trade.contract, "conId", None),
            "symbol": trade.contract.symbol,
            "secType": trade.contract.secType,
            "exchange": trade.contract.exchange,
            "currency": trade.contract.currency,
        },
        "order": {
            "account": getattr(trade.order, "account", ""),
            "action": trade.order.action,
            "totalQuantity": trade.order.totalQuantity,
            "orderType": trade.order.orderType,
            "lmtPrice": getattr(trade.order, "lmtPrice", None),
            "auxPrice": getattr(trade.order, "auxPrice", None),
            "orderRef": getattr(trade.order, "orderRef", ""),
        },
        "status": status.status if status else "Unknown",
        "filled": status.filled if status else 0,
        "remaining": status.remaining if status else trade.order.totalQuantity,
        "avgFillPrice": status.avgFillPrice if status else 0,
        "lastFillPrice": status.lastFillPrice if status else 0,
        "whyHeld": status.whyHeld if status else "",
    }


def register_read_only_order_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def ibkr_get_open_orders(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Get open orders without exposing any order mutation."""
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        trades = list(tws.ib.openTrades())
        return {"orders": [_trade_payload(item) for item in trades], "count": len(trades)}

    @mcp.tool()
    async def ibkr_get_all_orders(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Get orders known to the current TWS client session."""
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        trades = list(tws.ib.trades())
        return {"orders": [_trade_payload(item) for item in trades], "count": len(trades)}

    @mcp.tool()
    async def ibkr_get_order_status(
        ctx: Context[ServerSession, AppContext],
        orderId: int,
    ) -> Dict[str, Any]:
        """Get one order's current status without modifying it."""
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        trade = next(
            (item for item in tws.ib.trades() if item.order.orderId == orderId),
            None,
        )
        if trade is None:
            return {"error": f"order {orderId} not found"}
        return _trade_payload(trade)

    @mcp.tool()
    async def ibkr_get_executions(
        ctx: Context[ServerSession, AppContext],
        symbol: Optional[str] = None,
        secType: Optional[str] = None,
        exchange: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get execution history through the async TWS request."""
        from ib_async import ExecutionFilter

        tws = ctx.request_context.lifespan_context.tws
        runtime = ctx.request_context.lifespan_context.runtime
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        decision = await runtime.governor.check_general()
        if not decision.allowed:
            return {"error": "pacing guard rejected request", "pacing": decision.as_dict()}

        request_filter = ExecutionFilter()
        if symbol:
            request_filter.symbol = symbol.strip().upper()
        if secType:
            request_filter.secType = secType.strip().upper()
        if exchange:
            request_filter.exchange = exchange.strip().upper()
        executions = await tws.ib.reqExecutionsAsync(request_filter)
        result = []
        for fill in executions:
            execution = fill.execution
            report = fill.commissionReport
            result.append(
                {
                    "execId": execution.execId,
                    "orderId": execution.orderId,
                    "time": (
                        execution.time.isoformat()
                        if hasattr(execution.time, "isoformat")
                        else str(execution.time)
                    ),
                    "contract": {
                        "conId": getattr(fill.contract, "conId", None),
                        "symbol": fill.contract.symbol,
                        "secType": fill.contract.secType,
                        "exchange": fill.contract.exchange,
                        "currency": fill.contract.currency,
                    },
                    "shares": execution.shares,
                    "price": execution.price,
                    "side": execution.side,
                    "cumQty": execution.cumQty,
                    "avgPrice": execution.avgPrice,
                    "commission": report.commission if report else None,
                    "realizedPNL": report.realizedPNL if report else None,
                }
            )
        return {"executions": result, "count": len(result)}
