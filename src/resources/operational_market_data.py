"""Labelled market-data resource with UTC timestamps and subscription budgets."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from ..models import AppContext
from ..operational import MARKET_DATA_TYPE_NAMES


def _valid_number(value: Any) -> bool:
    return value is not None and value == value and value != -1


def _utc_iso(value: Any) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _resource_id(symbol: str, sec_type: str, currency: str) -> str:
    if sec_type.upper() == "CASH":
        return f"{symbol.upper()}.{currency.upper()}"
    return symbol.upper()


def _public_snapshot(stream: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    received_at = stream.get("received_at")
    age = None
    if isinstance(received_at, datetime):
        age = max(0.0, (now - received_at).total_seconds())
    stale_after = float(stream.get("stale_after_seconds", 15.0))
    is_stale = age is None or age > stale_after
    market_data_type = stream.get("market_data_type", "UNKNOWN")
    return {
        "resource_id": stream["resource_id"],
        "contract": stream["contract_params"],
        "market_data_type": market_data_type,
        "market_data_type_id": stream.get("market_data_type_id"),
        "trade_eligible": market_data_type == "LIVE" and not is_stale,
        "source_timestamp_utc": stream.get("source_timestamp_utc"),
        "received_timestamp_utc": _utc_iso(received_at),
        "transport_age_seconds": age,
        "is_transport_stale": is_stale,
        "data": stream.get("data", {}),
        "last_error": stream.get("last_error"),
    }


def register_operational_market_data_resource(mcp: FastMCP) -> None:
    registry: Dict[str, Dict[str, Any]] = {}

    @mcp.resource("ibkr://operational-market-data/{resource_id}")
    async def get_operational_market_data(resource_id: str) -> str:
        """Read the latest labelled quote snapshot for a subscribed symbol."""
        stream = registry.get(resource_id.upper())
        if not stream:
            return json.dumps(
                {"error": "stream not active", "resource_id": resource_id.upper()}
            )
        return json.dumps(_public_snapshot(stream), default=str)

    @mcp.tool()
    async def ibkr_start_operational_market_data(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        secType: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Dict[str, Any]:
        """Start a live-or-delayed quote stream whose data type is explicit."""
        app_context = ctx.request_context.lifespan_context
        tws = app_context.tws
        runtime = app_context.runtime
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}

        rid = _resource_id(symbol, secType, currency)
        if rid in runtime.operational_streams:
            return {
                "status": "already_subscribed",
                "resource_uri": f"ibkr://operational-market-data/{rid}",
                "snapshot": _public_snapshot(runtime.operational_streams[rid]),
            }
        if len(runtime.operational_streams) >= runtime.policy.max_operational_streams:
            return {
                "error": "operational stream budget exhausted",
                "active": len(runtime.operational_streams),
                "maximum": runtime.policy.max_operational_streams,
            }

        decision = await runtime.governor.check_general()
        if not decision.allowed:
            return {
                "error": "pacing guard rejected subscription",
                "pacing": decision.as_dict(),
            }

        from ib_async import Contract, Forex, Stock

        if secType.upper() == "STK":
            requested = Stock(symbol.upper(), exchange, currency.upper())
        elif secType.upper() == "CASH":
            requested = Forex(f"{symbol.upper()}{currency.upper()}")
        else:
            requested = Contract(
                symbol=symbol.upper(),
                secType=secType.upper(),
                exchange=exchange,
                currency=currency.upper(),
            )

        qualified = await tws.ib.qualifyContractsAsync(requested)
        if not qualified:
            return {"error": f"could not qualify contract for {rid}"}
        contract = qualified[0]
        tws.ib.reqMarketDataType(3)
        ticker = tws.ib.reqMktData(contract, "", False, False)

        stream: Dict[str, Any] = {
            "resource_id": rid,
            "contract": contract,
            "contract_params": {
                "symbol": symbol.upper(),
                "secType": secType.upper(),
                "exchange": exchange,
                "currency": currency.upper(),
                "conId": getattr(contract, "conId", None),
            },
            "ticker": ticker,
            "task": None,
            "market_data_type": "UNKNOWN",
            "market_data_type_id": None,
            "received_at": None,
            "source_timestamp_utc": None,
            "data": {},
            "last_error": None,
            "stale_after_seconds": runtime.policy.live_stale_after_seconds,
        }
        runtime.operational_streams[rid] = stream
        registry[rid] = stream

        async def pump() -> None:
            last_signature = None
            try:
                while True:
                    await tws.ib.updateEvent
                    md_id = int(getattr(ticker, "marketDataType", 0) or 0)
                    md_name = MARKET_DATA_TYPE_NAMES.get(md_id, "UNKNOWN")
                    source_time = getattr(ticker, "time", None)
                    data = {
                        "last": getattr(ticker, "last", None),
                        "bid": getattr(ticker, "bid", None),
                        "ask": getattr(ticker, "ask", None),
                        "volume": getattr(ticker, "volume", None),
                        "bidSize": getattr(ticker, "bidSize", None),
                        "askSize": getattr(ticker, "askSize", None),
                        "close": getattr(ticker, "close", None),
                    }
                    signature = (
                        source_time,
                        data["last"],
                        data["bid"],
                        data["ask"],
                        data["volume"],
                        data["bidSize"],
                        data["askSize"],
                        md_id,
                    )
                    if signature == last_signature:
                        continue
                    if not any(_valid_number(value) for value in data.values()):
                        continue
                    last_signature = signature
                    stream["market_data_type_id"] = md_id or None
                    stream["market_data_type"] = md_name
                    stream["source_timestamp_utc"] = _utc_iso(source_time)
                    stream["received_at"] = datetime.now(timezone.utc)
                    stream["data"] = data
                    stream["stale_after_seconds"] = (
                        runtime.policy.live_stale_after_seconds
                        if md_name == "LIVE"
                        else runtime.policy.delayed_stale_after_seconds
                    )
                    await ctx.session.send_resource_updated(
                        f"ibkr://operational-market-data/{rid}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stream["last_error"] = str(exc)
                runtime.recent_errors.append(
                    {
                        "stage": "operational_stream",
                        "resource_id": rid,
                        "error": str(exc),
                    }
                )
            finally:
                try:
                    tws.ib.cancelMktData(contract)
                except Exception:
                    pass

        stream["task"] = asyncio.create_task(pump())
        return {
            "status": "subscribed",
            "resource_uri": f"ibkr://operational-market-data/{rid}",
            "resource_id": rid,
            "note": "trade_eligible becomes true only after a fresh LIVE update.",
        }

    @mcp.tool()
    async def ibkr_stop_operational_market_data(
        ctx: Context[ServerSession, AppContext],
        resourceId: str,
    ) -> Dict[str, Any]:
        """Stop one labelled quote stream and release its market-data line."""
        runtime = ctx.request_context.lifespan_context.runtime
        rid = resourceId.upper()
        stream = runtime.operational_streams.pop(rid, None)
        registry.pop(rid, None)
        if not stream:
            return {"error": "stream not active", "resource_id": rid}
        task = stream.get("task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return {"status": "stopped", "resource_id": rid}

    @mcp.tool()
    async def ibkr_list_operational_market_data(
        ctx: Context[ServerSession, AppContext],
    ) -> Dict[str, Any]:
        """List active streams and their current trade-eligibility state."""
        runtime = ctx.request_context.lifespan_context.runtime
        return {
            "count": len(runtime.operational_streams),
            "maximum": runtime.policy.max_operational_streams,
            "streams": [
                _public_snapshot(stream)
                for stream in runtime.operational_streams.values()
            ],
        }
