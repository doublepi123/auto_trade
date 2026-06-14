from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.holiday_calendar import (
    closure_label,
    is_half_day,
    is_market_closed,
    list_closures,
)
from app.core.market_calendar import (
    is_trading_hours,
    market_for_symbol,
    next_session_open,
    trade_day_for,
)


def test_market_closed_on_known_us_holiday():
    # Independence Day 2024 — NYSE closed
    assert is_market_closed("US", date(2024, 7, 4)) is True
    assert closure_label("US", date(2024, 7, 4)) == "Independence Day"


def test_market_open_on_normal_weekday():
    assert is_market_closed("US", date(2024, 7, 2)) is False
    assert is_market_closed("US", date(2024, 7, 5)) is False


def test_hk_lunar_new_year_three_days():
    for d in (date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31)):
        assert is_market_closed("HK", d) is True, f"HK should be closed on {d}"


def test_rth_false_on_us_holiday_during_market_hours():
    # 2024-07-04 is a Thursday, 14:30 UTC = 10:30 ET (inside RTH),
    # but the exchange is closed for Independence Day.
    ts = datetime(2024, 7, 4, 14, 30, tzinfo=timezone.utc)
    assert is_trading_hours("US", ts) is False


def test_rth_true_on_normal_weekday():
    ts = datetime(2024, 7, 2, 14, 30, tzinfo=timezone.utc)  # Tuesday 10:30 ET
    assert is_trading_hours("US", ts) is True


def test_half_day_flags_christmas_eve_hk():
    assert is_half_day("HK", date(2024, 12, 24)) is True
    assert is_half_day("HK", date(2024, 7, 2)) is False


def test_list_closures_filters_by_market_and_year():
    items = list_closures("US", 2025)
    assert all(item["market"] == "US" for item in items)
    assert all(item["date"].startswith("2025") for item in items)
    assert any(item["date"] == "2025-12-25" for item in items)


def test_list_closures_returns_empty_for_unknown_market():
    assert list_closures("ZZ", 2025) == []


def test_market_for_symbol_handles_hk_suffix():
    assert market_for_symbol("700.HK") == "HK"
    assert market_for_symbol("AAPL.US") == "US"
    assert market_for_symbol("") == "US"


def test_trade_day_for_resolves_local_clock():
    # 2024-07-04 in HK = Thursday 16:00 HKT == 08:00 UTC.
    # Local trade day should be 2024-07-04 (HK calendar day).
    ts = datetime(2024, 7, 4, 8, 0, tzinfo=timezone.utc)
    assert trade_day_for("HK", ts).isoformat() == "2024-07-04"


def test_next_session_open_skips_holidays():
    # 2024-07-03 is a US half-day (early close), 2024-07-04 is closed.
    # From 14:00 UTC on 2024-07-04, next session open should be 2024-07-05.
    ts = datetime(2024, 7, 4, 14, 0, tzinfo=timezone.utc)
    nxt = next_session_open("US", ts)
    # 09:30 ET == 13:30 UTC (DST)
    assert nxt.hour == 13
    assert nxt.minute == 30
    assert nxt.date().isoformat() == "2024-07-05"


def test_2027_holidays_loaded():
    """2027 data must be present so the API bound is honest."""
    from app.core.holiday_calendar import COVERAGE_END_YEAR, list_closures
    assert COVERAGE_END_YEAR == 2027
    us_2027 = list_closures("US", 2027)
    assert len(us_2027) >= 9, f"expected at least 9 NYSE closures in 2027, got {len(us_2027)}"
    hk_2027 = list_closures("HK", 2027)
    assert len(hk_2027) >= 10, f"expected at least 10 HKEX closures in 2027, got {len(hk_2027)}"


def test_calendar_closures_api_rejects_out_of_range_year():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    # 2030 is now rejected (data only goes through 2027).
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2030})
    assert resp.status_code == 422
    # 2020 is below the coverage start.
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2020})
    assert resp.status_code == 422
    # 2027 is in range.
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2027})
    assert resp.status_code == 200
    assert resp.json()["coverage_end_year"] == 2027
