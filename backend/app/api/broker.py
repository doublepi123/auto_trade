from __future__ import annotations

import math
from decimal import Decimal
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.core.market_calendar import is_trading_hours
from app.database import get_db
from app.runner import get_runner
from app.schemas import (
    BacktestPricePoint,
    BrokerBuyingPowerResponse,
    BrokerCandlesResponse,
)

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

    try:
        runner = get_runner()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="runner is not available",
        ) from exc
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


@router.get(
    "/buying-power",
    response_model=BrokerBuyingPowerResponse,
)
def get_broker_buying_power(
    symbol: str = Query(..., min_length=2, max_length=50),
    side: Literal["BUY", "SELL"] = Query(default="BUY"),
    price: Optional[float] = Query(
        default=None,
        gt=0,
        allow_inf_nan=False,
    ),
) -> BrokerBuyingPowerResponse:
    """Estimate broker-authoritative capacity without submitting an order."""

    normalized_symbol = symbol.strip().upper()
    market_suffix = normalized_symbol.rsplit(".", 1)[-1]
    market: Literal["US", "HK"]
    currency: Literal["USD", "HKD"]
    if market_suffix == "US":
        market = "US"
        currency = "USD"
    elif market_suffix == "HK":
        market = "HK"
        currency = "HKD"
    else:
        raise HTTPException(
            status_code=422,
            detail="buying-power preflight supports only .US and .HK symbols",
        )

    try:
        runner = get_runner()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="runner is not available",
        ) from exc
    broker = getattr(runner, "broker", None)
    if broker is None:
        raise HTTPException(
            status_code=503,
            detail="broker is not available",
        )

    try:
        effective_price = float(price or 0.0)
        if effective_price <= 0.0:
            quote = broker.get_quote(normalized_symbol)
            effective_price = float(
                quote.ask if side == "BUY" else quote.bid
            )
            if effective_price <= 0.0:
                effective_price = float(quote.last_price)
        if not math.isfinite(effective_price) or effective_price <= 0.0:
            raise ValueError("broker returned no positive executable price")

        price_decimal = Decimal(str(effective_price))
        available_cash = Decimal(str(broker.get_cash(currency)))
        max_quantity = Decimal(str(
            broker.estimate_margin_max_quantity(
                normalized_symbol,
                side,
                price_decimal,
                currency,
            )
        ))
        if (
            not available_cash.is_finite()
            or not max_quantity.is_finite()
            or max_quantity < 0
        ):
            raise ValueError("broker returned invalid buying-power values")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="failed to estimate broker buying power",
        ) from exc

    estimated_at = datetime.now(timezone.utc)
    return BrokerBuyingPowerResponse(
        symbol=normalized_symbol,
        side=side,
        market=market,
        currency=currency,
        price=effective_price,
        available_cash=float(available_cash),
        max_quantity=float(max_quantity),
        buying_power=float(max_quantity * price_decimal),
        is_trading_hours=is_trading_hours(market, estimated_at),
        estimated_at=estimated_at,
    )
