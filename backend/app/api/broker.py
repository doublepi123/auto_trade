from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.runner import get_runner
from app.schemas import BacktestPricePoint, BrokerCandlesResponse

router = APIRouter(
    prefix="/api/broker",
    tags=["broker"],
    dependencies=[Depends(require_api_key())],
)

_ALLOWED_PERIODS = {"DAY", "WEEK", "MIN_1", "MIN_5", "MIN_15", "MIN_30", "MIN_60"}


def _is_valid_bar(o: float, h: float, l: float, c: float) -> bool:
    return min(o, h, l, c) > 0 and h >= l and h >= max(o, c) and l <= min(o, c)


def _to_csv_utc(ts: datetime) -> str:
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/candles", response_model=BrokerCandlesResponse)
def get_broker_candles(
    symbol: str = Query(..., description="e.g. AAPL.US", min_length=2, max_length=50),
    period: str = Query(default="DAY"),
    count: int = Query(default=60, ge=1, le=1000),
    db=Depends(get_db),  # noqa: ARG001 — kept for DI consistency with sibling routers
) -> BrokerCandlesResponse:
    """Fetch recent candlesticks from the broker for backtest loading.

    Broker-unavailable / fetch failures -> 503. Invalid bars (non-positive or
    inconsistent OHLC) are dropped so the result is always backtest-ready.
    """
    period = period.strip().upper().replace("-", "_")
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(status_code=422, detail=f"unsupported period: {period}")

    runner = get_runner()
    broker = getattr(runner, "broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="broker is not available")
    try:
        candles = broker.get_candlesticks(symbol, period, count)
    except Exception as exc:  # noqa: BLE001 — surface any broker failure as 503
        raise HTTPException(status_code=503, detail="failed to fetch candles") from exc

    bars: list[BacktestPricePoint] = []
    for c in candles:
        o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        if not _is_valid_bar(o, h, l, cl):
            continue
        bars.append(BacktestPricePoint(
            timestamp=c.timestamp, open=o, high=h, low=l, close=cl, volume=float(c.volume),
        ))

    header = "timestamp,open,high,low,close,volume"
    rows = [f"{_to_csv_utc(b.timestamp)},{b.open},{b.high},{b.low},{b.close},{b.volume}" for b in bars]
    csv_text = header + ("\n" + "\n".join(rows) if rows else "")

    return BrokerCandlesResponse(
        symbol=symbol,
        period=period,
        count=len(bars),
        bars=bars,
        csv_text=csv_text,
    )
