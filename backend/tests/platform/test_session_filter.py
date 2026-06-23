from __future__ import annotations

from datetime import datetime, time, timezone

from app.platform.session_filter import MarketSessionFilter


def test_rth_during_regular_hours():
    f = MarketSessionFilter(
        rth_window=(time(9, 30), time(16, 0)),
        pre_window=(time(4, 0), time(9, 30)),
        post_window=(time(16, 0), time(20, 0)),
    )
    assert f.session_at(datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)) == "rth"  # Monday
    assert f.session_at(datetime(2026, 6, 22, 5, 0, tzinfo=timezone.utc)) == "pre"
    assert f.session_at(datetime(2026, 6, 22, 17, 0, tzinfo=timezone.utc)) == "post"
    assert f.session_at(datetime(2026, 6, 22, 3, 0, tzinfo=timezone.utc)) == "closed"


def test_weekend_is_closed():
    f = MarketSessionFilter(rth_window=(time(9, 30), time(16, 0)))
    assert f.session_at(datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)) == "closed"  # Saturday
    assert f.session_at(datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)) == "closed"  # Sunday


def test_allows_respects_allowed_sessions():
    f = MarketSessionFilter(
        rth_window=(time(9, 30), time(16, 0)),
        pre_window=(time(4, 0), time(9, 30)),
    )
    rth_ts = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    pre_ts = datetime(2026, 6, 22, 5, 0, tzinfo=timezone.utc)
    assert f.allows(rth_ts, ("rth",)) is True
    assert f.allows(pre_ts, ("rth",)) is False
    assert f.allows(pre_ts, ("rth", "pre")) is True
