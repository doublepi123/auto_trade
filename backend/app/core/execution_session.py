"""Execution windows supported by this system, independently of entry policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Literal

from app.core.holiday_calendar import (
    COVERAGE_START_YEAR,
    is_coverage_expired,
    is_half_day,
    is_market_closed,
)
from app.core.market_calendar import get_session

ExecutionPhase = Literal["RTH", "PRE", "POST", "UNAVAILABLE", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class ExecutionSessionDecision:
    market: str
    phase: ExecutionPhase
    reason: str
    phase_started_at: datetime | None
    phase_ends_at: datetime | None

    @property
    def extended_hours_executable(self) -> bool:
        return self.phase in ("PRE", "POST")


def resolve_execution_session(
    market: str, instant: datetime | None = None,
) -> ExecutionSessionDecision:
    """Resolve the supported execution phase at an exchange-local instant."""
    code = market.upper()
    session = get_session(code)
    now = instant if instant is not None else datetime.now(timezone.utc)
    local = session.local(now)
    day = local.date()
    if day.year < COVERAGE_START_YEAR or is_coverage_expired(code, day):
        return ExecutionSessionDecision(
            code, "UNKNOWN", "calendar coverage unavailable for this date or market", None, None,
        )
    if local.weekday() >= 5 or is_market_closed(code, day):
        return ExecutionSessionDecision(code, "UNAVAILABLE", "market closed", None, None)

    current = local.time()
    close = session.close_time(day)
    phase: ExecutionPhase
    if session.is_rth(now):
        phase, start, end = "RTH", session.rth_open, close
        reason = "regular trading hours"
    else:
        if code != "US":
            return ExecutionSessionDecision(
                code, "UNAVAILABLE",
                f"extended-hours execution is not supported for {code}", None, None,
            )
        if is_half_day(code, day) and current >= close:
            return ExecutionSessionDecision(
                code, "UNAVAILABLE", "half-day post-market execution is not supported",
                None, None,
            )
        if time(4) <= current < session.rth_open:
            phase, start, end = "PRE", time(4), session.rth_open
            reason = "supported US pre-market execution window"
        elif close <= current < time(20):
            phase, start, end = "POST", close, time(20)
            reason = "supported US post-market execution window"
        else:
            return ExecutionSessionDecision(
                code, "UNAVAILABLE", "overnight session is not supported by this system",
                None, None,
            )

    return ExecutionSessionDecision(
        code, phase, reason,
        datetime.combine(day, start, tzinfo=session.timezone).astimezone(timezone.utc),
        datetime.combine(day, end, tzinfo=session.timezone).astimezone(timezone.utc),
    )
