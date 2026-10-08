"""Live risk-day boundary: the US risk day starts at the overnight open.

The daily RISK day — ``daily_pnl``, ``consecutive_losses``, the daily loss
limit, and their reconciliation replay — runs from 20:00 America/New_York
(the US overnight session open) to 20:00 ET, instead of ET midnight to ET
midnight. Owner decision, 2026-10-08.

This is an accounting-bucket rule, not a trading permission: it deliberately
does NOT skip weekends or holidays and does not consult the holiday calendar,
half-days, or the overnight session flag. Friday 20:00 ET maps to Saturday.

Only the literal ``US`` market gets the overnight boundary. ``HK`` and any
other market keep exactly what :func:`app.core.market_calendar.trade_day_for`
returns, so the exchange-local calendar day remains untouched for research,
reporting, and session semantics.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from app.core.market_calendar import get_session, trade_day_for

_US_RISK_DAY_START = time(20, 0)
"""ET wall-clock time at which the next US risk day begins."""


def risk_day_for(market: str, instant: datetime | None = None) -> date:
    """Return the live risk day for ``market`` at ``instant``.

    ``instant=None`` means now (UTC-aware); a naive datetime is interpreted
    as UTC, mirroring :func:`app.core.market_calendar.trade_day_for`.

    US: convert to ET wall-clock via the session timezone (DST handled by
    ``ZoneInfo``, never a fixed UTC offset). At or after 20:00:00 local the
    instant already belongs to the NEXT calendar day's risk bucket; before
    20:00 it belongs to the local calendar date.
    """
    if (market or "US").upper() != "US":
        return trade_day_for(market, instant)
    session = get_session("US")
    moment = instant or datetime.now(timezone.utc)
    local = session.local(moment)
    if local.time() >= _US_RISK_DAY_START:
        return local.date() + timedelta(days=1)
    return local.date()
