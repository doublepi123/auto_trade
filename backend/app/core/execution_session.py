"""Execution windows supported by this system, independently of entry policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from app.core.holiday_calendar import (
    COVERAGE_START_YEAR,
    is_coverage_expired,
    is_half_day,
    is_market_closed,
)
from app.core.market_calendar import get_session

ExecutionPhase = Literal["RTH", "PRE", "POST", "OVERNIGHT", "UNAVAILABLE", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class ExecutionSessionDecision:
    market: str
    phase: ExecutionPhase
    reason: str
    phase_started_at: datetime | None
    phase_ends_at: datetime | None

    @property
    def extended_hours_executable(self) -> bool:
        return self.phase in ("PRE", "POST", "OVERNIGHT")


def outside_rth_for_phase(phase: str) -> str | None:
    """SDK session token for an executable extended phase, else None."""
    if phase in ("PRE", "POST"):
        return "ANY_TIME"
    if phase == "OVERNIGHT":
        return "OVERNIGHT"
    return None


def _overnight_trading_day(local: datetime) -> date | None:
    """Trading day N that owns an overnight instant, or None outside the window.

    Overnight runs 20:00 ET on calendar day D through 03:50 ET on D+1 and
    belongs to N = D+1. The 03:50-04:00 gap has no venue.
    """
    current = local.time()
    if current >= time(20):
        return local.date() + timedelta(days=1)
    if current < time(3, 50):
        return local.date()
    return None


def _full_trading_day(market: str, day: date) -> bool:
    """Weekday, not closed, not a half day, and inside calendar coverage."""
    if day.year < COVERAGE_START_YEAR or is_coverage_expired(market, day):
        return False
    if day.weekday() >= 5 or is_market_closed(market, day) or is_half_day(market, day):
        return False
    return True


def _phase_bounds(
    session: object, day: date, start: time, end: time,
) -> tuple[datetime, datetime]:
    timezone_info = session.timezone  # type: ignore[attr-defined]
    return (
        datetime.combine(day, start, tzinfo=timezone_info).astimezone(timezone.utc),
        datetime.combine(day, end, tzinfo=timezone_info).astimezone(timezone.utc),
    )


def resolve_execution_session(
    market: str,
    instant: datetime | None = None,
    *,
    overnight_enabled: bool = False,
) -> ExecutionSessionDecision:
    """Resolve the supported execution phase at an exchange-local instant."""
    code = market.upper()
    session = get_session(code)
    now = instant if instant is not None else datetime.now(timezone.utc)
    local = session.local(now)
    day = local.date()
    if code == "US":
        overnight_day = _overnight_trading_day(local)
        if overnight_day is not None:
            if not overnight_enabled:
                return ExecutionSessionDecision(
                    code, "UNAVAILABLE",
                    "overnight session is not supported by this system", None, None,
                )
            if not _full_trading_day(code, overnight_day):
                return ExecutionSessionDecision(
                    code, "UNAVAILABLE", "no overnight session tonight", None, None,
                )
            started_at = datetime.combine(
                overnight_day - timedelta(days=1), time(20), tzinfo=session.timezone,
            ).astimezone(timezone.utc)
            ends_at = datetime.combine(
                overnight_day, time(3, 50), tzinfo=session.timezone,
            ).astimezone(timezone.utc)
            return ExecutionSessionDecision(
                code, "OVERNIGHT", "supported US overnight execution window",
                started_at, ends_at,
            )
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

    started_at, ends_at = _phase_bounds(session, day, start, end)
    return ExecutionSessionDecision(code, phase, reason, started_at, ends_at)


def _utc_now(instant: datetime | None) -> datetime:
    if instant is None:
        return datetime.now(timezone.utc)
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def extended_last_executable_close(
    market: str,
    instant: datetime | None = None,
    *,
    overnight_enabled: bool = False,
) -> datetime | None:
    """End of the executable span that contains ``instant``.

    With AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED effective, entry cutoff and
    flatten windows are measured from THIS boundary, not the RTH close:
    - US PRE/RTH/POST span: post-market ends 20:00 ET (RTH close on half days);
    - US overnight span, only when overnight is enabled and the instant is in
      OVERNIGHT: 03:50 ET on trading day N. The two spans do not cross 20:00;
    - HK: no extended phases, so the RTH close;
    - weekends, holidays, calendar-coverage gaps: None (nothing executable).
    The result is an exchange-local aware datetime; None means no executable
    phase exists for that instant.

    Lives here — NOT in market_calendar.py — because that module's source is
    hashed into frozen research digests (strategy_v2 forward semantics,
    watchlist_quant_v6) which must not drift.
    """
    code = market.upper()
    session = get_session(code)
    now = _utc_now(instant)
    local = session.local(now)
    if overnight_enabled and code == "US":
        decision = resolve_execution_session(code, now, overnight_enabled=True)
        if decision.phase == "OVERNIGHT" and decision.phase_ends_at is not None:
            return decision.phase_ends_at.astimezone(session.timezone)
    day = local.date()
    if day.year < COVERAGE_START_YEAR or is_coverage_expired(code, day):
        return None
    if local.weekday() >= 5 or is_market_closed(code, day):
        return None
    close = session.close_time(day)
    if code == "US" and not is_half_day(code, day):
        return datetime.combine(day, time(20), tzinfo=session.timezone)
    return datetime.combine(day, close, tzinfo=session.timezone)


def is_extended_closing_window(
    market: str,
    minutes: int,
    instant: datetime | None = None,
    *,
    overnight_enabled: bool = False,
) -> bool:
    """Whether ``instant`` is within ``minutes`` of the last executable phase end.

    Companion to ``extended_last_executable_close``: with the extended-hours
    trading flag effective this replaces the RTH-close ``is_closing_window``
    boundary. False whenever no executable phase exists that day. Unlike
    ``is_closing_window`` it does NOT require RTH to be open — the window may
    fall inside PRE/POST. Lives here for the same frozen-digest reason as
    ``extended_last_executable_close``.
    """
    if minutes <= 0:
        return False
    now = _utc_now(instant)
    close = extended_last_executable_close(
        market, now, overnight_enabled=overnight_enabled,
    )
    if close is None:
        return False
    return close - timedelta(minutes=minutes) <= now < close
