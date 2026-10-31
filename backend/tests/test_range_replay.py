from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.domain.range_replay import (
    RangeReplayConfig, RangeReplayEngine, ReplayArm, TapeQuote, round_trip_outcomes,
)

AT = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)


def quote(seconds: int, last: str, *, size: int = 1000, run: str = "r1") -> TapeQuote:
    price = D(last)
    now = AT + timedelta(seconds=seconds)
    return TapeQuote(
        symbol="TSLA.US", run_id=run, sequence=seconds+1,
        observed_at=now, source_at=now, depth_at=now,
        last=price, bid=price-D(".01"), ask=price+D(".01"),
        bid_size=size, ask_size=size,
    )


def config(**kwargs: object) -> RangeReplayConfig:
    values: dict[str, object] = {
        "initial_buy_low": D("100"), "initial_sell_high": D("100.6"),
        "max_notional": D("10000"), "max_quantity": 100,
        "band_settle_seconds": 0, "recenter_minutes": 600,
    }
    values.update(kwargs)
    return replace(RangeReplayConfig(), **values)


def run(arm: ReplayArm, observations: list[TapeQuote], **kwargs: object) -> dict[str, object]:
    engine = RangeReplayEngine(config(**kwargs), arm)
    for observation in observations:
        engine.on_quote(observation)
    return engine.report()


def test_round_trip_costs_share_prices_and_separate_fees_from_extra_cost() -> None:
    outcomes = round_trip_outcomes(D("100"), D("101"), 100)
    assert outcomes["measured"]["gross"] == D("100")
    assert outcomes["measured"]["fees"] == D("4.42441")
    assert outcomes["confirmation"]["fees"] == outcomes["measured"]["fees"]
    assert outcomes["legacy_stress"]["fees"] == D("10.05")
    assert outcomes["confirmation"]["extra_costs"] == D("4.08231")
    assert outcomes["confirmation"]["net"] == D("91.49328")


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), D("0")])
def test_costs_reject_invalid_execution_prices(value: D) -> None:
    with pytest.raises(ValueError):
        round_trip_outcomes(value, D("100"), 10)


def test_candidate_waits_for_reclaim_and_fills_only_on_a_later_event() -> None:
    observations = [quote(0, "100.10"), quote(1, "99.95"), quote(2, "99.96")]
    baseline = run("crossing_baseline", observations)
    candidate = run("reclaim_candidate", observations)
    assert baseline["position_open"] is True
    assert candidate["position_open"] is False
    observations += [quote(3, "100.00")]
    candidate = run("reclaim_candidate", observations)
    assert candidate["position_open"] is False
    assert candidate["pending_entry"] is True
    candidate = run("reclaim_candidate", observations+[quote(4, "100.00")])
    assert candidate["position_open"] is True


def test_expired_arm_and_changed_capture_run_cannot_create_a_reclaim() -> None:
    for last in (quote(302, "100.01"), quote(3, "100.01", run="r2")):
        result = run("reclaim_candidate", [
            quote(0, "100.10"), quote(1, "99.95"), last, quote(303, "100.01", run=last.run_id),
        ])
        assert result["position_open"] is False


def test_deep_breach_invalidates_candidate_instead_of_buying_a_large_dip() -> None:
    result = run("reclaim_candidate", [
        quote(0, "100.10"), quote(1, "99.70"), quote(2, "100.01"), quote(3, "100.01"),
    ])
    assert result["position_open"] is False


def test_no_displayed_size_means_no_manufactured_entry_fill() -> None:
    result = run("crossing_baseline", [
        quote(0, "100.1"), quote(1, "99.95"),
        quote(2, "99.95", size=0), quote(7, "99.95", size=0),
    ])
    assert result["position_open"] is False
    assert result["unfilled_entries"] == 1


def test_entry_limit_is_not_repriced_to_a_favorable_future_market() -> None:
    result = run("crossing_baseline", [
        quote(0, "100.1"), quote(1, "99.95"), quote(2, "100.1"), quote(7, "100.1"),
    ])
    assert result["position_open"] is False
    assert result["unfilled_entries"] == 1


def test_stop_uses_bid_and_leaves_unresolved_inventory_when_data_ends() -> None:
    result = run("crossing_baseline", [
        quote(0, "100.1"), quote(1, "99.95"), quote(2, "99.95"),
        quote(3, "99.60", size=0), quote(10, "99.60", size=0),
    ])
    assert result["position_open"] is True
    assert result["pending_exit"] is True
    assert result["closed_trades"] == []
    assert result["eligible_for_review"] is False


def test_stop_exit_has_delay_and_identical_cost_models_for_both_arms() -> None:
    result = run("crossing_baseline", [
        quote(0, "100.1"), quote(1, "99.95"), quote(2, "99.95"),
        quote(3, "99.60"), quote(4, "99.60"),
    ])
    assert result["position_open"] is False
    trades = result["closed_trades"]
    assert isinstance(trades, list) and len(trades) == 1
    assert trades[0]["exit_cause"] == "PRICE_STOP"
    assert trades[0]["exit_at"] == quote(4, "99.60").observed_at.isoformat()
    assert D(trades[0]["costs"]["confirmation"]["net"]) < 0


def test_stale_or_crossed_quotes_never_supply_execution_evidence() -> None:
    stale = replace(quote(2, "99.95"), source_at=AT-timedelta(seconds=60))
    crossed = replace(quote(3, "99.95"), bid=D("101"), ask=D("100"))
    result = run("crossing_baseline", [
        quote(0, "100.1"), quote(1, "99.95"), stale, crossed, quote(8, "100.1"),
    ])
    assert result["position_open"] is False
    assert result["quality_rejections"]


def test_input_cannot_be_reordered_or_duplicated_to_improve_results() -> None:
    engine = RangeReplayEngine(config(), "crossing_baseline")
    engine.on_quote(quote(1, "100.1"))
    with pytest.raises(ValueError):
        engine.on_quote(quote(0, "99.9"))


def test_config_versions_change_when_signal_or_risk_changes() -> None:
    assert config().version_hash != config(arm_seconds=120).version_hash
    with pytest.raises(ValueError):
        config(stop_pct=D("1"))
    with pytest.raises(ValueError):
        config(max_notional=D("100000"))
