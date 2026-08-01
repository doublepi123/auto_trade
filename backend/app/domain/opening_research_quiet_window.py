from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session


_QUIET_WINDOW_LEAD = timedelta(minutes=2)
_QUIET_WINDOW_AFTER_OPEN = timedelta(minutes=5)


def is_opening_research_quiet_window(
    now: datetime | None = None,
) -> bool:
    """Whether heavyweight research should yield around the US open.

    This admission window is intentionally independent from live opening
    execution settings. It reserves capacity without granting order authority
    or changing the live execution reservation window.
    """
    current = _as_utc(now or datetime.now(timezone.utc))
    session = get_session("US")
    local = session.local(current)
    session_day = local.date()
    if local.weekday() >= 5 or is_market_closed("US", session_day):
        return False
    session_open = datetime.combine(
        session_day,
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    return (
        session_open - _QUIET_WINDOW_LEAD
        <= current
        < session_open + _QUIET_WINDOW_AFTER_OPEN
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
