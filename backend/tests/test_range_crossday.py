from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.range_crossday import RangeCrossdayEngine
from app.domain.range_replay import TapeQuote
from tests.test_range_replay import config, quote

ET = ZoneInfo("America/New_York")


def event(day: str, second: int, last: str, sequence: int) -> TapeQuote:
    now = datetime.fromisoformat(day+"T10:00:00").replace(tzinfo=ET)+timedelta(seconds=second)
    return replace(quote(0, last), observed_at=now, source_at=now, depth_at=now, sequence=sequence)


def loss(engine: RangeCrossdayEngine, day: str, second: int, sequence: int) -> None:
    for i, price in enumerate(("100.1", "99.95", "99.95", "99.60", "99.60")):
        engine.on_quote(event(day, second+i, price, sequence+i))


def test_five_losses_across_days_block_the_next_whole_session_not_just_midnight() -> None:
    engines = {candidate: RangeCrossdayEngine(config(), candidate=candidate) for candidate in (False, True)}
    for engine in engines.values():
        for i in range(3):
            loss(engine, "2026-10-28", i*100, i*10+1)
        for i in range(2):
            loss(engine, "2026-10-29", i*100, 100+i*10)
        # Remainder of trigger day and next entire session are blocked.
        loss(engine, "2026-10-29", 300, 200)
        loss(engine, "2026-10-30", 0, 300)
    baseline: dict[str, Any] = engines[False].report()
    candidate: dict[str, Any] = engines[True].report()
    assert len(baseline["closed_trades"]) == 7
    assert len(candidate["closed_trades"]) == 5
    assert candidate["crossday_rest_through"] == "2026-10-30"
    assert candidate["crossday_rest_activations"] == 1
    loss(engines[True], "2026-11-02", 0, 400)
    resumed: dict[str, Any] = engines[True].report()
    assert len(resumed["closed_trades"]) == 6
    assert resumed["crossday_loss_streak"] == 1
    assert resumed["crossday_rest_through"] is None
    assert resumed["automatic_promotion"] is False


def test_next_session_rest_skips_holiday_and_survives_20_et_rollover() -> None:
    engine = RangeCrossdayEngine(config(), candidate=True)
    for i in range(3):
        loss(engine, "2026-11-24", i*100, i*10+1)
    for i in range(2):
        loss(engine, "2026-11-25", i*100, 100+i*10)
    report: dict[str, Any] = engine.report()
    assert report["crossday_rest_through"] == "2026-11-27"
    night = event("2026-11-25", 10*3600+1, "100.1", 200)
    engine.on_quote(night)
    assert engine.report()["crossday_rest_through"] == "2026-11-27"
    loss(engine, "2026-11-27", 0, 300)
    assert len(engine.report()["closed_trades"]) == 5
    loss(engine, "2026-11-30", 0, 400)
    assert len(engine.report()["closed_trades"]) == 6


def test_a_profitable_close_resets_crossday_loss_sequence() -> None:
    engine = RangeCrossdayEngine(config(), candidate=True)
    loss(engine, "2026-10-28", 0, 1)
    for i, price in enumerate(("100.1", "99.95", "99.95", "101", "101")):
        engine.on_quote(event("2026-10-29", i, price, 100+i))
    assert engine.report()["crossday_loss_streak"] == 0
    assert engine.report()["crossday_rest_activations"] == 0
