from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from app.domain.range_replay import RangeReplayEngine
from tests.test_range_replay import config, quote


def test_missing_displayed_sizes_cannot_certify_executable_minute_coverage() -> None:
    engine = RangeReplayEngine(config(), "crossing_baseline")
    engine.on_quote(replace(quote(0, "100.1"), bid_size=None, ask_size=None))
    report = engine.report()
    assert report["valid_events"] == 0
    assert report["quality_rejections"] == {"MISSING_POSITIVE_SIZE": 1}


def test_later_exit_cannot_erase_unresolved_inventory_at_prior_session_close() -> None:
    engine = RangeReplayEngine(config(), "crossing_baseline")
    for q in [quote(0, "100.1"), quote(1, "99.95"), quote(2, "99.95")]:
        engine.on_quote(q)
    next_day = quote(3, "99.95")
    next_day = replace(
        next_day, observed_at=next_day.observed_at+timedelta(days=3, hours=1),
        source_at=next_day.observed_at+timedelta(days=3, hours=1),
        depth_at=next_day.observed_at+timedelta(days=3, hours=1),
    )
    engine.on_quote(next_day)
    engine.on_quote(replace(
        next_day, sequence=5, observed_at=next_day.observed_at+timedelta(seconds=1),
        source_at=next_day.observed_at+timedelta(seconds=1),
        depth_at=next_day.observed_at+timedelta(seconds=1),
    ))
    report: dict[str, Any] = engine.report()
    assert report["position_open"] is False
    assert report["cross_session_carries"] == 1
    assert report["quality_complete"] is False
