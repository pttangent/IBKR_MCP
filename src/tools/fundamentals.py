"""Fundamentals tools — IBKR Reuters Fundamentals (requires ~$7/mo subscription)"""
import json
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from ..models import AppContext


def register_fundamentals_tools(mcp: FastMCP):
    """Register fundamentals tools on the MCP server."""

    @mcp.tool()
    async def fundamentals_snapshot(
        symbol: str, exchange: str = "SMART", currency: str = "USD",
        ctx: Context[ServerSession, AppContext] = None
    ) -> str:
        """Get company snapshot: overview, ratios, executives, forecasts.
        Requires Reuters Fundamentals subscription (~$7/month)."""
        tws = ctx.request_context.lifespan_context.tws
        try:
            contract = await tws.qualify_contract(symbol, "STK", exchange, currency)
            data = await tws.get_fundamental_data(contract, "ReportsFinSummary")
            return json.dumps({"symbol": symbol, "data": data}, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e), "note": "Requires Reuters Fundamentals subscription"})

    @mcp.tool()
    async def fundamentals_financials(
        symbol: str, report_type: str = "ReportsFinStatements",
        exchange: str = "SMART", currency: str = "USD",
        ctx: Context[ServerSession, AppContext] = None
    ) -> str:
        """Get full financial statements: income statement, balance sheet, cash flow.
        report_type: 'ReportsFinStatements' or 'ReportsFinSummary'.
        Requires Reuters Fundamentals subscription (~$7/month)."""
        tws = ctx.request_context.lifespan_context.tws
        try:
            contract = await tws.qualify_contract(symbol, "STK", exchange, currency)
            data = await tws.get_fundamental_data(contract, report_type)
            return json.dumps({"symbol": symbol, "reportType": report_type, "data": data}, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e), "note": "Requires Reuters Fundamentals subscription"})

    @mcp.tool()
    async def fundamentals_ownership(
        symbol: str, exchange: str = "SMART", currency: str = "USD",
        ctx: Context[ServerSession, AppContext] = None
    ) -> str:
        """Get ownership structure: institutional holders and insider transactions.
        Requires Reuters Fundamentals subscription (~$7/month)."""
        tws = ctx.request_context.lifespan_context.tws
        try:
            contract = await tws.qualify_contract(symbol, "STK", exchange, currency)
            data = await tws.get_fundamental_data(contract, "ReportsOwnership")
            return json.dumps({"symbol": symbol, "data": data}, indent=2, default=str)
        except Exception as e:
            return json.dumps({"error": str(e), "note": "Requires Reuters Fundamentals subscription"})
