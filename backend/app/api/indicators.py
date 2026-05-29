from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.broker import BrokerGateway
from app.database import get_db
from app.models import StrategyConfig
from app.schemas import IndicatorsResponse
from app.services.data_aggregator import DataAggregator

router = APIRouter(prefix="/api", tags=["indicators"])
logger = logging.getLogger("auto_trade.indicators")


def get_indicator_broker() -> BrokerGateway | None:
    """Reuse the running app's shared broker; None if runner unavailable."""
    try:
        from app.runner import get_runner

        return get_runner().broker
    except Exception:
        logger.warning("indicator broker unavailable; falling back to None")
        return None


@router.get("/indicators", response_model=IndicatorsResponse)
def get_indicators(
    symbol: str | None = Query(default=None),
    db: Session = Depends(get_db),
    broker: BrokerGateway | None = Depends(get_indicator_broker),
) -> IndicatorsResponse:
    config = db.query(StrategyConfig).first()
    resolved_symbol = symbol or (config.symbol if config else None)
    if not resolved_symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    market = config.market if config else "US"

    aggregator = DataAggregator(broker=broker)
    data = aggregator.fetch_market_data(resolved_symbol, market)
    if not data.get("daily_candles"):
        return IndicatorsResponse(available=False, symbol=resolved_symbol, market=market)

    return IndicatorsResponse(
        available=True,
        symbol=resolved_symbol,
        market=market,
        atr=data.get("atr"),
        rsi=data.get("rsi"),
        macd=data.get("macd"),
        volume_analysis=data.get("volume_analysis"),
        sentiment=data.get("sentiment"),
        multi_timeframe=data.get("multi_timeframe"),
        bb_upper=data.get("bb_upper"),
        bb_middle=data.get("bb_middle"),
        bb_lower=data.get("bb_lower"),
    )
