"""Streamlined MCP server entry point with modular tool structure."""

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from mcp.server.fastmcp import FastMCP
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

from .tws_client import TWSClient
from .models import AppContext
from .tools import (
    register_connection_tools,
    register_contract_tools,
    register_market_data_tools,
    register_order_tools,
    register_account_tools,
    register_news_tools,
    register_options_tools,
    register_scanner_tools,
    register_advanced_tools,
    register_fundamentals_tools
)
from .resources import (
    register_market_data_resource,
    register_portfolio_resource,
    register_news_resource
)
from .prompts import register_all_prompts


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage TWS client lifecycle."""
    tws = TWSClient()
    try:
        # TWS client is initialized but not connected here. Connection is done via the ibkr_connect tool.
        yield AppContext(tws=tws)
    finally:
        # Ensure TWS client is disconnected on shutdown
        if tws.is_connected():
            tws.disconnect()


# Create MCP server with lifespan
mcp = FastMCP(
    "IBKR TWS MCP Server",
    lifespan=app_lifespan,
    streamable_http_path="/api/v1/mcp"
)

# Register all tools
register_connection_tools(mcp)
register_contract_tools(mcp)
register_market_data_tools(mcp)
register_order_tools(mcp)
register_account_tools(mcp)
register_news_tools(mcp)
register_options_tools(mcp)
register_scanner_tools(mcp)
register_advanced_tools(mcp)
register_fundamentals_tools(mcp)

# Register all resources
register_market_data_resource(mcp)
register_portfolio_resource(mcp)
register_news_resource(mcp)

# Register all prompts
register_all_prompts(mcp)


# Health check endpoint
async def health_check(request):
    """Health check endpoint."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "healthy"})


# Get the MCP streamable HTTP app
mcp_base_app = mcp.streamable_http_app()

# Add custom routes
mcp_base_app.routes.extend([
    Route("/health", health_check),
])


# Enhanced lifespan that combines MCP's session manager with Starlette app lifecycle
@asynccontextmanager
async def combined_lifespan(app_instance):
    """Wrap the Starlette app to initialize MCP session manager."""
    # Get the MCP session manager and run it (initializes task group)
    # The TWS client is already managed by app_lifespan above
    async with mcp.session_manager.run():
        yield


# Replace the lifespan context - this combines MCP's task group init with our TWS setup
mcp_base_app.router.lifespan_context = combined_lifespan

# Add CORS middleware for browser-based MCP clients
app = CORSMiddleware(
    mcp_base_app,
    allow_origins=["*"],  # Allow all origins for browser-based clients
    allow_credentials=True,  # Allow credentials (cookies, authorization headers)
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=[
        "*",
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "Accept",
        "Accept-Encoding",
        "Accept-Language",
        "Cache-Control",
        "Connection",
        "Host",
        "Origin",
        "Referer",
        "Sec-Fetch-Dest",
        "Sec-Fetch-Mode",
        "Sec-Fetch-Site",
        "User-Agent",
        "Mcp-Session-Id",
        "Mcp-Initialize-Request",
    ],
    expose_headers=[
        "Mcp-Session-Id",
        "Access-Control-Allow-Origin",
        "Access-Control-Allow-Credentials",
        "Access-Control-Allow-Methods",
        "Access-Control-Allow-Headers",
    ],
    max_age=86400,  # Cache preflight for 24 hours
)


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", 8000))
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )
