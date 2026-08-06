import asyncio

from src.operational import (
    RequestGovernor,
    RuntimePolicy,
    RuntimeState,
    api_limits_payload,
    plan_watchlist,
)


def test_policy_defaults_are_read_only(monkeypatch):
    monkeypatch.delenv("IBKR_AGENT_MODE", raising=False)
    policy = RuntimePolicy.from_env()
    assert policy.agent_mode == "read_only"
    assert policy.order_tools_enabled is False


def test_live_mode_requires_explicit_second_switch(monkeypatch):
    monkeypatch.setenv("IBKR_AGENT_MODE", "live")
    monkeypatch.delenv("IBKR_ALLOW_LIVE_TRADING", raising=False)
    assert RuntimePolicy.from_env().order_tools_enabled is False
    monkeypatch.setenv("IBKR_ALLOW_LIVE_TRADING", "true")
    assert RuntimePolicy.from_env().order_tools_enabled is True


def test_paper_account_detection_and_order_guard():
    policy = RuntimePolicy(
        agent_mode="paper",
        require_order_confirmation=True,
        max_order_notional_usd=1_000,
    )
    runtime = RuntimeState(policy=policy)
    mode, _ = runtime.detect_account_mode(["DU123456"])
    assert mode == "paper"
    blocked = runtime.validate_order_intent("DU123456", 10, 50)
    assert blocked["allowed"] is False
    assert any("confirmation" in item for item in blocked["blockers"])
    allowed = runtime.validate_order_intent("DU123456", 10, 50, "approved")
    assert allowed["allowed"] is True


def test_watchlist_budget_is_conservative():
    policy = RuntimePolicy(max_operational_streams=3)
    plan = plan_watchlist(["AAPL", "MSFT", "NVDA"], policy, benchmarks=["SPY"])
    assert plan["accepted"] == ["SPY", "AAPL", "MSFT"]
    assert plan["rejected"] == ["NVDA"]


def test_limits_explain_unsubscribed_paper_account():
    payload = api_limits_payload(RuntimePolicy())
    paper = payload["paper_without_extra_subscriptions"]
    assert "not available" in paper["stock_and_option_live_quotes"]
    assert payload["request_pacing"]["configured_general_requests_per_second"] == 50


def test_historical_governor_blocks_identical_requests():
    async def scenario():
        governor = RequestGovernor(100)
        first = await governor.check_historical("same", "contract")
        second = await governor.check_historical("same", "contract")
        assert first.allowed is True
        assert second.allowed is False
        assert second.retry_after_seconds > 0

    asyncio.run(scenario())
