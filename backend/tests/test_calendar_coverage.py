from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.main import app
from app.services.calendar_coverage_service import CalendarCoverageService


def _service() -> CalendarCoverageService:
    return CalendarCoverageService(
        coverage_start_year=2024,
        coverage_end_year=2027,
        warning_window_days=60,
    )


def test_coverage_is_authoritative_before_warning_window() -> None:
    status = _service().status(date(2027, 10, 31))

    assert status.status == "COVERED"
    assert status.authoritative is True
    assert status.days_until_coverage_end == 61


def test_warning_starts_at_exact_boundary() -> None:
    status = _service().status(date(2027, 11, 1))

    assert status.status == "WARNING"
    assert status.authoritative is True
    assert status.days_until_coverage_end == 60


def test_coverage_end_date_remains_authoritative() -> None:
    status = _service().status(date(2027, 12, 31))

    assert status.status == "WARNING"
    assert status.authoritative is True
    assert status.days_until_coverage_end == 0


def test_first_date_after_coverage_is_expired() -> None:
    status = _service().status(date(2028, 1, 1))

    assert status.status == "EXPIRED"
    assert status.authoritative is False
    assert status.days_until_coverage_end == -1
    assert "not tracked" in status.message


def test_date_before_coverage_is_not_authoritative() -> None:
    status = _service().status(date(2023, 12, 31))

    assert status.status == "EXPIRED"
    assert status.authoritative is False
    assert "before the coverage window" in status.message


def test_calendar_coverage_api_returns_typed_boundary_status() -> None:
    response = TestClient(app).get(
        "/api/calendar/coverage",
        params={"as_of": "2027-11-01"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "WARNING",
        "authoritative": True,
        "coverage_start_year": 2024,
        "coverage_end_year": 2027,
        "coverage_years": [2024, 2025, 2026, 2027],
        "as_of": "2027-11-01",
        "coverage_end_date": "2027-12-31",
        "days_until_coverage_end": 60,
        "warning_window_days": 60,
        "message": (
            "Holiday data is authoritative but coverage ends in 60 day(s) "
            "on 2027-12-31; update the static calendar soon."
        ),
    }


def test_calendar_coverage_api_rejects_invalid_date() -> None:
    response = TestClient(app).get(
        "/api/calendar/coverage",
        params={"as_of": "2027-02-30"},
    )

    assert response.status_code == 422
