"""Connection management tools for IBKR TWS API."""

import os
from typing import Any, Dict

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

try:
    from ..models import AppContext
except ImportError:
    from src.models import AppContext


def register_connection_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def ibkr_connect(
        ctx: Context[ServerSession, AppContext],
        host: str = os.getenv("TWS_HOST", "127.0.0.1"),
        port: int = int(os.getenv("TWS_PORT", 7497)),
        clientId: int = int(os.getenv("TWS_CLIENT_ID", 1)),
    ) -> Dict[str, Any]:
        """Connect and verify that the session matches the configured safety mode."""
        app_context = ctx.request_context.lifespan_context
        tws = app_context.tws
        runtime = app_context.runtime
        await tws.connect(host, port, clientId)

        accounts = list(tws.ib.managedAccounts()) if tws.ib else []
        account_mode, account_reason = runtime.detect_account_mode(accounts)
        if runtime.policy.agent_mode == "paper" and account_mode != "paper":
            tws.disconnect()
            return {
                "status": "rejected",
                "error": "paper mode could not positively identify a paper account",
                "accounts": accounts,
                "account_mode": account_mode,
                "account_mode_reason": account_reason,
                "remediation": (
                    "Set TWS_PAPER_ACCOUNT to the exact paper account ID and log "
                    "TWS/IB Gateway into the paper username."
                ),
            }

        return {
            "status": "connected",
            "host": host,
            "port": port,
            "clientId": clientId,
            "accounts": accounts,
            "account_mode": account_mode,
            "account_mode_reason": account_reason,
            "policy": runtime.policy.as_dict(),
            "next_step": "Call ibkr_probe_capabilities before starting a radar or order workflow.",
        }

    @mcp.tool()
    async def ibkr_disconnect(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Stop operational streams and disconnect from TWS/IB Gateway."""
        app_context = ctx.request_context.lifespan_context
        for stream in list(app_context.runtime.operational_streams.values()):
            task = stream.get("task")
            if task:
                task.cancel()
        app_context.runtime.operational_streams.clear()
        app_context.tws.disconnect()
        return {"status": "disconnected"}

    @mcp.tool()
    async def ibkr_get_status(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Return connection state plus operational policy and stream status."""
        app_context = ctx.request_context.lifespan_context
        return {
            "is_connected": app_context.tws.is_connected(),
            "operational": app_context.runtime.status(),
        }

    @mcp.tool()
    async def ibkr_get_current_time(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Get the current IBKR server time."""
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        current_time = await tws.ib.reqCurrentTimeAsync()
        return {
            "server_time": current_time.isoformat(),
            "timestamp": current_time.timestamp(),
        }

    @mcp.tool()
    async def ibkr_get_managed_accounts(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """Get managed account IDs and the paper/live account heuristic."""
        app_context = ctx.request_context.lifespan_context
        tws = app_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        accounts = list(tws.ib.managedAccounts())
        mode, reason = app_context.runtime.detect_account_mode(accounts)
        return {
            "accounts": accounts,
            "count": len(accounts),
            "account_mode": mode,
            "account_mode_reason": reason,
        }
