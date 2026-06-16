from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.holiday_calendar import (
    COVERAGE_END_YEAR,
    COVERAGE_START_YEAR,
    closure_label,
    is_half_day,
    list_closures,
)
from app.core.market_calendar import (
    get_session,
    is_trading_hours,
    market_for_symbol,
    next_session_open,
    session_status,
    trade_day_for,
)
from app.api.auth import require_api_key
from app.schemas import MarketSessionStatus

router = APIRouter(prefix="/api", tags=["calendar"])


@router.get(
    "/calendar/today",
    dependencies=[Depends(require_api_key())],
)
def calendar_today(
    market: str = Query(
        "US",
        pattern="^(US|HK)$",
        description="Market code: US or HK",
    ),
) -> dict[str, object]:
    """Snapshot of the current trading-session state for a market.

    Returns whether the exchange is currently inside RTH, the trading day
    in the exchange's local clock, the next session open, and a closure
    label if today is a holiday. This endpoint is read-only and does not
    consult broker quote state.
    """
    session = get_session(market)
    now = datetime.now(timezone.utc)
    session_code = market.upper()
    day = trade_day_for(session_code, now)
    label = closure_label(session_code, day)
    return {
        "market": session_code,
        "now_utc": now.isoformat(),
        "trade_day": day.isoformat(),
        "is_rth": is_trading_hours(session_code, now),
        "is_holiday": label is not None,
        "closure_label": label,
        "next_session_open_utc": next_session_open(session_code, now).isoformat(),
        "session": {
            "timezone": str(session.timezone),
            "rth_open": session.rth_open.isoformat(),
            "rth_close": session.rth_close.isoformat(),
        },
    }


@router.get(
    "/calendar/session",
    dependencies=[Depends(require_api_key())],
    response_model=MarketSessionStatus,
)
def calendar_session(
    symbol: str = Query(default="", max_length=50),
) -> MarketSessionStatus:
    """Granular session phase (pre/RTH/post/lunch/closed) for a symbol's market.

    Infers the market from the symbol suffix (``.HK`` -> HK, else US). Read-only.
    """
    market = market_for_symbol(symbol) if symbol else "US"
    now = datetime.now(timezone.utc)
    session = get_session(market)
    status = session_status(market, now)
    return MarketSessionStatus(
        market=market,
        symbol=symbol,
        status=status,
        is_trading=(status == "rth"),
        local_time=session.local(now).strftime("%Y-%m-%d %H:%M:%S %Z"),
        utc_time=now,
        next_open=next_session_open(market, now),
    )


@router.get(
    "/calendar/closures",
    dependencies=[Depends(require_api_key())],
)
def calendar_closures(
    market: str = Query(
        "US",
        pattern="^(US|HK)$",
        description="Market code: US or HK",
    ),
    year: int = Query(
        ...,
        ge=COVERAGE_START_YEAR,
        le=COVERAGE_END_YEAR,
        description=(
            f"Calendar year — must be within {COVERAGE_START_YEAR}-{COVERAGE_END_YEAR} "
            "for accurate results (the static closure data does not cover other ranges)."
        ),
    ),
) -> dict[str, object]:
    """List all market closures for a given year within the calendar's coverage window."""
    items = list_closures(market, year)
    return {
        "market": market.upper(),
        "year": year,
        "count": len(items),
        "items": items,
        "coverage_end_year": COVERAGE_END_YEAR,
    }


@router.get(
    "/calendar/lookup",
    dependencies=[Depends(require_api_key())],
)
def calendar_lookup(
    symbol: str = Query(
        ...,
        pattern=r"^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$",
        description="Symbol, e.g. AAPL.US or 700.HK",
    ),
    instant: str | None = Query(None, description="ISO 8601 instant; defaults to now"),
) -> dict[str, object]:
    """Resolve a symbol + instant to its trade day, market, and RTH status."""
    market = market_for_symbol(symbol)
    if instant is not None:
        try:
            ts = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid instant: {exc}") from exc
    else:
        ts = datetime.now(timezone.utc)
    day = trade_day_for(market, ts)
    return {
        "symbol": symbol,
        "market": market,
        "instant_utc": ts.isoformat(),
        "trade_day": day.isoformat(),
        "is_rth": is_trading_hours(market, ts),
        "is_half_day": is_half_day(market, day),
        "closure_label": closure_label(market, day),
    }

