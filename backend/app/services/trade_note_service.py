"""Trade Journal — post-trade notes / tags / rating attached to an order."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import OrderRecord, TradeNote
from app.schemas import (
    TradeNoteAnalytics,
    TradeNoteOut,
    TradeNotePage,
    TradeNoteTagCount,
    TradeNoteUpsert,
)


class TradeNoteNotFoundError(LookupError):
    """Raised when a requested trade note does not exist."""


class OrderNotFoundError(LookupError):
    """Raised when the order a note is being attached to does not exist."""


class TradeNoteService:
    """CRUD over ``trade_notes``.

    One note per order (unique ``order_id``); PUT is an upsert. ``tags`` are
    stored as a JSON text column (the project stores all JSON-like data as
    Text rather than the SQLAlchemy JSON type).
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def list_notes(
        self,
        *,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> TradeNotePage:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        clauses = [TradeNote.symbol == symbol] if symbol else []
        count_stmt = select(func.count()).select_from(TradeNote)
        list_stmt = select(TradeNote).order_by(desc(TradeNote.updated_at))
        if clauses:
            count_stmt = count_stmt.where(*clauses)
            list_stmt = list_stmt.where(*clauses)
        total = self._db.scalar(count_stmt) or 0
        list_stmt = list_stmt.limit(page_size).offset((page - 1) * page_size)
        rows = list(self._db.scalars(list_stmt))
        return TradeNotePage(
            items=[self._to_out(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def analytics(self) -> TradeNoteAnalytics:
        rows = list(self._db.scalars(select(TradeNote)))
        ratings = [r.rating for r in rows if r.rating is not None]
        tag_counts: dict[str, int] = {}
        symbols: set[str] = set()
        for r in rows:
            for tag in _parse_tags(r.tags_json):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if r.symbol:
                symbols.add(r.symbol)
        top_tags = [
            TradeNoteTagCount(tag=t, count=c)
            for t, c in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ]
        return TradeNoteAnalytics(
            total=len(rows),
            rated_count=len(ratings),
            avg_rating=(sum(ratings) / len(ratings)) if ratings else None,
            rating_distribution={i: sum(1 for rt in ratings if rt == i) for i in range(1, 6)},
            top_tags=top_tags,
            distinct_symbols=len(symbols),
        )

    def get_note(self, order_id: int) -> TradeNoteOut:
        note = self._get(order_id)
        if note is None:
            raise TradeNoteNotFoundError(str(order_id))
        return self._to_out(note)

    def upsert_note(self, order_id: int, payload: TradeNoteUpsert) -> TradeNoteOut:
        order = self._db.get(OrderRecord, order_id)
        if order is None:
            raise OrderNotFoundError(str(order_id))
        note = self._get(order_id)
        if note is None:
            note = TradeNote(order_id=order_id, symbol=order.symbol or "")
            self._db.add(note)
        note.symbol = order.symbol or ""
        note.note = payload.note
        note.tags_json = json.dumps(payload.tags, ensure_ascii=False)
        note.rating = payload.rating
        self._db.commit()
        self._db.refresh(note)
        return self._to_out(note)

    def delete_note(self, order_id: int) -> bool:
        note = self._get(order_id)
        if note is None:
            return False
        self._db.delete(note)
        self._db.commit()
        return True

    def _get(self, order_id: int) -> TradeNote | None:
        return self._db.scalar(select(TradeNote).where(TradeNote.order_id == order_id))

    @staticmethod
    def _to_out(note: TradeNote) -> TradeNoteOut:
        return TradeNoteOut(
            id=note.id,
            order_id=note.order_id,
            symbol=note.symbol,
            note=note.note,
            tags=_parse_tags(note.tags_json),
            rating=note.rating,
            created_at=note.created_at,
            updated_at=note.updated_at,
        )


def _parse_tags(raw: Any) -> list[str]:
    """Defensive parse of the tags JSON column; never raises."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(tag) for tag in parsed]
