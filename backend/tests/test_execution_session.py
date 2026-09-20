from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.execution_session import (
    ExecutionPhase,
    ExecutionSessionDecision,
    resolve_execution_session,
)


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(
        tzinfo=ZoneInfo("America/New_York"),
    ).astimezone(timezone.utc)


def test_post_market_weekday_is_post() -> None:
    # Given
    instant = _et("2026-09-21T17:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.market == "US"
    assert result.phase == "POST"
    assert result.phase_started_at is not None
    assert result.phase_ends_at is not None
    assert result.phase_started_at == datetime(2026, 9, 21, 20, tzinfo=timezone.utc)
    assert result.phase_ends_at == datetime(2026, 9, 22, 0, tzinfo=timezone.utc)
    assert result.phase_started_at.tzinfo is timezone.utc
    assert result.phase_ends_at.tzinfo is timezone.utc


def test_two_am_et_is_unavailable_not_pre() -> None:
    # Given
    instant = _et("2026-09-21T02:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNAVAILABLE"
    assert "overnight" in result.reason.lower()
    assert "not supported by this system" in result.reason.lower()
    assert result.phase_started_at is None
    assert result.phase_ends_at is None


def test_eleven_pm_et_is_unavailable_not_post() -> None:
    # Given
    instant = _et("2026-09-21T23:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNAVAILABLE"
    assert "overnight" in result.reason.lower()


@pytest.mark.parametrize("day", ["2026-09-21", "2026-12-21"])
@pytest.mark.parametrize("clock", ["03:59:59", "04:00:00"])
def test_pre_market_starts_at_0400_et(day: str, clock: str) -> None:
    # Given
    instant = _et(f"{day}T{clock}")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == ("PRE" if clock == "04:00:00" else "UNAVAILABLE")
    if clock == "04:00:00":
        assert result.phase_started_at is not None
        assert result.phase_ends_at is not None
        assert result.phase_started_at == _et(f"{day}T04:00:00")
        assert result.phase_ends_at == _et(f"{day}T09:30:00")
        assert result.phase_started_at.tzinfo is timezone.utc
        assert result.phase_ends_at.tzinfo is timezone.utc


@pytest.mark.parametrize("clock", ["19:59:59", "20:00:00"])
def test_post_market_ends_at_2000_et(clock: str) -> None:
    # Given
    instant = _et(f"2026-09-21T{clock}")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == ("POST" if clock == "19:59:59" else "UNAVAILABLE")


@pytest.mark.parametrize("clock", ["09:30:00", "15:59:59"])
def test_rth_is_rth(clock: str) -> None:
    # Given
    instant = _et(f"2026-09-21T{clock}")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "RTH"
    assert result.phase_started_at == _et("2026-09-21T09:30:00")
    assert result.phase_ends_at == _et("2026-09-21T16:00:00")


def test_weekend_is_unavailable() -> None:
    # Given
    instant = _et("2026-09-20T17:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNAVAILABLE"
    assert result.reason == "market closed"


def test_holiday_is_unavailable() -> None:
    # Given
    instant = _et("2026-11-26T10:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNAVAILABLE"
    assert result.reason == "market closed"


@pytest.mark.parametrize("clock", ["13:00:00", "17:00:00"])
def test_half_day_post_is_unavailable(clock: str) -> None:
    # Given
    instant = _et(f"2026-11-27T{clock}")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNAVAILABLE"
    assert "half-day" in result.reason


@pytest.mark.parametrize("hour", [8, 12, 17])
def test_hk_extended_hours_unavailable(hour: int) -> None:
    # Given
    instant = datetime(2026, 9, 21, hour, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    # When
    result = resolve_execution_session("HK", instant.astimezone(timezone.utc))
    # Then
    assert result.phase == "UNAVAILABLE"
    assert "HK" in result.reason
    assert "extended-hours execution is not supported" in result.reason


@pytest.mark.parametrize("day", ["2031-09-22", "2031-09-21", "2023-09-21"])
def test_coverage_expired_is_unknown_and_not_executable(day: str) -> None:
    # Given
    instant = _et(f"{day}T17:00:00")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "UNKNOWN"
    assert "coverage" in result.reason
    assert result.extended_hours_executable is False
    assert result.phase_started_at is None
    assert result.phase_ends_at is None


@pytest.mark.parametrize("phase", ["RTH", "PRE", "POST", "UNAVAILABLE", "UNKNOWN"])
def test_extended_hours_executable_only_for_pre_and_post(phase: ExecutionPhase) -> None:
    # Given
    decision = ExecutionSessionDecision("US", phase, "test", None, None)
    # When
    executable = decision.extended_hours_executable
    # Then
    assert executable is (phase in ("PRE", "POST"))


def test_half_day_rth_ends_at_actual_close() -> None:
    # Given
    instant = _et("2026-11-27T12:59:59")
    # When
    result = resolve_execution_session("US", instant)
    # Then
    assert result.phase == "RTH"
    assert result.phase_ends_at == _et("2026-11-27T13:00:00")


def test_unknown_market_has_unknown_coverage() -> None:
    # Given
    instant = _et("2026-09-21T10:00:00")
    # When
    result = resolve_execution_session("INVALID", instant)
    # Then
    assert result.phase == "UNKNOWN"
    assert "coverage" in result.reason
