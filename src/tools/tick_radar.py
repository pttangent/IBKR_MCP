"""MCP tools for a deterministic IBKR tick-by-tick radar."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from ..models import AppContext
from ..operational import MARKET_DATA_TYPE_NAMES
from ..tick_radar import TickRadarEngine, get_or_create_engine


def _valid(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) != -1
    except (TypeError, ValueError):
        return False


def _contract_payload(contract: Any) -> Dict[str, Any]:
    return {
        "symbol": str(getattr(contract, "symbol", "")),
        "localSymbol": str(getattr(contract, "localSymbol", "")),
        "secType": str(getattr(contract, "secType", "")),
        "exchange": str(getattr(contract, "exchange", "")),
        "primaryExchange": str(getattr(contract, "primaryExchange", "")),
        "currency": str(getattr(contract, "currency", "")),
        "conId": getattr(contract, "conId", None),
    }


class TickRadarManager:
    """Own IBKR quote/tick subscriptions and feed the pure radar engine."""

    def __init__(self, tws: Any, runtime: Any, engine: TickRadarEngine) -> None:
        self.tws = tws
        self.runtime = runtime
        self.engine = engine
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.last_tick_request: Dict[str, float] = {}
        self._error_handler_registered = False

    @property
    def ib(self) -> Any:
        return self.tws.ib

    def _ensure_error_handler(self) -> None:
        if self._error_handler_registered:
            return

        def on_error(req_id: int, error_code: int, error_string: str, contract: Any) -> None:
            symbol = str(getattr(contract, "symbol", "") or "").upper()
            error = {
                "stage": "tick_radar",
                "reqId": req_id,
                "errorCode": error_code,
                "error": error_string,
                "symbol": symbol or None,
            }
            self.runtime.recent_errors.append(error)
            if symbol and symbol in self.subscriptions:
                self.subscriptions[symbol]["last_error"] = error_string
                self.engine.mark_subscription(
                    symbol,
                    tick_active=bool(self.subscriptions[symbol].get("tick_ticker")),
                    tick_type=str(self.subscriptions[symbol].get("tick_type", "")),
                    contract=self.subscriptions[symbol].get("contract_payload", {}),
                    error=f"{error_code}: {error_string}",
                )

        self.ib.errorEvent += on_error
        self._error_handler = on_error
        self._error_handler_registered = True

    async def _qualify(self, symbol: str) -> Any:
        from ib_async import Stock

        requested = Stock(symbol.upper(), "SMART", "USD")
        qualified = await self.ib.qualifyContractsAsync(requested)
        if not qualified:
            raise ValueError(f"could not qualify stock contract for {symbol}")
        return qualified[0]

    async def start_symbol(self, symbol: str, *, tick_type: str, tick_enabled: bool, ignore_size: bool) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        if not symbol:
            return {"error": "empty symbol"}
        existing = self.subscriptions.get(symbol)
        if existing:
            if tick_enabled and not existing.get("tick_ticker"):
                await self._start_tick(existing, tick_type, ignore_size)
            return {"symbol": symbol, "status": "already_active", "tick_active": bool(existing.get("tick_ticker"))}

        decision = await self.runtime.governor.check_general()
        if not decision.allowed:
            return {"symbol": symbol, "error": "pacing guard rejected quote subscription", "pacing": decision.as_dict()}

        contract = await self._qualify(symbol)
        self.ib.reqMarketDataType(1)
        quote_ticker = self.ib.reqMktData(contract, "", False, False)
        entry: Dict[str, Any] = {
            "symbol": symbol,
            "contract": contract,
            "contract_payload": _contract_payload(contract),
            "quote_ticker": quote_ticker,
            "tick_ticker": None,
            "tick_type": "",
            "quote_callback": None,
            "tick_callback": None,
            "started_at": time.time(),
            "last_error": None,
        }
        self.subscriptions[symbol] = entry

        def on_quote_update(ticker: Any) -> None:
            md_id = int(getattr(ticker, "marketDataType", 0) or 0)
            md_name = MARKET_DATA_TYPE_NAMES.get(md_id, "UNKNOWN")
            timestamp = getattr(ticker, "time", None) or time.time()
            self.engine.ingest_quote(
                symbol,
                timestamp=timestamp,
                bid=getattr(ticker, "bid", None),
                ask=getattr(ticker, "ask", None),
                bid_size=getattr(ticker, "bidSize", None),
                ask_size=getattr(ticker, "askSize", None),
                market_data_type=md_name,
            )

        quote_ticker.updateEvent += on_quote_update
        entry["quote_callback"] = on_quote_update
        self.engine.mark_subscription(symbol, tick_active=False, contract=entry["contract_payload"])
        if tick_enabled:
            await self._start_tick(entry, tick_type, ignore_size)
        return {
            "symbol": symbol,
            "status": "subscribed",
            "tick_active": bool(entry.get("tick_ticker")),
            "contract": entry["contract_payload"],
        }

    async def _start_tick(self, entry: Dict[str, Any], tick_type: str, ignore_size: bool) -> None:
        symbol = entry["symbol"]
        if entry.get("tick_ticker"):
            return
        tick_count = sum(1 for item in self.subscriptions.values() if item.get("tick_ticker"))
        if tick_count >= self.engine.config.max_tick_streams:
            raise ValueError(f"tick-by-tick budget exhausted: {tick_count}/{self.engine.config.max_tick_streams}")
        now = time.monotonic()
        last = self.last_tick_request.get(symbol, 0.0)
        if now - last < 15.0:
            raise ValueError(
                f"IBKR allows only one tick-by-tick request for {symbol} within 15 seconds; "
                f"retry after {15.0 - (now - last):.1f}s"
            )
        decision = await self.runtime.governor.check_general()
        if not decision.allowed:
            raise ValueError(
                f"pacing guard rejected tick subscription: {decision.reason}; "
                f"retry after {decision.retry_after_seconds:.2f}s"
            )
        self.last_tick_request[symbol] = now
        contract = entry["contract"]
        ticker = self.ib.reqTickByTickData(contract, tick_type, 0, ignore_size)

        def on_tick_update(updated: Any) -> None:
            for tick in list(getattr(updated, "tickByTicks", []) or []):
                if not hasattr(tick, "price") or not hasattr(tick, "size"):
                    continue
                attributes = getattr(tick, "tickAttribLast", None)
                self.engine.ingest_trade(
                    symbol,
                    timestamp=getattr(tick, "time", None) or time.time(),
                    price=getattr(tick, "price", None),
                    size=getattr(tick, "size", None),
                    exchange=str(getattr(tick, "exchange", "") or ""),
                    special_conditions=str(getattr(tick, "specialConditions", "") or ""),
                    past_limit=bool(getattr(attributes, "pastLimit", False)),
                    unreported=bool(getattr(attributes, "unreported", False)),
                )

        ticker.updateEvent += on_tick_update
        entry["tick_ticker"] = ticker
        entry["tick_type"] = tick_type
        entry["tick_callback"] = on_tick_update
        self.engine.mark_subscription(
            symbol,
            tick_active=True,
            tick_type=tick_type,
            contract=entry["contract_payload"],
        )

    async def stop_symbol(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        entry = self.subscriptions.pop(symbol, None)
        if not entry:
            return {"symbol": symbol, "status": "not_active"}
        quote_ticker = entry.get("quote_ticker")
        quote_callback = entry.get("quote_callback")
        tick_ticker = entry.get("tick_ticker")
        tick_callback = entry.get("tick_callback")
        try:
            if tick_ticker is not None and tick_callback is not None:
                tick_ticker.updateEvent -= tick_callback
        except Exception:
            pass
        try:
            if tick_ticker is not None:
                self.ib.cancelTickByTickData(entry["contract"], entry["tick_type"])
        except Exception as exc:
            entry["last_error"] = str(exc)
        try:
            if quote_ticker is not None and quote_callback is not None:
                quote_ticker.updateEvent -= quote_callback
        except Exception:
            pass
        try:
            self.ib.cancelMktData(entry["contract"])
        except Exception as exc:
            entry["last_error"] = str(exc)
        self.engine.mark_subscription(
            symbol,
            tick_active=False,
            tick_type="",
            contract=entry.get("contract_payload", {}),
            error=entry.get("last_error"),
        )
        return {"symbol": symbol, "status": "stopped"}

    async def stop_all(self) -> Dict[str, Any]:
        results = []
        for symbol in list(self.subscriptions):
            results.append(await self.stop_symbol(symbol))
        return {"status": "stopped", "results": results}

    async def shutdown(self) -> None:
        await self.stop_all()
        if self._error_handler_registered:
            try:
                self.ib.errorEvent -= self._error_handler
            except Exception:
                pass
            self._error_handler_registered = False

    def status(self) -> Dict[str, Any]:
        return {
            "quote_stream_count": len(self.subscriptions),
            "tick_stream_count": sum(1 for item in self.subscriptions.values() if item.get("tick_ticker")),
            "maximum_tick_streams": self.engine.config.max_tick_streams,
            "subscriptions": [
                {
                    "symbol": symbol,
                    "tick_active": bool(entry.get("tick_ticker")),
                    "tick_type": entry.get("tick_type") or None,
                    "contract": entry.get("contract_payload"),
                    "started_at": entry.get("started_at"),
                    "last_error": entry.get("last_error"),
                }
                for symbol, entry in sorted(self.subscriptions.items())
            ],
        }


def _get_manager(app_context: AppContext) -> TickRadarManager:
    runtime = app_context.runtime
    existing = getattr(runtime, "tick_radar_manager", None)
    if isinstance(existing, TickRadarManager):
        return existing
    engine = get_or_create_engine(runtime)
    manager = TickRadarManager(app_context.tws, runtime, engine)
    manager._ensure_error_handler()
    setattr(runtime, "tick_radar_manager", manager)
    return manager


def _clean_symbols(values: Optional[List[str]]) -> List[str]:
    seen = set()
    result = []
    for value in values or []:
        symbol = str(value).upper().strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def register_tick_radar_tools(mcp: FastMCP) -> None:
    """Register tick-radar lifecycle, status, and signal tools."""

    @mcp.tool()
    async def ibkr_plan_tick_radar(
        ctx: Context[ServerSession, AppContext],
        tickSymbols: List[str],
        quoteOnlySymbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Validate a quote/tick universe against IBKR's specialized line budget."""
        runtime = ctx.request_context.lifespan_context.runtime
        engine = get_or_create_engine(runtime)
        tick_symbols = _clean_symbols(tickSymbols)
        quote_symbols = _clean_symbols(quoteOnlySymbols)
        all_quotes = list(dict.fromkeys(tick_symbols + quote_symbols))
        blockers = []
        if len(tick_symbols) > engine.config.max_tick_streams:
            blockers.append(
                f"requested {len(tick_symbols)} tick streams but the configured limit is "
                f"{engine.config.max_tick_streams}"
            )
        if len(all_quotes) > runtime.policy.max_operational_streams:
            blockers.append(
                f"requested {len(all_quotes)} quote streams but the operational limit is "
                f"{runtime.policy.max_operational_streams}"
            )
        return {
            "allowed": not blockers,
            "blockers": blockers,
            "tick_symbols": tick_symbols,
            "quote_only_symbols": [item for item in quote_symbols if item not in tick_symbols],
            "quote_streams": len(all_quotes),
            "tick_streams": len(tick_symbols),
            "limits": {
                "market_data_lines": runtime.policy.market_data_lines,
                "maximum_tick_streams": engine.config.max_tick_streams,
                "maximum_quote_streams": runtime.policy.max_operational_streams,
            },
            "recommended_pattern": (
                "Keep the highest-priority 3-5 symbols on continuous AllLast; use quote-only "
                "monitoring for the rest and promote symbols only after stopping another tick stream."
            ),
        }

    @mcp.tool()
    async def ibkr_start_tick_radar(
        ctx: Context[ServerSession, AppContext],
        tickSymbols: List[str],
        quoteOnlySymbols: Optional[List[str]] = None,
        tickType: str = "AllLast",
        ignoreSize: bool = False,
        replaceExisting: bool = False,
    ) -> Dict[str, Any]:
        """Start Level-1 quotes and real-time stock Time & Sales radar streams."""
        app_context = ctx.request_context.lifespan_context
        if not app_context.tws or not app_context.tws.is_connected():
            return {"error": "TWS client not connected"}
        if tickType not in {"Last", "AllLast"}:
            return {"error": "radar trade tickType must be Last or AllLast"}
        manager = _get_manager(app_context)
        tick_symbols = _clean_symbols(tickSymbols)
        quote_symbols = _clean_symbols(quoteOnlySymbols)
        if len(tick_symbols) > manager.engine.config.max_tick_streams:
            return {
                "error": "tick-by-tick budget exceeded",
                "requested": len(tick_symbols),
                "maximum": manager.engine.config.max_tick_streams,
            }
        if replaceExisting:
            await manager.stop_all()
        results = []
        tick_set = set(tick_symbols)
        for symbol in list(dict.fromkeys(tick_symbols + quote_symbols)):
            try:
                results.append(
                    await manager.start_symbol(
                        symbol,
                        tick_type=tickType,
                        tick_enabled=symbol in tick_set,
                        ignore_size=ignoreSize,
                    )
                )
            except Exception as exc:
                manager.engine.mark_subscription(
                    symbol,
                    tick_active=False,
                    tick_type=tickType if symbol in tick_set else "",
                    error=str(exc),
                )
                results.append({"symbol": symbol, "error": str(exc)})
        return {
            "status": "started",
            "dashboard": "/radar",
            "snapshot_endpoint": "/api/v1/radar/snapshot",
            "results": results,
            "manager": manager.status(),
            "warning": (
                "Tick-by-tick requires LIVE Level-1 permission. The dashboard will keep "
                "trade_eligible false for delayed, stale, halted, or quote-only symbols."
            ),
        }

    @mcp.tool()
    async def ibkr_stop_tick_radar(
        ctx: Context[ServerSession, AppContext],
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Stop selected radar symbols, or every radar stream when omitted."""
        app_context = ctx.request_context.lifespan_context
        manager = _get_manager(app_context)
        selected = _clean_symbols(symbols)
        if not selected:
            return await manager.stop_all()
        return {"status": "stopped", "results": [await manager.stop_symbol(symbol) for symbol in selected]}

    @mcp.tool()
    async def ibkr_get_tick_radar_snapshot(
        ctx: Context[ServerSession, AppContext],
        symbol: str = "",
    ) -> Dict[str, Any]:
        """Read signals, decay projections, calibration, and data-quality state."""
        runtime = ctx.request_context.lifespan_context.runtime
        engine = get_or_create_engine(runtime)
        result = engine.snapshot(symbol=symbol)
        manager = getattr(runtime, "tick_radar_manager", None)
        result["subscription_manager"] = manager.status() if manager else None
        return result

    @mcp.tool()
    async def ibkr_get_tick_radar_alerts(
        ctx: Context[ServerSession, AppContext],
        limit: int = 50,
        minimumAbsoluteScore: float = 55.0,
    ) -> Dict[str, Any]:
        """Return recent threshold-crossing radar alerts without raw tick noise."""
        runtime = ctx.request_context.lifespan_context.runtime
        engine = get_or_create_engine(runtime)
        values = engine.recent_alerts(
            limit=max(1, min(500, int(limit))),
            minimum_score=max(0.0, float(minimumAbsoluteScore)),
        )
        return {"count": len(values), "alerts": values}
