from src.tick_radar import RadarConfig, TickRadarEngine


def make_engine(**overrides):
    base = RadarConfig(
        max_tick_streams=5,
        retention_seconds=900,
        baseline_seconds=60,
        recent_volume_seconds=5,
        flow_seconds=10,
        price_impulse_seconds=10,
        min_baseline_seconds=10,
        min_large_trade_notional_usd=10_000,
        large_trade_z_threshold=2.0,
        alert_score_threshold=40,
        alert_rearm_score=20,
        alert_cooldown_seconds=0,
        decay_floor_score=20,
        include_unreported=False,
        min_calibration_samples=1,
        stale_after_seconds=10,
    )
    values = {**base.__dict__, **overrides}
    return TickRadarEngine(RadarConfig(**values))


def seed_baseline(engine, symbol="TEST", start=1_000.0, seconds=70):
    engine.mark_subscription(symbol, tick_active=True, tick_type="AllLast")
    for i in range(seconds):
        ts = start + i
        price = 100.0 + i * 0.001
        engine.ingest_quote(
            symbol,
            timestamp=ts,
            bid=price - 0.01,
            ask=price + 0.01,
            bid_size=100,
            ask_size=100,
            market_data_type="LIVE",
        )
        engine.ingest_trade(
            symbol,
            timestamp=ts,
            price=price + 0.01,
            size=100,
            exchange="NASDAQ",
        )
    return start + seconds


def test_large_trade_emits_decaying_signal():
    engine = make_engine()
    now = seed_baseline(engine)
    engine.ingest_trade("TEST", timestamp=now, price=100.20, size=5_000, exchange="NASDAQ")
    snapshot = engine.snapshot("TEST", now=now)["symbols"][0]
    signal = snapshot["signals"]["large_trade"]
    assert signal["score"] > 40
    assert signal["projected_score_if_unconfirmed"]["1m"] < signal["score"]
    assert signal["effective_horizon_minutes"] > 0


def test_volume_burst_and_flow_are_directional():
    engine = make_engine()
    now = seed_baseline(engine)
    for i in range(20):
        engine.ingest_quote(
            "TEST",
            timestamp=now + i * 0.1,
            bid=100.10,
            ask=100.11,
            bid_size=200,
            ask_size=100,
            market_data_type="LIVE",
        )
        engine.ingest_trade(
            "TEST",
            timestamp=now + i * 0.1,
            price=100.11,
            size=500,
            exchange="NASDAQ",
        )
    snapshot = engine.snapshot("TEST", now=now + 3)["symbols"][0]
    assert snapshot["signals"]["volume_burst"]["score"] > 30
    assert snapshot["signals"]["signed_flow"]["score"] > 0
    assert snapshot["signals"]["composite"]["score"] > 0


def test_unreported_ticks_are_excluded_by_default():
    engine = make_engine()
    engine.mark_subscription("TEST", tick_active=True, tick_type="AllLast")
    engine.ingest_quote("TEST", timestamp=1000, bid=99.9, ask=100.1, bid_size=100, ask_size=100, market_data_type="LIVE")
    engine.ingest_trade("TEST", timestamp=1000, price=100, size=10_000, exchange="DARK", unreported=True)
    payload = engine.snapshot("TEST", now=1001)["symbols"][0]
    assert payload["data_quality"]["total_ticks"] == 1
    assert payload["data_quality"]["signal_ticks"] == 0
    assert payload["data_quality"]["excluded_unreported_ticks"] == 1


def test_halt_tick_changes_trade_eligibility():
    engine = make_engine()
    now = seed_baseline(engine, seconds=20)
    engine.ingest_trade("TEST", timestamp=now, price=0, size=0, past_limit=True)
    payload = engine.snapshot("TEST", now=now)["symbols"][0]
    assert payload["halted"] is True
    assert payload["trade_eligible"] is False


def test_forward_calibration_resolves_at_one_minute():
    engine = make_engine(alert_score_threshold=30)
    now = seed_baseline(engine)
    engine.ingest_trade("TEST", timestamp=now, price=100.20, size=5_000, exchange="NASDAQ")
    assert engine.pending_outcomes
    engine.ingest_quote("TEST", timestamp=now + 61, bid=100.49, ask=100.51, bid_size=100, ask_size=100, market_data_type="LIVE")
    engine.ingest_trade("TEST", timestamp=now + 61, price=100.51, size=100, exchange="NASDAQ")
    payload = engine.snapshot("TEST", now=now + 61)["symbols"][0]
    empirical = payload["signals"]["large_trade"]["empirical_forward_performance"]["1m"]
    assert empirical["sample_count"] >= 1
    assert empirical["calibrated"] is True
    assert empirical["mean_signed_return_bps"] > 0


def test_trade_eligible_requires_live_fresh_tick_stream():
    engine = make_engine()
    state = engine.state("TEST")
    state.tick_active = True
    state.ingest_quote(1000, 99.9, 100.1, 100, 100, "DELAYED")
    state.ingest_trade(1000, 100.1, 100)
    assert engine.snapshot("TEST", now=1001)["symbols"][0]["trade_eligible"] is False
    state.ingest_quote(1002, 100.0, 100.2, 100, 100, "LIVE")
    state.ingest_trade(1002, 100.2, 100)
    assert engine.snapshot("TEST", now=1003)["symbols"][0]["trade_eligible"] is True
