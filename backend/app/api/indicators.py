from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.core.broker import BrokerGateway
from app.core.market_calendar import market_for_symbol
from app.database import get_db
from app.models import StrategyConfig
from app.schemas import (
    IndicatorsResponse,
    MacdValue,
    MultiTimeframeSchema,
    SentimentValue,
    VolumeAnalysisSchema,
)
from app.services.data_aggregator import DataAggregator

router = APIRouter(prefix="/api", tags=["indicators"], dependencies=[Depends(require_api_key())])
logger = logging.getLogger("auto_trade.indicators")

_MACD_KEYS = frozenset({"macd", "signal", "histogram"})
_VOLUME_KEYS = frozenset({"avg_volume", "volume_ratio", "trend"})
_SENTIMENT_KEYS = frozenset({"sentiment", "score", "description"})
_MULTI_TIMEFRAME_KEYS = frozenset({"daily_trend", "minute_trend", "aligned", "description"})
_NestedModel = TypeVar("_NestedModel", bound=BaseModel)


def _complete_dict(value: object, required_keys: frozenset[str]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if any(key not in value or value[key] is None for key in required_keys):
        return None
    return dict(value)


def _nested_model_or_none(
    model_type: type[_NestedModel],
    value: object,
    required_keys: frozenset[str],
) -> _NestedModel | None:
    payload = _complete_dict(value, required_keys)
    if payload is None:
        return None
    try:
        return model_type.model_validate(payload)
    except ValidationError:
        logger.warning("invalid indicator payload for %s: %.200r", model_type.__name__, value)
        return None


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
    symbol: str | None = Query(default=None, pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    db: Session = Depends(get_db),
    broker: BrokerGateway | None = Depends(get_indicator_broker),
) -> IndicatorsResponse:
    config = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    resolved_symbol = (symbol.strip().upper() if symbol else None) or (config.symbol if config else None)
    if not resolved_symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    market = market_for_symbol(resolved_symbol)

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
        macd=_nested_model_or_none(MacdValue, data.get("macd"), _MACD_KEYS),
        volume_analysis=_nested_model_or_none(
            VolumeAnalysisSchema,
            data.get("volume_analysis"),
            _VOLUME_KEYS,
        ),
        sentiment=_nested_model_or_none(SentimentValue, data.get("sentiment"), _SENTIMENT_KEYS),
        multi_timeframe=_nested_model_or_none(
            MultiTimeframeSchema,
            data.get("multi_timeframe"),
            _MULTI_TIMEFRAME_KEYS,
        ),
        bb_upper=data.get("bb_upper"),
        bb_middle=data.get("bb_middle"),
        bb_lower=data.get("bb_lower"),
    )
