from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.core.broker import BrokerGateway
from app.database import get_db
from app.runner import get_runner
from app.models import StrategyConfig
from app.schemas import WatchlistItemResponse, WatchlistItemSchema, MessageResponse, WatchlistQuote, WatchlistSnapshot
from app.services.watchlist_service import WatchlistService

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])
logger = logging.getLogger("auto_trade.watchlist")


@router.get("", response_model=List[WatchlistItemResponse])
def get_watchlist(db: Session = Depends(get_db)) -> List[WatchlistItemResponse]:
    svc = WatchlistService(db)
    items = svc.list_items()
    return [WatchlistItemResponse.model_validate(item) for item in items]


@router.post("", response_model=WatchlistItemResponse, dependencies=[Depends(require_api_key())])
def add_watchlist_item(
    payload: WatchlistItemSchema,
    db: Session = Depends(get_db),
) -> WatchlistItemResponse:
    svc = WatchlistService(db)
    try:
        item = svc.add_item(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WatchlistItemResponse.model_validate(item)



@router.get("/snapshots", response_model=List[WatchlistSnapshot])
def get_watchlist_snapshots(
    db: Session = Depends(get_db),
) -> List[WatchlistSnapshot]:
    svc = WatchlistService(db)
    items = svc.list_items()
    if not items:
        return []

    symbols = [item.symbol for item in items]
    try:
        broker = get_runner().broker
    except Exception:
        raise HTTPException(status_code=503, detail="runner not initialized") from None
    try:
        quotes = broker.get_quotes(symbols)
    except Exception:
        logger.exception("failed to fetch watchlist snapshots")
        raise HTTPException(status_code=503, detail="broker quote unavailable") from None

    quote_by_symbol = {quote.symbol: quote for quote in quotes}
    strategy = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    trading_symbol = strategy.symbol if strategy is not None else ""
    snapshots: list[WatchlistSnapshot] = []
    for item in items:
        quote = quote_by_symbol.get(item.symbol)
        if quote is None:
            continue
        timestamp = quote.timestamp
        snapshots.append(
            WatchlistSnapshot(
                symbol=item.symbol,
                market=item.market,
                alias=item.alias,
                is_trading_target=item.symbol == trading_symbol,
                last_price=float(quote.last_price),
                bid=float(quote.bid),
                ask=float(quote.ask),
                timestamp=timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
            )
        )
    return snapshots
@router.delete("/{item_id}", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def remove_watchlist_item(
    item_id: int,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = WatchlistService(db)
    removed = svc.remove_item(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return MessageResponse(message="removed")


@router.post("/{item_id}/set-trading", response_model=WatchlistItemResponse, dependencies=[Depends(require_api_key())])
def set_trading_symbol(
    item_id: int,
    db: Session = Depends(get_db),
) -> WatchlistItemResponse:
    from app.api.strategy import _reload_strategy_after_save
    from app.services.strategy_service import StrategyService

    svc = WatchlistService(db)
    try:
        item = svc.set_trading_symbol(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Sync active symbol to StrategyConfig via StrategyService for audit diff tracking
    strategy_svc = StrategyService(db)
    current = strategy_svc.get_config()
    if current.symbol != item.symbol or current.market != item.market:
        strategy_svc.update_config({"symbol": item.symbol, "market": item.market})
        _reload_strategy_after_save()

    return WatchlistItemResponse.model_validate(item)


@router.get("/quotes", response_model=List[WatchlistQuote])
def get_watchlist_quotes(
    db: Session = Depends(get_db),
) -> List[WatchlistQuote]:
    svc = WatchlistService(db)
    items = svc.list_items()
    if not items:
        return []

    symbols = [item.symbol for item in items]
    try:
        broker = get_runner().broker
    except Exception:
        raise HTTPException(status_code=503, detail="runner not initialized") from None
    try:
        quotes = broker.get_quotes(symbols)
        return [
            WatchlistQuote(
                symbol=q.symbol,
                last_price=q.last_price,
                bid=q.bid,
                ask=q.ask,
                timestamp=q.timestamp,
            )
            for q in quotes
        ]
    except Exception:
        logger.exception("failed to fetch watchlist quotes")
        raise HTTPException(status_code=503, detail="broker quote unavailable") from None
