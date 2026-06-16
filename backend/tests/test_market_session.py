"""Market session clock — session_status + API. Deterministic instants."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.market_calendar import session_status
from app.main import app

client = TestClient(app)

# 2026-06-17 is a Wednesday. EDT = UTC-4 (June DST); HKT = UTC+8 (no DST).
WED = datetime(2026, 6, 17, tzinfo=timezone.utc)
SAT = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _at(base: datetime, h: int, m: int = 0) -> datetime:
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


class TestSessionStatus:
    def test_us_rth(self) -> None:
        # 14:30 UTC = 10:30 EDT -> RTH
        assert session_status("US", _at(WED, 14, 30)) == "rth"

    def test_us_pre(self) -> None:
        # 12:00 UTC = 08:00 EDT -> pre
        assert session_status("US", _at(WED, 12, 0)) == "pre"

    def test_us_post(self) -> None:
        # 21:00 UTC = 17:00 EDT -> post
        assert session_status("US", _at(WED, 21, 0)) == "post"

    def test_us_weekend_closed(self) -> None:
        assert session_status("US", _at(SAT, 14, 30)) == "closed"

    def test_hk_rth(self) -> None:
        # 03:00 UTC = 11:00 HKT -> RTH
        assert session_status("HK", _at(WED, 3, 0)) == "rth"

    def test_hk_lunch(self) -> None:
        # 04:30 UTC = 12:30 HKT -> lunch break (window is [12:00, 13:00); 13:00 resumes)
        assert session_status("HK", _at(WED, 4, 30)) == "lunch"


class TestSessionAPI:
    def test_session_endpoint(self) -> None:
        resp = client.get("/api/calendar/session", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["market"] == "US"
        assert data["status"] in {"rth", "pre", "post", "lunch", "closed"}
        assert "local_time" in data and "next_open" in data

    def test_session_hk_symbol(self) -> None:
        resp = client.get("/api/calendar/session", params={"symbol": "0700.HK"})
        assert resp.status_code == 200
        assert resp.json()["market"] == "HK"
