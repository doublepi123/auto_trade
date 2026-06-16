from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import TradeNoteAnalytics, TradeNoteOut, TradeNotePage, TradeNoteUpsert
from app.services.trade_note_service import (
    OrderNotFoundError,
    TradeNoteNotFoundError,
    TradeNoteService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/trade-notes",
    tags=["trade-notes"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=TradeNotePage)
def list_trade_notes(
    symbol: str | None = Query(default=None, description="Filter by symbol"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
) -> TradeNotePage:
    return TradeNoteService(db).list_notes(symbol=symbol, page=page, page_size=page_size)


@router.get("/analytics", response_model=TradeNoteAnalytics)
def get_trade_note_analytics(db=Depends(get_db)) -> TradeNoteAnalytics:
    return TradeNoteService(db).analytics()


@router.get("/{order_id}", response_model=TradeNoteOut)
def get_trade_note(order_id: int, db=Depends(get_db)) -> TradeNoteOut:
    try:
        return TradeNoteService(db).get_note(order_id)
    except TradeNoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trade note not found") from exc


@router.put("/{order_id}", response_model=TradeNoteOut)
def upsert_trade_note(
    order_id: int,
    payload: TradeNoteUpsert,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> TradeNoteOut:
    """Create or update the journal note for an order (one note per order)."""
    actor_hash, source_ip = extract_actor(request)
    try:
        note = TradeNoteService(db).upsert_note(order_id, payload)
    except OrderNotFoundError:
        audit.record(
            "TRADE_NOTE_UPSERT",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"order_id": order_id, "order_found": False},
            result="NOT_FOUND",
        )
        raise HTTPException(status_code=404, detail="order not found")
    audit.record(
        "TRADE_NOTE_UPSERT",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={
            "order_id": order_id,
            "symbol": note.symbol,
            "tags": note.tags,
            "rating": note.rating,
            "note_len": len(note.note),
        },
        result="SUCCESS",
    )
    return note


@router.delete("/{order_id}", status_code=204)
def delete_trade_note(
    order_id: int,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """Idempotent delete: 204 whether or not a note existed."""
    actor_hash, source_ip = extract_actor(request)
    existed = TradeNoteService(db).delete_note(order_id)
    audit.record(
        "TRADE_NOTE_DELETE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"order_id": order_id, "existed": existed},
        result="SUCCESS",
    )
    return Response(status_code=204)
