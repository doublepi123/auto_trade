"""Synthetic risk/calendar regressions; no services, database, or broker fixtures."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from app.domain.range_replay import RangeReplayConfig, RangeReplayEngine, ReplayArm, TapeQuote

ET = ZoneInfo("America/New_York")
START = datetime(2026, 11, 2, 10, 0, tzinfo=ET)
ARMS: tuple[ReplayArm, ...] = ("crossing_baseline", "reclaim_candidate")


def _at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ET)


def _config(**changes: object) -> RangeReplayConfig:
    values: dict[str, object] = {
        "initial_buy_low": D("100"),
        "initial_sell_high": D("101"),
        "max_quantity": 100,
        "max_notional": D("10000"),
        # Keep recentering out of risk/exit scenarios unless explicitly requested.
        "recenter_minutes": 600,
    }
    values.update(changes)
    return replace(RangeReplayConfig(), **values)


def _q(
    second: int,
    last: str,
    *,
    start: datetime = START,
    sequence: int | None = None,
    run: str = "synthetic",
    bid_size: int = 1000,
    ask_size: int = 1000,
) -> TapeQuote:
    instant = start + timedelta(seconds=second)
    price = D(last)
    return TapeQuote(
        symbol="TSLA.US", run_id=run, sequence=second + 1 if sequence is None else sequence,
        observed_at=instant, source_at=instant, depth_at=instant,
        last=price, bid=price - D(".01"), ask=price + D(".01"),
        bid_size=bid_size, ask_size=ask_size,
    )


def _report(engine: RangeReplayEngine) -> dict[str, Any]:
    # The public report deliberately exposes a heterogeneous JSON-like mapping.
    return cast(dict[str, Any], engine.report())


def _open(*, start: datetime = START, **changes: object) -> RangeReplayEngine:
    engine = RangeReplayEngine(_config(**changes), "crossing_baseline")
    for second, last in ((0, "100.10"), (1, "99.95"), (2, "99.95")):
        engine.on_quote(_q(second, last, start=start))
    report = _report(engine)
    assert report["position_open"] is True
    assert report["pending_exit"] is False
    assert report["unresolved_position"]["entry_price"] == "99.96"
    assert report["unresolved_position"]["quantity"] == 100
    return engine


@pytest.mark.parametrize("day,next_day", [("2026-10-29", "2026-10-30"), ("2026-11-02", "2026-11-03")])
def test_drawdown_latch_survives_20_et_rollover_in_both_dst_regimes(
    day: str, next_day: str,
) -> None:
    start = _at(day, "10:00:00")
    engine = _open(start=start, daily_loss=D("20"), max_drawdown=D("30"))
    engine.on_quote(_q(3, "99.60", start=start))
    engine.on_quote(_q(4, "99.60", start=start))
    before = deepcopy(_report(engine))
    assert before["day_paused"] is True
    assert before["drawdown_paused"] is True
    assert before["closed_trades"][0]["exit_cause"] == "DRAWDOWN_LIMIT"
    assert D(before["max_mark_to_market_drawdown"]) > D("30")

    engine.on_quote(_q(0, "100.10", start=_at(day, "19:59:59"), sequence=100))
    assert _report(engine)["day_paused"] is True
    engine.on_quote(_q(0, "100.10", start=_at(day, "20:00:00"), sequence=101))
    rolled = _report(engine)
    assert rolled["day_paused"] is False
    assert rolled["drawdown_paused"] is True
    assert rolled["max_mark_to_market_drawdown"] == before["max_mark_to_market_drawdown"]
    assert rolled["cost_totals"] == before["cost_totals"]
    assert rolled["closed_trades"] == before["closed_trades"]

    for offset, last in enumerate(("100.10", "99.95", "99.95")):
        engine.on_quote(_q(offset, last, start=_at(next_day, "10:00:00"), sequence=200 + offset))
    after = _report(engine)
    assert after["signals"]["ENTRY_FILLED"] == 1
    assert after["pending_entry"] is False
    assert after["position_open"] is False
    assert after["drawdown_paused"] is True


def test_daily_loss_streak_and_entry_budget_reset_while_drawdown_history_remains() -> None:
    engine = _open(max_losses=1, max_entries=1)
    engine.on_quote(_q(3, "99.60"))
    engine.on_quote(_q(4, "99.60"))
    before = _report(engine)
    assert before["day_paused"] is True
    assert before["drawdown_paused"] is False
    assert before["closed_trades"][0]["exit_cause"] == "PRICE_STOP"
    for second, last in ((70, "100.10"), (71, "99.95"), (72, "99.95")):
        engine.on_quote(_q(second, last))
    assert _report(engine)["signals"]["ENTRY_FILLED"] == 1

    engine.on_quote(_q(0, "100.10", start=_at("2026-11-02", "20:00:00"), sequence=100))
    rolled = _report(engine)
    assert rolled["day_paused"] is False
    assert rolled["max_mark_to_market_drawdown"] == before["max_mark_to_market_drawdown"]
    for offset, last in enumerate(("100.10", "99.95", "99.95")):
        engine.on_quote(_q(offset, last, start=_at("2026-11-03", "10:00:00"), sequence=200 + offset))
    assert _report(engine)["signals"]["ENTRY_FILLED"] == 2
    assert _report(engine)["position_open"] is True
    assert _report(engine)["day_paused"] is False


def test_prior_risk_day_equity_peak_still_triggers_new_day_drawdown() -> None:
    engine = _open(initial_sell_high=D("100.6"), max_drawdown=D("30"))
    engine.on_quote(_q(3, "100.65"))
    engine.on_quote(_q(4, "100.65"))
    before = _report(engine)
    assert before["closed_trades"][0]["exit_cause"] == "TARGET"
    assert D(before["cost_totals"]["confirmation"]["net"]) > D("50")
    assert before["drawdown_paused"] is False

    tomorrow = _at("2026-11-03", "10:00:00")
    for offset, last in enumerate(("100.10", "99.95", "99.95")):
        engine.on_quote(_q(offset, last, start=tomorrow, sequence=100 + offset))
    assert _report(engine)["drawdown_paused"] is False
    engine.on_quote(_q(3, "99.70", start=tomorrow, sequence=103))
    assert _report(engine)["drawdown_paused"] is True
    engine.on_quote(_q(4, "99.70", start=tomorrow, sequence=104))
    after = _report(engine)
    assert after["closed_trades"][1]["exit_cause"] == "DRAWDOWN_LIMIT"
    assert D(after["max_mark_to_market_drawdown"]) > D("30")
    # Resetting the peak at the second entry would see only a $25 price decline.
    assert D(after["closed_trades"][1]["costs"]["confirmation"]["net"]) < D("-30")


@pytest.mark.parametrize("pending_cause", ["TARGET", "PROFIT_LOCK"])
def test_price_stop_replaces_lower_priority_pending_exit_and_restarts_fill_delay(
    pending_cause: str,
) -> None:
    engine = _open()
    if pending_cause == "TARGET":
        engine.on_quote(_q(3, "101.10", bid_size=0))
        stop_second = 4
    else:
        engine.on_quote(_q(3, "100.40"))
        engine.on_quote(_q(4, "100.15", bid_size=0))
        stop_second = 5
    assert _report(engine)["pending_exit"] is True
    assert _report(engine)["signals"]["EXIT_DECISION"] == 1

    stop = _q(stop_second, "99.60")
    engine.on_quote(stop)
    pending = _report(engine)
    assert pending["position_open"] is True
    assert pending["closed_trades"] == []
    assert pending["signals"]["EXIT_DECISION"] == 2
    # A different callback at the same instant cannot fill the replacement.
    engine.on_quote(replace(stop, sequence=100))
    assert _report(engine)["closed_trades"] == []
    fill = _q(stop_second + 1, "99.60", sequence=101)
    engine.on_quote(fill)
    trade = _report(engine)["closed_trades"][0]
    assert trade["exit_cause"] == "PRICE_STOP"
    assert trade["exit_decision"]["sequence"] == stop.sequence
    assert trade["exit_fill"]["sequence"] == fill.sequence
    assert trade["exit_price"] == "99.59"
    assert _report(engine)["position_open"] is False


@pytest.mark.parametrize("activate", [False, True])
def test_profit_floor_requires_activation_and_a_later_executable_quote(activate: bool) -> None:
    engine = _open()
    entry = D(_report(engine)["unresolved_position"]["entry_price"])
    activation = entry * D("1.004")
    floor = entry * D("1.002")
    bid = activation if activate else activation - D(".000001")
    engine.on_quote(replace(_q(3, str(bid + D(".01"))), bid=bid))
    assert _report(engine)["pending_exit"] is False
    floor_quote = replace(_q(4, str(floor + D(".01"))), bid=floor)
    engine.on_quote(floor_quote)
    assert _report(engine)["pending_exit"] is activate
    assert _report(engine)["closed_trades"] == []
    engine.on_quote(replace(floor_quote, sequence=100))
    assert _report(engine)["closed_trades"] == []
    engine.on_quote(replace(floor_quote, observed_at=START + timedelta(seconds=5),
                            source_at=START + timedelta(seconds=5),
                            depth_at=START + timedelta(seconds=5), sequence=101))
    report = _report(engine)
    if activate:
        trade = report["closed_trades"][0]
        assert trade["exit_cause"] == "PROFIT_LOCK"
        assert trade["exit_decision"]["sequence"] == floor_quote.sequence
        assert trade["exit_fill"]["sequence"] == 101
        assert D(trade["exit_price"]) == floor
    else:
        assert report["position_open"] is True
        assert report["pending_exit"] is False
        assert report["closed_trades"] == []


def test_max_holding_clock_starts_at_entry_fill_and_exits_after_exact_deadline() -> None:
    engine = _open()
    engine.on_quote(_q(3601, "100.10"))  # 60 minutes after decision, but not fill.
    assert _report(engine)["pending_exit"] is False
    engine.on_quote(_q(3602, "100.10"))
    assert _report(engine)["pending_exit"] is True
    assert _report(engine)["closed_trades"] == []
    engine.on_quote(_q(3603, "100.10"))
    trade = _report(engine)["closed_trades"][0]
    assert trade["exit_cause"] == "TIME_STOP"
    assert trade["exit_decision"]["observed_at"] == _q(3602, "100.10").observed_at.isoformat()
    assert trade["exit_at"] == _q(3603, "100.10").observed_at.isoformat()


@pytest.mark.parametrize(
    "day,entry_time,flatten_time,expected_minutes",
    [("2026-11-02", "15:00:00", "15:45:00", 390),
     ("2026-11-27", "11:50:00", "12:45:00", 210)],
)
def test_eod_uses_actual_session_close_and_delays_fill(
    day: str, entry_time: str, flatten_time: str, expected_minutes: int,
) -> None:
    start = _at(day, entry_time)
    engine = _open(start=start)
    flatten = _at(day, flatten_time)
    second = int((flatten - start).total_seconds())
    engine.on_quote(_q(second - 1, "100.10", start=start))
    assert _report(engine)["pending_exit"] is False
    engine.on_quote(_q(second, "100.10", start=start))
    assert _report(engine)["closed_trades"] == []
    assert _report(engine)["pending_exit"] is True
    engine.on_quote(_q(second + 1, "100.10", start=start))
    report = _report(engine)
    trade = report["closed_trades"][0]
    assert trade["exit_cause"] == "EOD_FLATTEN"
    assert trade["exit_decision"]["observed_at"] == flatten.isoformat()
    assert report["daily_coverage"][day]["expected_minutes"] == expected_minutes


def test_half_day_close_cannot_supply_a_fill_for_unresolved_eod_inventory() -> None:
    start = _at("2026-11-27", "11:50:00")
    engine = _open(start=start)
    engine.on_quote(_q(3300, "100.10", start=start, bid_size=0))  # 12:45 ET.
    assert _report(engine)["pending_exit"] is True
    engine.on_quote(_q(4200, "100.10", start=start))  # Exactly 13:00 ET.
    report = _report(engine)
    assert report["closed_trades"] == []
    assert report["position_open"] is True
    assert report["unresolved_position"]["quantity"] == 100
    assert report["eligible_for_review"] is False
    # Zero displayed bid size can trigger an exit but cannot certify execution
    # coverage; the exact-close callback is outside RTH altogether.
    assert report["valid_events"] == 3
    assert report["quality_rejections"]["MISSING_POSITIVE_SIZE"] == 1


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("day,cutoff", [("2026-11-02", "15:15:00"), ("2026-11-27", "12:15:00")])
@pytest.mark.parametrize("window", ["warmup", "cutoff"])
def test_warmup_and_cutoff_cannot_create_entries_or_reclaim_arms(
    arm: ReplayArm, day: str, cutoff: str, window: str,
) -> None:
    start = _at(day, "09:30:00" if window == "warmup" else cutoff)
    engine = RangeReplayEngine(_config(), arm)
    for offset, last in enumerate(("100.10", "99.99", "100.00", "100.00")):
        engine.on_quote(_q(offset, last, start=start))
    report = _report(engine)
    assert report["position_open"] is False
    assert report["pending_entry"] is False
    assert report["signals"].get("ENTRY_DECISION", 0) == 0
    assert report["signals"].get("ARMED", 0) == 0
    assert report["valid_events"] == 4


@pytest.mark.parametrize("arm", ARMS)
def test_warmup_arm_is_not_carried_to_first_eligible_reclaim(arm: ReplayArm) -> None:
    start = _at("2026-11-02", "09:30:00")
    engine = RangeReplayEngine(_config(), arm)
    for second, last in ((298, "100.10"), (299, "99.99"), (300, "100.00")):
        engine.on_quote(_q(second, last, start=start))
    assert _report(engine)["pending_entry"] is False
    assert _report(engine)["signals"].get("ENTRY_DECISION", 0) == 0
    for second, last in ((301, "100.10"), (302, "99.99"), (303, "100.00"), (304, "100.00")):
        engine.on_quote(_q(second, last, start=start))
    assert _report(engine)["signals"]["ENTRY_FILLED"] == 1


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("day,cutoff", [("2026-11-02", "15:15:00"), ("2026-11-27", "12:15:00")])
def test_pending_entry_is_canceled_at_exact_cutoff_before_attempting_fill(
    arm: ReplayArm, day: str, cutoff: str,
) -> None:
    start = _at(day, cutoff) - timedelta(seconds=3)
    engine = RangeReplayEngine(_config(), arm)
    for quote in (_q(0, "100.10", start=start), _q(1, "99.99", start=start),
                  _q(2, "100.00", start=start, ask_size=0)):
        engine.on_quote(quote)
    assert _report(engine)["pending_entry"] is True
    engine.on_quote(_q(3, "100.00", start=start))
    report = _report(engine)
    assert report["pending_entry"] is False
    assert report["position_open"] is False
    assert report["unfilled_entries"] == 1
    assert report["signals"]["ENTRY_DECISION"] == 1
    assert report["signals"].get("ENTRY_FILLED", 0) == 0


@pytest.mark.parametrize("arm", ARMS)
def test_recenter_settlement_blocks_crossings_until_exact_180_second_boundary(arm: ReplayArm) -> None:
    engine = RangeReplayEngine(_config(recenter_minutes=10), arm)
    engine.on_quote(_q(0, "98"))
    event = _report(engine)["recenter_events"][0]
    assert D(event["buy"]) == D("97.706")
    assert D(event["sell"]) == D("98.294")
    for second, last in ((177, "97.80"), (178, "97.70"), (179, "97.80")):
        engine.on_quote(_q(second, last))
    assert _report(engine)["signals"].get("ENTRY_DECISION", 0) == 0
    assert _report(engine)["signals"].get("ARMED", 0) == 0
    engine.on_quote(_q(180, "97.70"))
    if arm == "crossing_baseline":
        assert _report(engine)["pending_entry"] is True
    else:
        assert _report(engine)["signals"]["ARMED"] == 1
    engine.on_quote(_q(181, "97.71"))
    engine.on_quote(_q(182, "97.71"))
    report = _report(engine)
    assert report["position_open"] is True
    assert report["signals"]["ENTRY_FILLED"] == 1
    assert len(report["recenter_events"]) == 1


def test_recenter_cancels_old_arm_even_when_settlement_is_zero() -> None:
    engine = RangeReplayEngine(_config(recenter_minutes=1, band_settle_seconds=0), "reclaim_candidate")
    engine.on_quote(_q(0, "100.10"))
    engine.on_quote(_q(1, "99.95"))
    assert _report(engine)["signals"]["ARMED"] == 1
    engine.on_quote(_q(60, "99.00"))  # New band: 98.703 .. 99.297.
    assert len(_report(engine)["recenter_events"]) == 1
    engine.on_quote(_q(61, "98.69"))
    engine.on_quote(_q(62, "98.71"))
    assert _report(engine)["pending_entry"] is False
    assert _report(engine)["signals"].get("ENTRY_DECISION", 0) == 0
    for second, last in ((63, "98.69"), (64, "98.71"), (65, "98.71")):
        engine.on_quote(_q(second, last))
    assert _report(engine)["signals"]["ARMED"] == 2
    assert _report(engine)["signals"]["ENTRY_FILLED"] == 1


@pytest.mark.parametrize("arm", ARMS)
def test_different_future_prices_depth_and_runs_preserve_realized_prefix_and_entry(arm: ReplayArm) -> None:
    prefix = [
        _q(second, last) for second, last in (
            (0, "100.10"), (1, "99.95"), (2, "100.00"), (3, "99.95"),
            (4, "101.10"), (5, "101.10"),
            (70, "100.10"), (71, "99.95"), (72, "100.00"), (73, "99.95"),
        )
    ]
    prefix_engine = RangeReplayEngine(_config(), arm)
    for quote in prefix:
        prefix_engine.on_quote(quote)
    frozen_prefix = deepcopy(_report(prefix_engine))
    assert len(frozen_prefix["closed_trades"]) == 1
    assert frozen_prefix["position_open"] is True
    boundary = prefix[-1].observed_at
    finals: list[dict[str, Any]] = []
    for price, run in (("101.10", "future-up"), ("99.60", "future-down")):
        suffix = [_q(74, price, run=run, bid_size=0), _q(75, price, run=run)]
        engine = RangeReplayEngine(_config(), arm)
        snapshots: list[dict[str, Any]] = []
        for quote in prefix + suffix:
            engine.on_quote(quote)
            snapshots.append(deepcopy(_report(engine)))
        assert snapshots[len(prefix) - 1] == frozen_prefix
        final = _report(engine)
        earlier_trades = [
            trade for trade in final["closed_trades"]
            if datetime.fromisoformat(trade["exit_at"]) <= boundary
        ]
        assert earlier_trades == frozen_prefix["closed_trades"]
        future_trade = final["closed_trades"][1]
        for key in ("entry_at", "entry_price", "quantity"):
            assert future_trade[key] == frozen_prefix["unresolved_position"][key]
        finals.append(final)
    # These are genuinely different complete replays, not two copies of a prefix.
    assert finals[0]["closed_trades"][1]["exit_cause"] == "TARGET"
    assert finals[1]["closed_trades"][1]["exit_cause"] == "PRICE_STOP"
    assert finals[0]["cost_totals"] != finals[1]["cost_totals"]
    assert _report(prefix_engine) == frozen_prefix


@pytest.mark.parametrize(
    "field,age_seconds,reason",
    [("source_at", 30.000001, "STALE_SOURCE"),
     ("source_at", -2.000001, "STALE_SOURCE"),
     ("depth_at", 5.000001, "STALE_DEPTH"),
     ("depth_at", -0.000001, "STALE_DEPTH")],
)
def test_out_of_bound_aware_clocks_cannot_fill_pending_entry(
    field: str, age_seconds: float, reason: str,
) -> None:
    engine = RangeReplayEngine(_config(), "crossing_baseline")
    engine.on_quote(_q(0, "100.10"))
    engine.on_quote(_q(1, "99.95"))
    bad = _q(2, "99.95")
    engine.on_quote(replace(bad, **{field: bad.observed_at - timedelta(seconds=age_seconds)}))
    report = _report(engine)
    assert report["pending_entry"] is True
    assert report["position_open"] is False
    assert report["quality_rejections"] == {reason: 1}
    engine.on_quote(_q(3, "99.95"))
    assert _report(engine)["position_open"] is True


@pytest.mark.parametrize("source_age,depth_age", [(30, 5), (-2, 0)])
def test_inclusive_source_and_depth_clock_boundaries_remain_executable(
    source_age: int, depth_age: int,
) -> None:
    engine = RangeReplayEngine(_config(), "crossing_baseline")
    engine.on_quote(_q(0, "100.10"))
    engine.on_quote(_q(1, "99.95"))
    fill = _q(2, "99.95")
    engine.on_quote(replace(
        fill, source_at=fill.observed_at - timedelta(seconds=source_age),
        depth_at=fill.observed_at - timedelta(seconds=depth_age),
    ))
    assert _report(engine)["position_open"] is True
    assert _report(engine)["quality_rejections"] == {}


@pytest.mark.parametrize(
    "field,reason", [("source_at", "MISSING_SOURCE_TIME"), ("depth_at", "MISSING_DEPTH_TIME")],
)
def test_naive_quote_clocks_are_reported_and_cannot_execute(field: str, reason: str) -> None:
    engine = RangeReplayEngine(_config(), "crossing_baseline")
    quote = _q(0, "100.10")
    engine.on_quote(replace(quote, **{field: quote.observed_at.replace(tzinfo=None)}))
    assert _report(engine)["quality_rejections"] == {reason: 1}
    assert _report(engine)["valid_events"] == 0


@pytest.mark.parametrize(
    "changes",
    [{"observed_at": START.replace(tzinfo=None)}, {"symbol": "AAPL.US"},
     {"run_id": ""}, {"sequence": 0}, {"sequence": -1}, {"sequence": True},
     pytest.param({"sequence": 1.5}, id="fractional-capture-sequence")],
)
def test_malformed_receive_time_or_capture_identity_fails_closed(changes: dict[str, object]) -> None:
    engine = RangeReplayEngine(_config(), "crossing_baseline")
    before = deepcopy(_report(engine))
    with pytest.raises(ValueError):
        engine.on_quote(replace(_q(0, "100.10"), **changes))
    assert _report(engine) == before


def test_duplicate_sequence_and_resumed_old_run_are_rejected_without_state_mutation() -> None:
    engine = RangeReplayEngine(_config(), "reclaim_candidate")
    first = _q(0, "100.10")
    engine.on_quote(first)
    before = deepcopy(_report(engine))
    with pytest.raises(ValueError, match="sequence"):
        engine.on_quote(replace(_q(1, "99.95"), sequence=first.sequence))
    assert _report(engine) == before
    engine.on_quote(_q(2, "99.95", run="restarted", sequence=1))
    before_restart = deepcopy(_report(engine))
    with pytest.raises(ValueError, match="run"):
        engine.on_quote(_q(3, "100.00", run=first.run_id))
    assert _report(engine) == before_restart


@pytest.mark.parametrize(
    "changes",
    [
        {"max_quantity": 0}, {"max_quantity": 1001},
        pytest.param({"max_quantity": 1.5}, id="fractional-share-bound"),
        {"max_notional": D("0")}, {"max_price_risk": D("0")},
        {"max_price_risk": D("500.01")},
        {"initial_buy_low": D("101")}, {"profit_floor_pct": D("0")},
        {"profit_floor_pct": D(".4")}, {"fill_delay_seconds": 0},
        {"order_timeout_seconds": 1}, {"flatten_minutes": -1},
        {"flatten_minutes": 45}, {"cutoff_minutes": 391},
        {"holding_minutes": 0}, {"arm_seconds": 0}, {"recenter_minutes": 0},
        {"max_recenters": 0}, {"max_entries": 0}, {"max_losses": 0},
        {"cooldown_seconds": -1}, {"band_settle_seconds": -1}, {"warmup_minutes": -1},
        {"daily_loss": D("NaN")}, {"max_drawdown": D("Infinity")},
        {"recenter_drift_pct": D("-.1")}, {"max_entries": True},
    ],
)
def test_invalid_risk_calendar_and_execution_config_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**changes)
