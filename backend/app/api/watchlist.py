from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import List, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.models import StrategyConfig, WatchlistScore
from app.runner import get_runner
from app.schemas import (
    MessageResponse,
    WatchlistItemResponse,
    WatchlistItemSchema,
    WatchlistQuote,
    WatchlistScoreListResponse,
    WatchlistScoreRequest,
    WatchlistScoreResponse,
    WatchlistScoredSnapshot,
    WatchlistSnapshot,
)
from app.services.watchlist_quant_service import WatchlistQuantService
from app.services.watchlist_service import WatchlistService
from app.services.watchlist_score_service import WatchlistScoreService

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"], dependencies=[Depends(require_api_key())])
logger = logging.getLogger("auto_trade.watchlist")
_MIN_SWITCH_HALF_WIDTH_PCT = 0.005
_MAX_SWITCH_HALF_WIDTH_PCT = 0.04


def _trading_symbol(db: Session) -> str:
    strategy = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    return strategy.symbol if strategy is not None else ""


def _item_response(
    item: object,
    *,
    trading_symbol: str,
) -> WatchlistItemResponse:
    response = WatchlistItemResponse.model_validate(item)
    return response.model_copy(
        update={"is_trading_target": response.symbol == trading_symbol},
    )


def _scaled_switch_interval(
    current: object,
    *,
    reference_price: float,
) -> tuple[float, float]:
    if not math.isfinite(reference_price) or reference_price <= 0:
        raise ValueError("new trading symbol has no valid reference price")
    buy_low = float(getattr(current, "buy_low", 0) or 0)
    sell_high = float(getattr(current, "sell_high", 0) or 0)
    if (
        math.isfinite(buy_low)
        and math.isfinite(sell_high)
        and 0 < buy_low < sell_high
    ):
        half_width_pct = (sell_high - buy_low) / (
            sell_high + buy_low
        )
    else:
        half_width_pct = _MIN_SWITCH_HALF_WIDTH_PCT
    bounded_half_width = min(
        _MAX_SWITCH_HALF_WIDTH_PCT,
        max(_MIN_SWITCH_HALF_WIDTH_PCT, half_width_pct),
    )
    return (
        round(reference_price * (1 - bounded_half_width), 6),
        round(reference_price * (1 + bounded_half_width), 6),
    )


@router.get("", response_model=List[WatchlistItemResponse])
def get_watchlist(db: Session = Depends(get_db)) -> List[WatchlistItemResponse]:
    svc = WatchlistService(db)
    items = svc.list_items()
    trading_symbol = _trading_symbol(db)
    return [
        _item_response(item, trading_symbol=trading_symbol)
        for item in items
    ]


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
    return _item_response(item, trading_symbol=_trading_symbol(db))


@router.post(
    "/quant-rank",
    response_model=WatchlistScoreListResponse,
)
def rank_watchlist_quantitatively(
    ttl_minutes: int = Query(default=360, ge=1, le=1_440),
    db: Session = Depends(get_db),
) -> WatchlistScoreListResponse:
    items = WatchlistService(db).list_items()
    if not items:
        return WatchlistScoreListResponse(scores=[])
    try:
        broker = get_runner().broker
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="runner not initialized",
        ) from None
    try:
        rows = WatchlistQuantService(db, broker).score_items(
            items,
            ttl_minutes=ttl_minutes,
        )
    except Exception:
        logger.exception("failed to rank watchlist quantitatively")
        raise HTTPException(
            status_code=503,
            detail="broker market data unavailable",
        ) from None
    score_service = WatchlistScoreService(db)
    responses: list[WatchlistScoreResponse] = []
    for row in rows:
        response = WatchlistScoreResponse.model_validate(row)
        response.is_stale = not score_service.is_fresh(row)
        responses.append(response)
    return WatchlistScoreListResponse(scores=responses)


@router.get("/snapshots", response_model=List[WatchlistSnapshot], dependencies=[Depends(require_api_key())])
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
    trading_symbol = _trading_symbol(db)
    snapshots: list[WatchlistSnapshot] = []
    for item in items:
        quote = quote_by_symbol.get(item.symbol)
        if quote is None:
            continue
        timestamp = quote.timestamp
        if isinstance(timestamp, datetime):
            timestamp_str = timestamp.isoformat()
        else:
            timestamp_str = str(timestamp)
        snapshots.append(
            WatchlistSnapshot(
                symbol=item.symbol,
                market=item.market,
                alias=item.alias,
                is_trading_target=item.symbol == trading_symbol,
                last_price=float(quote.last_price),
                bid=float(quote.bid),
                ask=float(quote.ask),
                timestamp=timestamp_str,
            )
        )
    return snapshots


@router.get(
    "/scored-snapshots",
    response_model=List[WatchlistScoredSnapshot],
    dependencies=[Depends(require_api_key())],
)
def get_watchlist_scored_snapshots(
    db: Session = Depends(get_db),
) -> List[WatchlistScoredSnapshot]:
    """Snapshots enriched with the latest cached LLM score.

    Symbols without a fresh cached score are still included (with score=0
    and is_stale=True) so the UI can render the full list while scoring runs.
    Sorted by score desc, then by symbol asc for stable ordering on ties.
    """
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
    trading_symbol = _trading_symbol(db)

    score_svc = WatchlistScoreService(db)
    latest_scores = {row.symbol: row for row in score_svc.list_latest_per_symbol()}

    rows: list[tuple[float, WatchlistScoredSnapshot]] = []
    for item in items:
        quote = quote_by_symbol.get(item.symbol)
        if quote is None:
            continue
        timestamp = quote.timestamp
        timestamp_str = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        score_row = latest_scores.get(item.symbol)
        score_value = float(score_row.score) if score_row is not None else 0.0
        is_stale = score_row is None or not score_svc.is_fresh(score_row)
        rows.append((
            score_value,
            WatchlistScoredSnapshot(
                symbol=item.symbol,
                market=item.market,
                alias=item.alias,
                is_trading_target=item.symbol == trading_symbol,
                last_price=float(quote.last_price),
                bid=float(quote.bid),
                ask=float(quote.ask),
                timestamp=timestamp_str,
                score=score_value,
                is_stale=is_stale,
            ),
        ))

    # Sort by score desc, then by symbol asc for stable ordering on ties.
    rows.sort(key=lambda pair: (-pair[0], pair[1].symbol))
    return [snapshot for _, snapshot in rows]


@router.post(
    "/score",
    response_model=WatchlistScoreResponse,
    dependencies=[Depends(require_api_key())],
)
def post_watchlist_score(
    payload: WatchlistScoreRequest,
    db: Session = Depends(get_db),
) -> WatchlistScoreResponse:
    """Score a single watchlist symbol via the LLM advisor and cache the
    result. Returns a neutral fallback row when the advisor is unavailable
    or the response is malformed — see :class:`WatchlistScoreService`."""
    svc = WatchlistScoreService(db)
    row = svc.score_from_llm_or_fallback(
        symbol=payload.symbol,
        market=payload.market,
        ttl_minutes=payload.ttl_minutes,
    )
    response = WatchlistScoreResponse.model_validate(row)
    response.is_stale = not svc.is_fresh(row)
    return response


@router.get(
    "/scores",
    response_model=WatchlistScoreListResponse,
    dependencies=[Depends(require_api_key())],
)
def get_watchlist_scores(db: Session = Depends(get_db)) -> WatchlistScoreListResponse:
    """List primary quant scores and supplementary AI reviews."""
    svc = WatchlistScoreService(db)
    family_rows = svc.list_latest_per_symbol_and_family()

    def responses_for(
        rows: Sequence[WatchlistScore],
    ) -> list[WatchlistScoreResponse]:
        responses: list[WatchlistScoreResponse] = []
        for row in rows:
            response = WatchlistScoreResponse.model_validate(row)
            response.is_stale = not svc.is_fresh(row)
            responses.append(response)
        return responses

    quant_scores = [
        row
        for row in family_rows
        if svc.source_family(row.source) == "quant"
    ]
    reviews = [
        row
        for row in family_rows
        if svc.source_family(row.source) == "review"
    ]
    return WatchlistScoreListResponse(
        scores=responses_for(quant_scores),
        reviews=responses_for(reviews),
    )


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
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> WatchlistItemResponse:
    from app.api.strategy import update_strategy_with_runtime_reload
    from app.services.strategy_service import StrategyService

    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    diff: dict[str, object] = {}
    svc = WatchlistService(db)
    item = svc.get_item(item_id)
    if item is None:
        result = "FAILED"
        diff = {"detail": "Watchlist item not found"}
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    strategy_svc = StrategyService(db)
    current = strategy_svc.get_config()
    if current.symbol != item.symbol or current.market != item.market:
        runner = get_runner()
        try:
            quote = next(
                (
                    row
                    for row in runner.broker.get_quotes([item.symbol])
                    if row.symbol == item.symbol
                ),
                None,
            )
        except Exception:
            logger.exception(
                "failed to quote new watchlist trading symbol %s",
                item.symbol,
            )
            raise HTTPException(
                status_code=503,
                detail="new trading symbol quote unavailable",
            ) from None
        if quote is None:
            raise HTTPException(
                status_code=503,
                detail="new trading symbol quote unavailable",
            )
        bid = float(quote.bid)
        ask = float(quote.ask)
        last_price = float(quote.last_price)
        reference_price = (
            (bid + ask) / 2
            if (
                math.isfinite(bid)
                and math.isfinite(ask)
                and 0 < bid <= ask
            )
            else last_price
        )
        try:
            buy_low, sell_high = _scaled_switch_interval(
                current,
                reference_price=reference_price,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc
        _, diff = update_strategy_with_runtime_reload(
            strategy_svc,
            current,
            {
                "symbol": item.symbol,
                "market": item.market,
                "buy_low": buy_low,
                "sell_high": sell_high,
            },
        )
    item = svc.get_item(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Watchlist item not found",
        )
    try:
        return _item_response(item, trading_symbol=_trading_symbol(db))
    finally:
        if diff:
            summary: dict[str, object] = {"changed": diff, "source": "watchlist_set_trading"}
            audit.record(
                "STRATEGY_UPDATE",
                severity="INFO",
                actor_hash=actor_hash,
                source_ip=source_ip,
                request_summary=summary,
                result=result,
            )


@router.get("/quotes", response_model=List[WatchlistQuote], dependencies=[Depends(require_api_key())])
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
