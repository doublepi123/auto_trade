"""Exchange-aware calendar helpers.

The trading day for risk accounting and daily PnL must follow the exchange's
local clock, not UTC. The US session crosses UTC midnight cleanly only because
RTH runs in the evening UTC; HK and other Asian sessions sit either side of
UTC midnight too. Using a single UTC date to slice ``daily_pnl`` mid-session
silently breaks accounting.

The helpers below resolve a UTC instant to the exchange's local trade day
("the trading session whose RTH window the instant belongs to or precedes"),
and answer whether an instant is inside RTH for that market.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from app.core.holiday_calendar import closure_label, is_market_closed


@dataclass(frozen=True)
class MarketSession:
    """A market's regular trading hours (local time)."""

    code: str
    timezone: ZoneInfo
    rth_open: time
    rth_close: time
    lunch_start: time | None = None
    lunch_end: time | None = None

    def local(self, instant: datetime) -> datetime:
        return _ensure_utc(instant).astimezone(self.timezone)

    def trade_day(self, instant: datetime) -> date:
        local = self.local(instant)
        return local.date()

    def is_rth(self, instant: datetime) -> bool:
        local = self.local(instant)
        if local.weekday() >= 5:
            return False
        if is_market_closed(self.code, local.date()):
            return False
        current = local.time()
        if not (self.rth_open <= current < self.rth_close):
            return False
        if (
            self.lunch_start is not None
            and self.lunch_end is not None
            and self.lunch_start <= current < self.lunch_end
        ):
            return False
        return True


_US_SESSION = MarketSession(
    code="US",
    timezone=ZoneInfo("America/New_York"),
    rth_open=time(9, 30),
    rth_close=time(16, 0),
)

_HK_SESSION = MarketSession(
    code="HK",
    timezone=ZoneInfo("Asia/Hong_Kong"),
    rth_open=time(9, 30),
    rth_close=time(16, 0),
    lunch_start=time(12, 0),
    lunch_end=time(13, 0),
)


_SESSIONS: dict[str, MarketSession] = {
    "US": _US_SESSION,
    "HK": _HK_SESSION,
}


def get_session(market: str) -> MarketSession:
    """Resolve a market code (US/HK) to its trading session. Defaults to US."""
    return _SESSIONS.get((market or "US").upper(), _US_SESSION)


def supported_markets() -> Iterable[str]:
    return tuple(_SESSIONS.keys())


def trade_day_for(market: str, instant: datetime | None = None) -> date:
    """Return the trading day in the exchange's local clock.

    The boundary is local midnight. Instants that fall on a weekend or after
    RTH but before local midnight still belong to that local date; consumers
    that need session-aware semantics should also consult ``is_trading_hours``.
    """
    session = get_session(market)
    return session.trade_day(instant or datetime.now(timezone.utc))


def is_trading_hours(market: str, instant: datetime | None = None) -> bool:
    """Whether ``instant`` falls inside the market's regular trading hours."""
    session = get_session(market)
    return session.is_rth(instant or datetime.now(timezone.utc))


def market_for_symbol(symbol: str) -> str:
    """Infer market code from a broker symbol suffix."""
    upper = (symbol or "").upper()
    if upper.endswith(".HK"):
        return "HK"
    return "US"


def next_session_open(market: str, instant: datetime | None = None) -> datetime:
    """Return the next RTH open in UTC after ``instant``.

    Skips weekends and full-day market closures. If the lookup overflows the
    holiday calendar's coverage window (data ends 2026 currently), the
    function falls back to skipping weekends only — it never returns a date
    that ``is_market_closed`` would mark as closed within the covered range.
    """
    session = get_session(market)
    here = _ensure_utc(instant or datetime.now(timezone.utc)).astimezone(session.timezone)
    if (
        here.weekday() < 5
        and session.lunch_start is not None
        and session.lunch_end is not None
        and session.lunch_start <= here.time() < session.lunch_end
    ):
        resume = here.replace(
            hour=session.lunch_end.hour,
            minute=session.lunch_end.minute,
            second=0,
            microsecond=0,
        )
        return resume.astimezone(timezone.utc)
    candidate = here.replace(hour=session.rth_open.hour, minute=session.rth_open.minute, second=0, microsecond=0)
    if here > candidate or here.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    # Bound the search to avoid infinite loops on misconfigured clocks.
    for _ in range(14):
        if candidate.weekday() < 5 and not is_market_closed(session.code, candidate.date()):
            return candidate.astimezone(timezone.utc)
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _ensure_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)
