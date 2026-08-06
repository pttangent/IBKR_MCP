"""Account and portfolio management tools for IBKR TWS API.

NOTE on ib_async 2.1.0 API:
- `IB.reqPnL(account, modelCode="")` and `IB.reqPnLSingle(account, modelCode, conId)`
  are SYNC, subscription-based calls. There is NO `reqPnLAsync` / `reqPnLSingleAsync`
  in ib_async -> those names raise AttributeError.
- `IB.reqAccountUpdates(account)` is a BLOCKING method that calls `self._run(...)`,
  which raises "This event loop is already running" when invoked inside the MCP's
  async event loop. Use the low-level `ib.client.reqAccountUpdates(True, account)`
  (non-blocking send) instead, then read `ib.accountValues(account)`.
- `IB.accountSummary()` is also blocking (`self._run`). Use the async
  `IB.accountSummaryAsync(account)` which returns the list of AccountValue directly.
"""

import asyncio
from typing import Dict, Any, List, Optional
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from ..models import AppContext


def _num(x):
    """Coerce NaN/None to None so MCP JSON responses stay well-formed."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and x != x:  # NaN != NaN
            return None
    except Exception:
        pass
    return x


def register_account_tools(mcp: FastMCP):
    """Register account and portfolio management tools."""

    @mcp.tool()
    async def ibkr_get_account_summary(
        ctx: Context[ServerSession, AppContext],
        account: str = "",
        tags: str = "NetLiquidation,TotalCashValue,SettledCash,BuyingPower,GrossPositionValue"
    ) -> Dict[str, Any]:
        """Get account summary with key metrics.

        Args:
            account: Account ID (empty for all accounts)
            tags: Comma-separated list of tags to retrieve (informational; ib_async
                  always requests the full standard summary set)

        Returns:
            Account summary with requested metrics
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        # accountSummaryAsync is the async (non-blocking) variant and returns a
        # list[AccountValue] directly (no blocking _run()).
        summary = await tws.ib.accountSummaryAsync(account)

        result = {}
        for item in summary:
            if account and item.account != account:
                continue
            result[item.tag] = {
                "value": item.value,
                "currency": item.currency,
                "account": item.account,
            }

        return {"summary": result, "account": account or "All"}

    @mcp.tool()
    async def ibkr_get_positions(
        ctx: Context[ServerSession, AppContext],
        account: str = ""
    ) -> Dict[str, Any]:
        """Get current portfolio positions.

        Args:
            account: Account ID (empty for all accounts)

        Returns:
            List of positions with contract details and P&L
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        positions = tws.ib.positions()

        results = []
        for pos in positions:
            if account and pos.account != account:
                continue
            results.append({
                "account": pos.account,
                "contract": {
                    "conId": pos.contract.conId,
                    "symbol": pos.contract.symbol,
                    "secType": pos.contract.secType,
                    "exchange": pos.contract.exchange,
                    "currency": pos.contract.currency,
                    "localSymbol": pos.contract.localSymbol,
                },
                "position": pos.position,
                "avgCost": pos.avgCost,
            })

        return {"positions": results, "count": len(results)}

    @mcp.tool()
    async def ibkr_get_account_values(
        ctx: Context[ServerSession, AppContext],
        account: str = ""
    ) -> Dict[str, Any]:
        """Get detailed account values and portfolio data.

        Args:
            account: Account ID (uses first available if not specified)

        Returns:
            Comprehensive account values including cash, margins, and portfolio
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        if not account:
            accounts = tws.ib.managedAccounts()
            if not accounts:
                return {"error": "No managed accounts found"}
            account = accounts[0]

        # Use the LOW-LEVEL non-blocking subscribe. The blocking IB.reqAccountUpdates
        # variant calls self._run() -> "This event loop is already running" inside an
        # async tool. The low-level client.reqAccountUpdates(True, account) just sends
        # the request; we then read ib.accountValues(account) after a brief wait.
        try:
            tws.ib.client.reqAccountUpdates(True, account)
            # Allow TWS to push the account-value snapshot.
            await asyncio.sleep(2.0)
            account_values = tws.ib.accountValues(account)
        finally:
            # Unsubscribe to avoid leaking the account-update subscription.
            try:
                tws.ib.client.reqAccountUpdates(False, account)
            except Exception:
                pass

        values = {}
        for av in account_values:
            key = f"{av.tag}_{av.currency}" if av.currency else av.tag
            values[key] = {
                "value": av.value,
                "currency": av.currency,
                "account": av.account,
            }

        return {
            "account": account,
            "values": values,
            "count": len(values),
        }

    @mcp.tool()
    async def ibkr_get_pnl(
        ctx: Context[ServerSession, AppContext],
        account: str = ""
    ) -> Dict[str, Any]:
        """Get real-time P&L for account.

        Args:
            account: Account ID (uses first available if not specified)

        Returns:
            Daily and unrealized P&L
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        if not account:
            accounts = tws.ib.managedAccounts()
            if not accounts:
                return {"error": "No managed accounts found"}
            account = accounts[0]

        # reqPnL is a SYNC subscription-based call (no reqPnLAsync in ib_async).
        # It returns a live-updated PnL object; we wait for the first pnlEvent.
        pnl = tws.ib.reqPnL(account)

        received = []

        def on_pnl(p):
            if p.account == account:
                received.append(p)

        tws.ib.pnlEvent += on_pnl
        try:
            loop = asyncio.get_event_loop()
            end = loop.time() + 5.0
            while not received and loop.time() < end:
                await asyncio.sleep(0.1)
        finally:
            tws.ib.pnlEvent -= on_pnl
            try:
                tws.ib.cancelPnL(account)
            except Exception:
                pass

        src = received[0] if received else pnl
        return {
            "account": account,
            "dailyPnL": _num(src.dailyPnL),
            "unrealizedPnL": _num(src.unrealizedPnL),
            "realizedPnL": _num(src.realizedPnL),
        }

    @mcp.tool()
    async def ibkr_get_pnl_single(
        ctx: Context[ServerSession, AppContext],
        account: str,
        conId: int
    ) -> Dict[str, Any]:
        """Get real-time P&L for a single position.

        Args:
            account: Account ID
            conId: Contract ID

        Returns:
            Position-specific P&L details
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        # reqPnLSingle is a SYNC subscription-based call (no reqPnLSingleAsync in ib_async).
        pnl = tws.ib.reqPnLSingle(account, "", conId)

        received = []

        def on_pnl(p):
            if p.account == account and p.conId == conId:
                received.append(p)

        tws.ib.pnlSingleEvent += on_pnl
        try:
            loop = asyncio.get_event_loop()
            end = loop.time() + 5.0
            while not received and loop.time() < end:
                await asyncio.sleep(0.1)
        finally:
            tws.ib.pnlSingleEvent -= on_pnl
            try:
                tws.ib.cancelPnLSingle(account, "", conId)
            except Exception:
                pass

        src = received[0] if received else pnl
        return {
            "account": account,
            "conId": conId,
            "position": _num(src.position),
            "dailyPnL": _num(src.dailyPnL),
            "unrealizedPnL": _num(src.unrealizedPnL),
            "realizedPnL": _num(src.realizedPnL),
            "value": _num(getattr(src, "value", None)),
        }
