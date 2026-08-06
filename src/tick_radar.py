"""Deterministic tick-by-tick radar engine with signal decay and online calibration.

The engine is deliberately broker-agnostic. IBKR subscription plumbing lives in
``src.tools.tick_radar``; this module only consumes normalized quote/trade events.
"""

from __future__ import annotations

import math
import os
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple


LN2 = math.log(2.0)
SIGNAL_HALF_LIVES_SECONDS: Mapping[str, float] = {
    "large_trade": 75.0,
    "volume_burst": 180.0,
    "trade_intensity": 120.0,
    "signed_flow": 90.0,
    "price_impulse": 120.0,
    "quote_pressure": 30.0,
    "composite": 120.0,
    "activity": 150.0,
}
CALIBRATION_HORIZONS_SECONDS = (60, 180, 300)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if _finite(value) else default


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _utc_iso(epoch_seconds: Optional[float]) -> Optional[str]:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _to_epoch(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if _finite(value):
        return float(value)
    return time.time()


def _median(values: Iterable[float]) -> float:
    values = list(values)
    return float(statistics.median(values)) if values else 0.0


def _robust_z(value: float, history: Iterable[float]) -> float:
    samples = [float(item) for item in history if _finite(item)]
    if len(samples) < 20:
        return 0.0
    transformed = [math.log1p(max(0.0, item)) for item in samples]
    center = _median(transformed)
    mad = _median(abs(item - center) for item in transformed)
    scale = max(1e-9, 1.4826 * mad)
    return (math.log1p(max(0.0, value)) - center) / scale


def _score_from_ratio(ratio: float, neutral: float = 1.0, sensitivity: float = 1.6) -> float:
    excess = max(0.0, ratio - neutral)
    return 100.0 * (1.0 - math.exp(-excess / max(1e-6, sensitivity)))


def _score_from_z(z_score: float, threshold: float = 2.5) -> float:
    excess = max(0.0, z_score - threshold)
    return 100.0 * (1.0 - math.exp(-excess / 1.5))


@dataclass(frozen=True)
class RadarConfig:
    max_tick_streams: int = 5
    retention_seconds: int = 900
    baseline_seconds: int = 120
    recent_volume_seconds: int = 5
    flow_seconds: int = 15
    price_impulse_seconds: int = 30
    min_baseline_seconds: int = 30
    min_large_trade_notional_usd: float = 100_000.0
    large_trade_z_threshold: float = 3.5
    alert_score_threshold: float = 55.0
    alert_rearm_score: float = 35.0
    alert_cooldown_seconds: float = 30.0
    decay_floor_score: float = 20.0
    include_unreported: bool = False
    min_calibration_samples: int = 20
    stale_after_seconds: float = 10.0

    @classmethod
    def from_env(cls, market_data_lines: int = 100) -> "RadarConfig":
        derived_limit = max(1, int(max(100, market_data_lines) * 0.05))
        return cls(
            max_tick_streams=min(
                derived_limit,
                _env_int("IBKR_TICK_RADAR_MAX_STREAMS", derived_limit, 1),
            ),
            retention_seconds=_env_int("IBKR_TICK_RADAR_RETENTION_SECONDS", 900, 120),
            baseline_seconds=_env_int("IBKR_TICK_RADAR_BASELINE_SECONDS", 120, 30),
            recent_volume_seconds=_env_int("IBKR_TICK_RADAR_RECENT_SECONDS", 5, 1),
            flow_seconds=_env_int("IBKR_TICK_RADAR_FLOW_SECONDS", 15, 3),
            price_impulse_seconds=_env_int("IBKR_TICK_RADAR_PRICE_SECONDS", 30, 5),
            min_baseline_seconds=_env_int("IBKR_TICK_RADAR_MIN_BASELINE_SECONDS", 30, 10),
            min_large_trade_notional_usd=_env_float(
                "IBKR_TICK_RADAR_LARGE_NOTIONAL_USD", 100_000.0, 0.0
            ),
            large_trade_z_threshold=_env_float("IBKR_TICK_RADAR_LARGE_Z", 3.5, 0.5),
            alert_score_threshold=_env_float("IBKR_TICK_RADAR_ALERT_SCORE", 55.0, 1.0),
            alert_rearm_score=_env_float("IBKR_TICK_RADAR_REARM_SCORE", 35.0, 0.0),
            alert_cooldown_seconds=_env_float(
                "IBKR_TICK_RADAR_ALERT_COOLDOWN_SECONDS", 30.0, 0.0
            ),
            decay_floor_score=_env_float("IBKR_TICK_RADAR_DECAY_FLOOR", 20.0, 1.0),
            include_unreported=_env_bool("IBKR_TICK_RADAR_INCLUDE_UNREPORTED", False),
            min_calibration_samples=_env_int(
                "IBKR_TICK_RADAR_MIN_CALIBRATION_SAMPLES", 20, 1
            ),
            stale_after_seconds=_env_float(
                "IBKR_TICK_RADAR_STALE_AFTER_SECONDS", 10.0, 1.0
            ),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_tick_streams": self.max_tick_streams,
            "retention_seconds": self.retention_seconds,
            "baseline_seconds": self.baseline_seconds,
            "recent_volume_seconds": self.recent_volume_seconds,
            "flow_seconds": self.flow_seconds,
            "price_impulse_seconds": self.price_impulse_seconds,
            "min_baseline_seconds": self.min_baseline_seconds,
            "min_large_trade_notional_usd": self.min_large_trade_notional_usd,
            "large_trade_z_threshold": self.large_trade_z_threshold,
            "alert_score_threshold": self.alert_score_threshold,
            "alert_rearm_score": self.alert_rearm_score,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
            "decay_floor_score": self.decay_floor_score,
            "include_unreported": self.include_unreported,
            "min_calibration_samples": self.min_calibration_samples,
            "stale_after_seconds": self.stale_after_seconds,
        }


@dataclass
class QuoteState:
    timestamp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    market_data_type: str = "UNKNOWN"

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return 0.0


@dataclass
class SecondBucket:
    second: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    trade_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    unknown_volume: float = 0.0
    notional: float = 0.0
    max_trade_size: float = 0.0
    max_trade_notional: float = 0.0

    def add(self, price: float, size: float, side: int) -> None:
        if self.trade_count == 0:
            self.open = self.high = self.low = self.close = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
        self.trade_count += 1
        self.volume += size
        self.notional += price * size
        self.max_trade_size = max(self.max_trade_size, size)
        self.max_trade_notional = max(self.max_trade_notional, price * size)
        if side > 0:
            self.buy_volume += size
        elif side < 0:
            self.sell_volume += size
        else:
            self.unknown_volume += size


@dataclass
class LargeTradeImpulse:
    timestamp: float
    signed_score: float
    size: float
    notional: float
    exchange: str
    special_conditions: str


@dataclass
class CalibrationStat:
    count: int = 0
    hit_count: int = 0
    sum_signed_return_bps: float = 0.0
    ewma_signed_return_bps: float = 0.0
    ewma_abs_return_bps: float = 0.0
    updated_at: float = 0.0

    def update(self, signed_return_bps: float, alpha: float = 0.08) -> None:
        self.count += 1
        self.hit_count += int(signed_return_bps > 0)
        self.sum_signed_return_bps += signed_return_bps
        if self.count == 1:
            self.ewma_signed_return_bps = signed_return_bps
            self.ewma_abs_return_bps = abs(signed_return_bps)
        else:
            self.ewma_signed_return_bps = alpha * signed_return_bps + (1.0 - alpha) * self.ewma_signed_return_bps
            self.ewma_abs_return_bps = alpha * abs(signed_return_bps) + (1.0 - alpha) * self.ewma_abs_return_bps
        self.updated_at = time.time()

    def payload(self, minimum_samples: int) -> Dict[str, Any]:
        calibrated = self.count >= minimum_samples
        return {
            "sample_count": self.count,
            "calibrated": calibrated,
            "hit_rate": self.hit_count / self.count if self.count else None,
            "mean_signed_return_bps": self.sum_signed_return_bps / self.count if self.count else None,
            "ewma_signed_return_bps": self.ewma_signed_return_bps if self.count else None,
            "ewma_abs_return_bps": self.ewma_abs_return_bps if self.count else None,
            "updated_at_utc": _utc_iso(self.updated_at) if self.updated_at else None,
        }


@dataclass
class PendingOutcome:
    symbol: str
    signal_kind: str
    direction: int
    emitted_at: float
    start_price: float
    pending_horizons: set[int] = field(default_factory=lambda: set(CALIBRATION_HORIZONS_SECONDS))


@dataclass
class SymbolRadarState:
    symbol: str
    config: RadarConfig
    quote: QuoteState = field(default_factory=QuoteState)
    buckets: Deque[SecondBucket] = field(default_factory=deque)
    trade_sizes: Deque[Tuple[float, float]] = field(default_factory=deque)
    price_points: Deque[Tuple[float, float]] = field(default_factory=deque)
    large_trade_impulses: Deque[LargeTradeImpulse] = field(default_factory=deque)
    signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_trade_timestamp: float = 0.0
    last_trade_price: float = 0.0
    previous_trade_price: float = 0.0
    previous_side: int = 0
    total_ticks: int = 0
    signal_ticks: int = 0
    excluded_unreported_ticks: int = 0
    halted: bool = False
    tick_active: bool = False
    tick_type: str = ""
    contract: Dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None
    alert_armed: Dict[str, bool] = field(default_factory=lambda: defaultdict(lambda: True))
    last_alert_at: Dict[str, float] = field(default_factory=dict)

    def ingest_quote(self, timestamp: Any, bid: Any, ask: Any, bid_size: Any, ask_size: Any, market_data_type: str = "UNKNOWN") -> None:
        ts = _to_epoch(timestamp)
        self.quote = QuoteState(
            timestamp=ts,
            bid=max(0.0, _number(bid)),
            ask=max(0.0, _number(ask)),
            bid_size=max(0.0, _number(bid_size)),
            ask_size=max(0.0, _number(ask_size)),
            market_data_type=str(market_data_type or "UNKNOWN").upper(),
        )

    def _classify_side(self, price: float) -> int:
        quote = self.quote
        epsilon = max(1e-8, price * 1e-7)
        if quote.ask > 0 and price >= quote.ask - epsilon:
            return 1
        if quote.bid > 0 and price <= quote.bid + epsilon:
            return -1
        mid = quote.mid
        if mid > 0:
            if price > mid + epsilon:
                return 1
            if price < mid - epsilon:
                return -1
        if self.last_trade_price > 0:
            if price > self.last_trade_price:
                return 1
            if price < self.last_trade_price:
                return -1
        return self.previous_side

    def _trim(self, now: float) -> None:
        cutoff = now - self.config.retention_seconds
        while self.buckets and self.buckets[0].second < int(cutoff):
            self.buckets.popleft()
        while self.trade_sizes and self.trade_sizes[0][0] < cutoff:
            self.trade_sizes.popleft()
        while self.price_points and self.price_points[0][0] < cutoff:
            self.price_points.popleft()
        longest_half_life = max(SIGNAL_HALF_LIVES_SECONDS.values())
        impulse_cutoff = now - max(self.config.retention_seconds, 8 * longest_half_life)
        while self.large_trade_impulses and self.large_trade_impulses[0].timestamp < impulse_cutoff:
            self.large_trade_impulses.popleft()

    def ingest_trade(self, timestamp: Any, price: Any, size: Any, exchange: str = "", special_conditions: str = "", past_limit: bool = False, unreported: bool = False) -> None:
        ts = _to_epoch(timestamp)
        px = _number(price)
        qty = max(0.0, _number(size))
        self.total_ticks += 1
        if px == 0 and qty == 0:
            if past_limit:
                self.halted = True
            elif self.halted:
                self.halted = False
            return
        if px <= 0 or qty <= 0:
            return
        if unreported and not self.config.include_unreported:
            self.excluded_unreported_ticks += 1
            return
        history_sizes = [item[1] for item in list(self.trade_sizes)[-500:]]
        size_z = _robust_z(qty, history_sizes)
        notional = px * qty
        z_score = _score_from_z(size_z, self.config.large_trade_z_threshold)
        notional_ratio = notional / max(1.0, self.config.min_large_trade_notional_usd)
        notional_score = _score_from_ratio(notional_ratio, neutral=1.0, sensitivity=2.0)
        large_score = max(z_score, notional_score)
        side = self._classify_side(px)
        self.previous_trade_price = self.last_trade_price
        self.last_trade_price = px
        self.last_trade_timestamp = ts
        if side:
            self.previous_side = side
        second = int(ts)
        if not self.buckets or self.buckets[-1].second != second:
            self.buckets.append(SecondBucket(second=second, open=px, high=px, low=px, close=px))
        self.buckets[-1].add(px, qty, side)
        self.trade_sizes.append((ts, qty))
        self.price_points.append((ts, px))
        self.signal_ticks += 1
        if large_score >= 10.0:
            self.large_trade_impulses.append(
                LargeTradeImpulse(
                    timestamp=ts,
                    signed_score=large_score * side,
                    size=qty,
                    notional=notional,
                    exchange=exchange,
                    special_conditions=special_conditions,
                )
            )
        self._trim(ts)

    def _window(self, now: float, seconds: int, end_offset: int = 0) -> List[SecondBucket]:
        end = now - end_offset
        start = end - seconds
        return [bucket for bucket in self.buckets if start < bucket.second <= end]

    @staticmethod
    def _sum_window(buckets: Iterable[SecondBucket]) -> Dict[str, float]:
        result = {"volume": 0.0, "trade_count": 0.0, "buy_volume": 0.0, "sell_volume": 0.0, "unknown_volume": 0.0, "notional": 0.0}
        for bucket in buckets:
            result["volume"] += bucket.volume
            result["trade_count"] += bucket.trade_count
            result["buy_volume"] += bucket.buy_volume
            result["sell_volume"] += bucket.sell_volume
            result["unknown_volume"] += bucket.unknown_volume
            result["notional"] += bucket.notional
        return result

    def _large_trade_signal(self, now: float) -> Tuple[float, Dict[str, Any]]:
        half_life = SIGNAL_HALF_LIVES_SECONDS["large_trade"]
        signed_total = 0.0
        strongest: Optional[LargeTradeImpulse] = None
        strongest_decayed = 0.0
        for impulse in self.large_trade_impulses:
            age = max(0.0, now - impulse.timestamp)
            decayed = impulse.signed_score * math.exp(-LN2 * age / half_life)
            signed_total += decayed
            if abs(decayed) > abs(strongest_decayed):
                strongest = impulse
                strongest_decayed = decayed
        score = _clip(signed_total, -100.0, 100.0)
        raw = {
            "strongest_trade_size": strongest.size if strongest else None,
            "strongest_trade_notional_usd": strongest.notional if strongest else None,
            "strongest_exchange": strongest.exchange if strongest else None,
            "strongest_special_conditions": strongest.special_conditions if strongest else None,
            "active_impulses": len(self.large_trade_impulses),
        }
        return score, raw

    def _price_before(self, target: float) -> Optional[float]:
        selected = None
        for ts, price in self.price_points:
            if ts <= target:
                selected = price
            else:
                break
        return selected

    def _realized_one_second_sigma(self, now: float, seconds: int = 300) -> float:
        points = [(ts, px) for ts, px in self.price_points if ts >= now - seconds]
        if len(points) < 10:
            return 0.0
        closes: Dict[int, float] = {}
        for ts, price in points:
            closes[int(ts)] = price
        ordered = [closes[key] for key in sorted(closes)]
        returns = [math.log(current / previous) for previous, current in zip(ordered, ordered[1:]) if previous > 0 and current > 0]
        return statistics.pstdev(returns) if len(returns) >= 5 else 0.0

    def _signal_payload(self, kind: str, score: float, raw_value: Any, confidence: float, now: float, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        half_life = SIGNAL_HALF_LIVES_SECONDS[kind]
        absolute = abs(score)
        if absolute > self.config.decay_floor_score:
            horizon_seconds = half_life * math.log(absolute / self.config.decay_floor_score, 2)
        else:
            horizon_seconds = 0.0
        projections = {f"{seconds // 60}m": score * math.exp(-LN2 * seconds / half_life) for seconds in CALIBRATION_HORIZONS_SECONDS}
        payload = {
            "kind": kind,
            "score": round(_clip(score, -100.0, 100.0), 3),
            "direction": _sign(score),
            "raw_value": raw_value,
            "confidence": round(_clip(confidence, 0.0, 1.0), 3),
            "half_life_seconds": half_life,
            "effective_horizon_minutes": round(horizon_seconds / 60.0, 3),
            "projected_score_if_unconfirmed": {key: round(value, 3) for key, value in projections.items()},
            "updated_at_utc": _utc_iso(now),
        }
        if metadata:
            payload["metadata"] = metadata
        return payload

    def evaluate(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        now = float(now if now is not None else time.time())
        self._trim(now)
        recent_seconds = self.config.recent_volume_seconds
        recent = self._sum_window(self._window(now, recent_seconds))
        baseline = self._sum_window(self._window(now, self.config.baseline_seconds, end_offset=recent_seconds))
        baseline_span = min(self.config.baseline_seconds, max(0.0, now - (self.buckets[0].second if self.buckets else now)))
        sample_confidence = _clip(baseline_span / max(1.0, self.config.baseline_seconds), 0.0, 1.0)
        if baseline_span < self.config.min_baseline_seconds:
            sample_confidence *= baseline_span / max(1.0, self.config.min_baseline_seconds)
        baseline_volume_rate = baseline["volume"] / max(1.0, baseline_span)
        recent_volume_rate = recent["volume"] / max(1.0, recent_seconds)
        volume_ratio = recent_volume_rate / max(1e-9, baseline_volume_rate)
        volume_score_abs = _score_from_ratio(volume_ratio, neutral=1.0, sensitivity=1.8)
        baseline_trade_rate = baseline["trade_count"] / max(1.0, baseline_span)
        recent_trade_rate = recent["trade_count"] / max(1.0, recent_seconds)
        trade_rate_ratio = recent_trade_rate / max(1e-9, baseline_trade_rate)
        intensity_score_abs = _score_from_ratio(trade_rate_ratio, neutral=1.0, sensitivity=1.8)
        flow = self._sum_window(self._window(now, self.config.flow_seconds))
        known_flow = flow["buy_volume"] + flow["sell_volume"]
        total_flow = known_flow + flow["unknown_volume"]
        flow_imbalance = (flow["buy_volume"] - flow["sell_volume"]) / known_flow if known_flow > 0 else 0.0
        known_fraction = known_flow / total_flow if total_flow > 0 else 0.0
        flow_score = 100.0 * math.tanh(2.0 * flow_imbalance)
        flow_confidence = sample_confidence * known_fraction * min(1.0, total_flow / max(1.0, baseline_volume_rate * self.config.flow_seconds))
        flow_score *= flow_confidence
        directional_anchor = _sign(flow_score)
        prior_price = self._price_before(now - self.config.price_impulse_seconds)
        price_return_bps = 0.0
        price_z = 0.0
        if prior_price and self.last_trade_price > 0:
            price_return_bps = (self.last_trade_price / prior_price - 1.0) * 10_000.0
            sigma = self._realized_one_second_sigma(now)
            expected_sigma = sigma * math.sqrt(self.config.price_impulse_seconds)
            if expected_sigma > 1e-9:
                price_z = math.log(self.last_trade_price / prior_price) / expected_sigma
            else:
                price_z = price_return_bps / 10.0
        price_score = 100.0 * math.tanh(price_z / 2.0) * sample_confidence
        if directional_anchor == 0:
            directional_anchor = _sign(price_score)
        large_score, large_raw = self._large_trade_signal(now)
        if directional_anchor == 0:
            directional_anchor = _sign(large_score)
        quote_denominator = self.quote.bid_size + self.quote.ask_size
        quote_imbalance = (self.quote.bid_size - self.quote.ask_size) / quote_denominator if quote_denominator > 0 else 0.0
        quote_age = now - self.quote.timestamp if self.quote.timestamp else float("inf")
        quote_confidence = 1.0 if quote_age <= self.config.stale_after_seconds else 0.0
        quote_score = 100.0 * math.tanh(1.5 * quote_imbalance) * quote_confidence
        volume_score = volume_score_abs * directional_anchor
        intensity_score = intensity_score_abs * directional_anchor
        activity_score = max(volume_score_abs, intensity_score_abs, abs(large_score))
        components = {
            "large_trade": (large_score, 0.20),
            "volume_burst": (volume_score, 0.20),
            "trade_intensity": (intensity_score, 0.10),
            "signed_flow": (flow_score, 0.25),
            "price_impulse": (price_score, 0.20),
            "quote_pressure": (quote_score, 0.05),
        }
        composite_score = sum(score * weight for score, weight in components.values())
        composite_confidence = _clip(0.45 * sample_confidence + 0.35 * flow_confidence + 0.20 * quote_confidence, 0.0, 1.0)
        self.signals = {
            "large_trade": self._signal_payload("large_trade", large_score, large_raw, sample_confidence, now),
            "volume_burst": self._signal_payload("volume_burst", volume_score, round(volume_ratio, 4), sample_confidence, now, {"recent_volume_per_second": recent_volume_rate, "baseline_volume_per_second": baseline_volume_rate, "recent_window_seconds": recent_seconds}),
            "trade_intensity": self._signal_payload("trade_intensity", intensity_score, round(trade_rate_ratio, 4), sample_confidence, now, {"recent_trades_per_second": recent_trade_rate, "baseline_trades_per_second": baseline_trade_rate}),
            "signed_flow": self._signal_payload("signed_flow", flow_score, round(flow_imbalance, 5), flow_confidence, now, {"buy_volume": flow["buy_volume"], "sell_volume": flow["sell_volume"], "unknown_volume": flow["unknown_volume"], "known_fraction": known_fraction, "window_seconds": self.config.flow_seconds}),
            "price_impulse": self._signal_payload("price_impulse", price_score, round(price_return_bps, 4), sample_confidence, now, {"return_bps": price_return_bps, "volatility_adjusted_z": price_z, "window_seconds": self.config.price_impulse_seconds}),
            "quote_pressure": self._signal_payload("quote_pressure", quote_score, round(quote_imbalance, 5), quote_confidence, now, {"bid_size": self.quote.bid_size, "ask_size": self.quote.ask_size, "quote_age_seconds": quote_age if math.isfinite(quote_age) else None}),
            "activity": self._signal_payload("activity", activity_score, round(activity_score, 4), sample_confidence, now),
            "composite": self._signal_payload("composite", composite_score, round(composite_score, 4), composite_confidence, now, {"component_scores": {name: round(score, 4) for name, (score, _weight) in components.items()}}),
        }
        return self.signals

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = float(now if now is not None else time.time())
        self.evaluate(now)
        trade_age = now - self.last_trade_timestamp if self.last_trade_timestamp else None
        quote_age = now - self.quote.timestamp if self.quote.timestamp else None
        live = self.quote.market_data_type == "LIVE"
        stale = trade_age is None or trade_age > self.config.stale_after_seconds
        return {
            "symbol": self.symbol,
            "contract": self.contract,
            "tick_active": self.tick_active,
            "tick_type": self.tick_type or None,
            "market_data_type": self.quote.market_data_type,
            "live": live,
            "trade_eligible": live and not stale and self.tick_active and not self.halted,
            "halted": self.halted,
            "last_trade_price": self.last_trade_price or None,
            "last_trade_timestamp_utc": _utc_iso(self.last_trade_timestamp) if self.last_trade_timestamp else None,
            "last_trade_age_seconds": trade_age,
            "quote": {"bid": self.quote.bid or None, "ask": self.quote.ask or None, "bid_size": self.quote.bid_size, "ask_size": self.quote.ask_size, "timestamp_utc": _utc_iso(self.quote.timestamp) if self.quote.timestamp else None, "age_seconds": quote_age},
            "data_quality": {"total_ticks": self.total_ticks, "signal_ticks": self.signal_ticks, "excluded_unreported_ticks": self.excluded_unreported_ticks, "baseline_seconds_observed": max(0.0, now - self.buckets[0].second) if self.buckets else 0.0, "last_error": self.last_error},
            "signals": self.signals,
        }


class TickRadarEngine:
    """Stateful multi-symbol radar with deterministic alerts and calibration."""

    def __init__(self, config: Optional[RadarConfig] = None) -> None:
        self.config = config or RadarConfig.from_env()
        self.symbols: Dict[str, SymbolRadarState] = {}
        self.alerts: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self.pending_outcomes: Deque[PendingOutcome] = deque(maxlen=2000)
        self.calibration: MutableMapping[Tuple[str, str, int], CalibrationStat] = defaultdict(CalibrationStat)
        self.created_at = time.time()
        self.subscription_meta: Dict[str, Dict[str, Any]] = {}

    def state(self, symbol: str) -> SymbolRadarState:
        key = symbol.upper().strip()
        if not key:
            raise ValueError("symbol is required")
        state = self.symbols.get(key)
        if state is None:
            state = SymbolRadarState(symbol=key, config=self.config)
            self.symbols[key] = state
        return state

    def mark_subscription(self, symbol: str, *, tick_active: bool, tick_type: str = "", contract: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        state = self.state(symbol)
        state.tick_active = tick_active
        state.tick_type = tick_type
        if contract is not None:
            state.contract = dict(contract)
        state.last_error = error
        self.subscription_meta[state.symbol] = {"tick_active": tick_active, "tick_type": tick_type or None, "contract": state.contract, "error": error, "updated_at_utc": _utc_iso(time.time())}

    def ingest_quote(self, symbol: str, **kwargs: Any) -> None:
        self.state(symbol).ingest_quote(**kwargs)

    def ingest_trade(self, symbol: str, **kwargs: Any) -> None:
        state = self.state(symbol)
        state.ingest_trade(**kwargs)
        now = _to_epoch(kwargs.get("timestamp"))
        self._resolve_pending(state, now)
        self._evaluate_alerts(state, now)

    def _evaluate_alerts(self, state: SymbolRadarState, now: float) -> None:
        signals = state.evaluate(now)
        for kind in ("large_trade", "volume_burst", "trade_intensity", "signed_flow", "price_impulse", "composite"):
            payload = signals[kind]
            score = float(payload["score"])
            absolute = abs(score)
            if absolute <= self.config.alert_rearm_score:
                state.alert_armed[kind] = True
            last_at = state.last_alert_at.get(kind, 0.0)
            eligible = absolute >= self.config.alert_score_threshold and state.alert_armed[kind] and now - last_at >= self.config.alert_cooldown_seconds
            if not eligible:
                continue
            direction = _sign(score)
            if direction == 0 or state.last_trade_price <= 0:
                continue
            state.alert_armed[kind] = False
            state.last_alert_at[kind] = now
            alert = {
                "id": f"{state.symbol}:{kind}:{int(now * 1000)}",
                "symbol": state.symbol,
                "kind": kind,
                "score": score,
                "direction": direction,
                "confidence": payload["confidence"],
                "emitted_at_utc": _utc_iso(now),
                "start_price": state.last_trade_price,
                "half_life_seconds": payload["half_life_seconds"],
                "effective_horizon_minutes": payload["effective_horizon_minutes"],
                "projected_score_if_unconfirmed": payload["projected_score_if_unconfirmed"],
                "raw_value": payload["raw_value"],
            }
            self.alerts.append(alert)
            self.pending_outcomes.append(PendingOutcome(symbol=state.symbol, signal_kind=kind, direction=direction, emitted_at=now, start_price=state.last_trade_price))

    def _resolve_pending(self, state: SymbolRadarState, now: float) -> None:
        if state.last_trade_price <= 0:
            return
        keep: Deque[PendingOutcome] = deque(maxlen=self.pending_outcomes.maxlen)
        for pending in self.pending_outcomes:
            if pending.symbol != state.symbol:
                keep.append(pending)
                continue
            due = [horizon for horizon in pending.pending_horizons if now >= pending.emitted_at + horizon]
            for horizon in due:
                raw_return_bps = (state.last_trade_price / pending.start_price - 1.0) * 10_000.0
                signed_return_bps = raw_return_bps * pending.direction
                self.calibration[(pending.symbol, pending.signal_kind, horizon)].update(signed_return_bps)
                pending.pending_horizons.remove(horizon)
            if pending.pending_horizons:
                keep.append(pending)
        self.pending_outcomes = keep

    def _attach_calibration(self, symbol_payload: Dict[str, Any]) -> None:
        symbol = symbol_payload["symbol"]
        for kind, signal in symbol_payload["signals"].items():
            signal["empirical_forward_performance"] = {f"{horizon // 60}m": self.calibration[(symbol, kind, horizon)].payload(self.config.min_calibration_samples) for horizon in CALIBRATION_HORIZONS_SECONDS}

    def snapshot(self, symbol: str = "", now: Optional[float] = None) -> Dict[str, Any]:
        now = float(now if now is not None else time.time())
        if symbol:
            state = self.symbols.get(symbol.upper())
            selected: Iterable[SymbolRadarState] = [state] if state else []
        else:
            selected = self.symbols.values()
        payloads = [state.snapshot(now) for state in selected if state is not None]
        for payload in payloads:
            self._attach_calibration(payload)
        payloads.sort(key=lambda item: abs(item.get("signals", {}).get("composite", {}).get("score", 0.0)), reverse=True)
        return {
            "generated_at_utc": _utc_iso(now),
            "engine_uptime_seconds": max(0.0, now - self.created_at),
            "config": self.config.as_dict(),
            "symbol_count": len(payloads),
            "active_tick_streams": sum(1 for item in payloads if item["tick_active"]),
            "symbols": payloads,
            "alerts": list(self.alerts)[-100:],
            "interpretation": {
                "score_range": "-100 bearish to +100 bullish; activity is unsigned",
                "decay_projection": "Projected scores assume no new confirming ticks and are not return forecasts.",
                "empirical_forward_performance": "Only treat as calibrated when sample_count reaches the configured minimum.",
            },
        }

    def recent_alerts(self, limit: int = 50, minimum_score: float = 0.0) -> List[Dict[str, Any]]:
        values = [item for item in self.alerts if abs(float(item.get("score", 0.0))) >= minimum_score]
        return values[-max(1, limit):]


_DEFAULT_ENGINE: Optional[TickRadarEngine] = None


def get_or_create_engine(runtime: Any = None) -> TickRadarEngine:
    global _DEFAULT_ENGINE
    if runtime is not None:
        existing = getattr(runtime, "tick_radar_engine", None)
        if isinstance(existing, TickRadarEngine):
            _DEFAULT_ENGINE = existing
            return existing
        market_lines = int(getattr(getattr(runtime, "policy", None), "market_data_lines", 100))
        engine = TickRadarEngine(RadarConfig.from_env(market_lines))
        setattr(runtime, "tick_radar_engine", engine)
        _DEFAULT_ENGINE = engine
        return engine
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = TickRadarEngine()
    return _DEFAULT_ENGINE


def get_default_engine() -> TickRadarEngine:
    return get_or_create_engine(None)
