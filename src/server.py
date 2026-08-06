"""IBKR TWS MCP server with operational safety defaults."""

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, List

from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

from .models import AppContext
from .operational import RuntimePolicy, RuntimeState
from .prompts import register_all_prompts
from .resources import (
    register_market_data_resource,
    register_news_resource,
    register_operational_market_data_resource,
    register_portfolio_resource,
)
from .tools import (
    register_account_tools,
    register_advanced_tools,
    register_connection_tools,
    register_contract_tools,
    register_fundamentals_tools,
    register_market_data_tools,
    register_news_tools,
    register_operational_tools,
    register_options_tools,
    register_order_tools,
    register_read_only_order_tools,
    register_scanner_tools,
)
from .tws_client import TWSClient


SERVER_POLICY = RuntimePolicy.from_env()


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage the TWS client and operational state lifecycle."""
    tws = TWSClient()
    runtime = RuntimeState(policy=SERVER_POLICY)
    try:
        yield AppContext(tws=tws, runtime=runtime)
    finally:
        for stream in list(runtime.operational_streams.values()):
            task = stream.get("task")
            if task:
                task.cancel()
        runtime.operational_streams.clear()
        if tws.is_connected():
            tws.disconnect()


mcp = FastMCP(
    "IBKR TWS MCP Server",
    lifespan=app_lifespan,
    streamable_http_path="/api/v1/mcp",
)

# Read-only tools are always exposed.
register_connection_tools(mcp)
register_contract_tools(mcp)
register_market_data_tools(mcp)
register_account_tools(mcp)
register_news_tools(mcp)
register_scanner_tools(mcp)
register_advanced_tools(mcp)
register_fundamentals_tools(mcp)
register_operational_tools(mcp)

legacy_write_tools_enabled = (
    SERVER_POLICY.order_tools_enabled and SERVER_POLICY.enable_legacy_order_tools
)

# Preserve monitoring in safe mode without publishing the original mutation tools.
if not legacy_write_tools_enabled:
    register_read_only_order_tools(mcp)

# The original order and options modules expose direct order placement. They are
# disabled by default. Guarded stock limit orders remain available through the
# operational module and enforce policy at call time.
if legacy_write_tools_enabled:
    register_order_tools(mcp)
    register_options_tools(mcp)

# Legacy resources remain for compatibility. New agent workflows should use the
# labelled operational resource, which reports LIVE/DELAYED and freshness.
register_market_data_resource(mcp)
register_portfolio_resource(mcp)
register_news_resource(mcp)
register_operational_market_data_resource(mcp)
register_all_prompts(mcp)


async def health_check(request):
    """Return non-sensitive process health and safety mode."""
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "status": "healthy",
            "agent_mode": SERVER_POLICY.agent_mode,
            "order_tools_enabled": SERVER_POLICY.order_tools_enabled,
            "legacy_order_tools_enabled": legacy_write_tools_enabled,
            "read_only_order_monitoring": not legacy_write_tools_enabled,
        }
    )


mcp_base_app = mcp.streamable_http_app()
mcp_base_app.routes.extend([Route("/health", health_check)])


@asynccontextmanager
async def combined_lifespan(app_instance):
    async with mcp.session_manager.run():
        yield


mcp_base_app.router.lifespan_context = combined_lifespan


def _cors_origins() -> List[str]:
    raw = os.getenv(
        "IBKR_CORS_ORIGINS",
        "http://localhost,http://127.0.0.1",
    )
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or ["http://localhost", "http://127.0.0.1"]


allowed_origins = _cors_origins()
app = CORSMiddleware(
    mcp_base_app,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "Mcp-Session-Id",
        "Mcp-Initialize-Request",
    ],
    expose_headers=["Mcp-Session-Id"],
    max_age=3600,
)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("SERVER_HOST", "127.0.0.1")
    port = int(os.getenv("SERVER_PORT", 8000))
    uvicorn.run(app, host=host, port=port, log_level="info")
