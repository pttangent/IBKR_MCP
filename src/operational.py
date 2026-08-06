"""Operational policy, capability discovery, pacing, and paper-trading guards."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple


MARKET_DATA_TYPE_NAMES = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}
FREE_API_NEWS_PROVIDERS = {"BRFUPDN", "BRFG", "DJNL"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


@dataclass(frozen=True)
class RuntimePolicy:
    agent_mode: str = "read_only"
    allow_live_trading: bool = False
    enable_legacy_order_tools: bool = False
    require_order_confirmation: bool = True
    expected_paper_account: str = ""
    market_data_lines: int = 100
    reserved_market_data_lines: int = 20
    max_operational_streams: int = 40
    max_order_notional_usd: float = 5_000.0
    max_orders_per_day: int = 20
    live_stale_after_seconds: float = 15.0
    delayed_stale_after_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "RuntimePolicy":
        mode = os.getenv("IBKR_AGENT_MODE", "read_only").strip().lower()
        if mode not in {"read_only", "paper", "live"}:
            mode = "read_only"

        lines = _env_int("IBKR_MARKET_DATA_LINES", 100, 1)
        reserved = _env_int("IBKR_RESERVED_MARKET_DATA_LINES", 20, 0)
        available = max(1, lines - reserved)
        streams = min(
            available,
            _env_int("IBKR_MAX_OPERATIONAL_STREAMS", min(40, available), 1),
        )
        return cls(
            agent_mode=mode,
            allow_live_trading=_env_bool("IBKR_ALLOW_LIVE_TRADING", False),
            enable_legacy_order_tools=_env_bool("IBKR_ENABLE_LEGACY_ORDER_TOOLS", False),
            require_order_confirmation=_env_bool("IBKR_REQUIRE_ORDER_CONFIRMATION", True),
            expected_paper_account=os.getenv("TWS_PAPER_ACCOUNT", "").strip(),
            market_data_lines=lines,
            reserved_market_data_lines=reserved,
            max_operational_streams=streams,
            max_order_notional_usd=_env_float("IBKR_MAX_ORDER_NOTIONAL_USD", 5_000.0),
            max_orders_per_day=_env_int("IBKR_MAX_ORDERS_PER_DAY", 20, 0),
            live_stale_after_seconds=_env_float("IBKR_LIVE_STALE_AFTER_SECONDS", 15.0),
            delayed_stale_after_seconds=_env_float("IBKR_DELAYED_STALE_AFTER_SECONDS", 120.0),
        )

    @property
    def order_tools_enabled(self) -> bool:
        if self.agent_mode == "paper":
            return True
        return self.agent_mode == "live" and self.allow_live_trading

    def as_dict(self) -> Dict[str, Any]:
        return {
            "agent_mode": self.agent_mode,
            "order_tools_enabled": self.order_tools_enabled,
            "allow_live_trading": self.allow_live_trading,
            "enable_legacy_order_tools": self.enable_legacy_order_tools,
            "require_order_confirmation": self.require_order_confirmation,
            "expected_paper_account": self.expected_paper_account or None,
            "market_data_lines": self.market_data_lines,
            "reserved_market_data_lines": self.reserved_market_data_lines,
            "max_operational_streams": self.max_operational_streams,
            "max_order_notional_usd": self.max_order_notional_usd,
            "max_orders_per_day": self.max_orders_per_day,
        }


@dataclass
class PacingDecision:
    allowed: bool
    retry_after_seconds: float = 0.0
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "retry_after_seconds": round(max(0.0, self.retry_after_seconds), 3),
            "reason": self.reason,
        }


class RequestGovernor:
    """Conservative in-process TWS pacing guard."""

    def __init__(self, market_data_lines: int = 100) -> None:
        self.general_per_second = max(1, market_data_lines // 2)
        self._general: Deque[float] = deque()
        self._historical_all: Deque[float] = deque()
        self._historical_by_contract: Dict[str, Deque[float]] = defaultdict(deque)
        self._historical_identical: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _trim(values: Deque[float], now: float, window: float) -> None:
        while values and now - values[0] >= window:
            values.popleft()

    async def check_general(self) -> PacingDecision:
        async with self._lock:
            now = time.monotonic()
            self._trim(self._general, now, 1.0)
            if len(self._general) >= self.general_per_second:
                retry = 1.0 - (now - self._general[0])
                return PacingDecision(False, retry, "general request rate")
            self._general.append(now)
            return PacingDecision(True)

    async def check_historical(
        self,
        request_key: str,
        contract_key: str,
        bid_ask: bool = False,
    ) -> PacingDecision:
        async with self._lock:
            now = time.monotonic()
            weight = 2 if bid_ask else 1
            last = self._historical_identical.get(request_key)
            if last is not None and now - last < 15.0:
                return PacingDecision(
                    False,
                    15.0 - (now - last),
                    "identical historical request within 15 seconds",
                )

            self._trim(self._historical_all, now, 600.0)
            if len(self._historical_all) + weight > 60:
                return PacingDecision(
                    False,
                    600.0 - (now - self._historical_all[0]),
                    "more than 60 historical requests in 10 minutes",
                )

            per_contract = self._historical_by_contract[contract_key]
            self._trim(per_contract, now, 2.0)
            if len(per_contract) + weight >= 6:
                return PacingDecision(
                    False,
                    2.0 - (now - per_contract[0]),
                    "six or more historical requests for one contract in two seconds",
                )

            for _ in range(weight):
                self._historical_all.append(now)
                per_contract.append(now)
            self._historical_identical[request_key] = now
            return PacingDecision(True)

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        self._trim(self._general, now, 1.0)
        self._trim(self._historical_all, now, 600.0)
        return {
            "general_per_second": self.general_per_second,
            "general_used_last_second": len(self._general),
            "historical_used_last_10_minutes": len(self._historical_all),
        }


@dataclass
class RuntimeState:
    policy: RuntimePolicy = field(default_factory=RuntimePolicy.from_env)
    governor: RequestGovernor = field(init=False)
    operational_streams: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    capability_cache: Dict[str, Any] = field(default_factory=dict)
    recent_errors: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    order_count_date: str = ""
    order_count: int = 0

    def __post_init__(self) -> None:
        self.governor = RequestGovernor(self.policy.market_data_lines)

    def detect_account_mode(self, accounts: Iterable[str]) -> Tuple[str, str]:
        values = [str(account) for account in accounts if account]
        if not values:
            return "unknown", "no managed accounts returned"
        if self.policy.expected_paper_account and self.policy.expected_paper_account in values:
            return "paper", "matched TWS_PAPER_ACCOUNT"
        if all(value.upper().startswith("DU") for value in values):
            return "paper", "all account IDs use the common DU paper prefix"
        if any(value.upper().startswith("DU") for value in values):
            return "mixed", "paper-like and non-paper account IDs are both visible"
        return "live_or_unknown", "no paper account match; confirm the TWS login manually"

    def validate_order_intent(
        self,
        account: str,
        quantity: float,
        estimated_price: float,
        confirmation_token: str = "",
    ) -> Dict[str, Any]:
        notional = abs(quantity * estimated_price)
        account_mode, account_reason = self.detect_account_mode([account])
        blockers: List[str] = []
        if not self.policy.order_tools_enabled:
            blockers.append("order tools are disabled by IBKR_AGENT_MODE")
        if self.policy.agent_mode == "paper" and account_mode != "paper":
            blockers.append("paper mode requires a positively identified paper account")
        if self.policy.agent_mode == "live" and not self.policy.allow_live_trading:
            blockers.append("live trading requires IBKR_ALLOW_LIVE_TRADING=true")
        if notional > self.policy.max_order_notional_usd:
            blockers.append("estimated order notional exceeds IBKR_MAX_ORDER_NOTIONAL_USD")
        if self.policy.require_order_confirmation and not confirmation_token.strip():
            blockers.append("a non-empty confirmation token is required")

        today = datetime.now(timezone.utc).date().isoformat()
        count = self.order_count if self.order_count_date == today else 0
        if count >= self.policy.max_orders_per_day:
            blockers.append("daily order-count limit reached")
        return {
            "allowed": not blockers,
            "blockers": blockers,
            "account_mode": account_mode,
            "account_mode_reason": account_reason,
            "estimated_notional_usd": notional,
            "policy": self.policy.as_dict(),
        }

    def status(self) -> Dict[str, Any]:
        return {
            "policy": self.policy.as_dict(),
            "active_operational_streams": len(self.operational_streams),
            "stream_ids": sorted(self.operational_streams),
            "pacing": self.governor.snapshot(),
            "capability_cache": self.capability_cache,
            "recent_errors": list(self.recent_errors)[-10:],
        }


def api_limits_payload(policy: RuntimePolicy) -> Dict[str, Any]:
    return {
        "scope": "TWS socket API used by this repository",
        "as_of": "2026-08-06",
        "market_data": {
            "default_lines": 100,
            "configured_lines": policy.market_data_lines,
            "shared_with_tws_watchlists": True,
            "tick_by_tick_at_100_lines": 5,
            "market_depth_at_100_lines": 3,
            "live_data_requires_subscription_for_most_securities": True,
            "forex_and_crypto_no_additional_market_data_subscription": True,
        },
        "request_pacing": {
            "general_requests_per_second_formula": "maximum market data lines / 2",
            "configured_general_requests_per_second": max(1, policy.market_data_lines // 2),
            "historical_identical_request_cooldown_seconds": 15,
            "historical_same_contract_limit": "fewer than 6 requests in 2 seconds",
            "historical_global_limit": "no more than 60 requests in 10 minutes",
            "historical_bid_ask_weight": 2,
        },
        "scanner": {
            "max_rows_per_scan": 50,
            "max_concurrent_scans": 10,
            "subscription_required_for_ranking": False,
            "prices_included_without_separate_quote_request": False,
        },
        "delayed_data": {
            "typical_delay_minutes": "15-20",
            "supported": ["reqMktData", "reqHistoricalData"],
            "not_supported": ["tick-by-tick", "market depth", "real-time bars"],
            "trade_eligible": False,
        },
        "paper_without_extra_subscriptions": {
            "account_positions_orders_pnl": "available",
            "stock_and_option_live_quotes": "not available unless owned or shared",
            "delayed_watchlist_and_historical_data": "often available where IBKR offers delayed data",
            "tick_by_tick": "requires live Level 1 permission",
            "option_greeks": "require both underlying and option permissions",
            "free_api_news": sorted(FREE_API_NEWS_PROVIDERS),
            "paper_fills": "top-of-book simulation; no queue or deep-liquidity realism",
        },
        "important_error_codes": {
            "10089": "additional API market-data subscription required",
            "10167": "live data missing; delayed data displayed",
            "10186": "delayed data is not enabled",
            "10187": "historical ticks unavailable due to permissions",
            "10197": "competing live/paper session for shared data",
            "1100": "connectivity lost",
            "1101": "connectivity restored; resubscribe market data",
            "1102": "connectivity restored; data maintained",
        },
    }


def _iso_utc(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _has_price(ticker: Any) -> bool:
    for value in (
        getattr(ticker, "last", None),
        getattr(ticker, "bid", None),
        getattr(ticker, "ask", None),
        getattr(ticker, "close", None),
    ):
        if value is not None and value == value and value != -1:
            return True
    return False


async def probe_capabilities(
    tws: Any,
    runtime: RuntimeState,
    symbol: str = "SPY",
    timeout_seconds: float = 4.0,
    include_historical: bool = True,
) -> Dict[str, Any]:
    if not tws or not tws.is_connected():
        return {"connected": False, "error": "TWS client not connected"}

    ib = tws.ib
    accounts = list(ib.managedAccounts())
    account_mode, account_reason = runtime.detect_account_mode(accounts)

    server_time = None
    try:
        server_time = await ib.reqCurrentTimeAsync()
    except Exception as exc:
        runtime.recent_errors.append({"stage": "server_time", "error": str(exc)})

    providers: List[Dict[str, str]] = []
    try:
        raw = await ib.reqNewsProvidersAsync()
        providers = [{"code": str(item.code), "name": str(item.name)} for item in raw]
    except Exception as exc:
        runtime.recent_errors.append({"stage": "news_providers", "error": str(exc)})

    market_data: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "available": False,
        "market_data_type": "NONE",
        "trade_eligible": False,
    }
    contract = None
    ticker = None
    try:
        from ib_async import Stock

        qualified = await ib.qualifyContractsAsync(Stock(symbol.upper(), "SMART", "USD"))
        if not qualified:
            raise RuntimeError(f"could not qualify {symbol}")
        contract = qualified[0]
        decision = await runtime.governor.check_general()
        if not decision.allowed:
            market_data["pacing"] = decision.as_dict()
        else:
            ib.reqMarketDataType(3)
            ticker = ib.reqMktData(contract, "", False, False)
            deadline = asyncio.get_running_loop().time() + max(0.5, timeout_seconds)
            while asyncio.get_running_loop().time() < deadline:
                if getattr(ticker, "marketDataType", None) or _has_price(ticker):
                    break
                await asyncio.sleep(0.1)
            md_id = int(getattr(ticker, "marketDataType", 0) or 0)
            md_name = MARKET_DATA_TYPE_NAMES.get(md_id, "UNKNOWN")
            prices = {
                "last": getattr(ticker, "last", None),
                "bid": getattr(ticker, "bid", None),
                "ask": getattr(ticker, "ask", None),
                "close": getattr(ticker, "close", None),
            }
            available = _has_price(ticker)
            market_data.update(
                {
                    "available": available,
                    "market_data_type_id": md_id or None,
                    "market_data_type": md_name,
                    "trade_eligible": available and md_name == "LIVE",
                    "source_timestamp_utc": _iso_utc(getattr(ticker, "time", None)),
                    "prices": prices,
                }
            )
    except Exception as exc:
        market_data["error"] = str(exc)
        runtime.recent_errors.append({"stage": "market_data_probe", "error": str(exc)})
    finally:
        if ticker is not None and contract is not None:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass

    historical: Dict[str, Any] = {"tested": include_historical, "available": False}
    if include_historical and contract is not None:
        con_id = getattr(contract, "conId", symbol)
        decision = await runtime.governor.check_historical(
            f"{con_id}|1 D|1 day|TRADES|1",
            f"{con_id}|SMART|TRADES",
        )
        if not decision.allowed:
            historical["pacing"] = decision.as_dict()
        else:
            try:
                bars = await ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
                historical.update({"available": bool(bars), "bar_count": len(bars)})
            except Exception as exc:
                historical["error"] = str(exc)
                runtime.recent_errors.append({"stage": "historical_probe", "error": str(exc)})

    provider_codes = {item["code"] for item in providers}
    if market_data.get("trade_eligible"):
        profile = "LIVE_FULL"
    elif market_data.get("available"):
        profile = "DELAYED_ONLY"
    else:
        profile = "ACCOUNT_ONLY"

    result = {
        "connected": True,
        "server_time_utc": _iso_utc(server_time),
        "accounts": accounts,
        "account_mode": account_mode,
        "account_mode_reason": account_reason,
        "market_data": market_data,
        "historical_data": historical,
        "news": {
            "providers": providers,
            "free_provider_codes_available": sorted(provider_codes & FREE_API_NEWS_PROVIDERS),
            "note": "Enable providers in TWS/IB Gateway API News Configuration.",
        },
        "scanner": {
            "contract_ranking_expected_available": True,
            "prices_require_separate_market_data": True,
        },
        "tick_by_tick": {
            "status": "not_probed",
            "requires_live_level_1": True,
            "default_capacity_at_100_market_data_lines": 5,
        },
        "operating_profile": profile,
        "trade_actions_allowed": profile == "LIVE_FULL" and runtime.policy.order_tools_enabled,
        "policy": runtime.policy.as_dict(),
        "limits": api_limits_payload(runtime.policy),
    }
    runtime.capability_cache = result
    return result


def plan_watchlist(
    requested_symbols: Iterable[str],
    policy: RuntimePolicy,
    active_streams: int = 0,
    benchmarks: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    normalized: List[str] = []
    for symbol in [*(benchmarks or ("SPY", "QQQ")), *requested_symbols]:
        item = str(symbol).strip().upper()
        if item and item not in normalized:
            normalized.append(item)
    remaining = max(0, policy.max_operational_streams - active_streams)
    return {
        "accepted": normalized[:remaining],
        "rejected": normalized[remaining:],
        "requested_unique": len(normalized),
        "active_streams": active_streams,
        "remaining_capacity_before_plan": remaining,
        "max_operational_streams": policy.max_operational_streams,
        "reserved_market_data_lines": policy.reserved_market_data_lines,
        "note": "Conservative budget; TWS watchlists and other API clients are not observable here.",
    }
