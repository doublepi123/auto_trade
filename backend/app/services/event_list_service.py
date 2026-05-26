from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import AuditLog, TradeEvent
from app.schemas import TimelineEventResponse
from app.services.trade_event_service import decode_event_payload



SourceFilter = Literal["trade", "audit", "all"]


def _audit_payload(request_summary: str) -> dict[str, Any]:
    if not request_summary or not request_summary.strip():
        return {}
    try:
        parsed = json.loads(request_summary)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": request_summary}


def _trade_row_to_out(event: TradeEvent) -> TimelineEventResponse:
    return TimelineEventResponse(
        source="trade",
        id=event.id,
        event_type=event.event_type,
        symbol=event.symbol,
        broker_order_id=event.broker_order_id,
        side=event.side,
        status=event.status,
        message=event.message,
        payload=decode_event_payload(event.payload_json),
        created_at=event.created_at,
        actor_hash=None,
        source_ip=None,
        severity=None,
        result=None,
    )


def _audit_row_to_out(row: AuditLog) -> TimelineEventResponse:
    return TimelineEventResponse(
        source="audit",
        id=row.id,
        event_type=row.action,
        symbol="",
        broker_order_id="",
        side="",
        status="",
        message=row.request_summary[:200] if row.request_summary else "",
        payload=_audit_payload(row.request_summary),
        created_at=row.created_at,
        actor_hash=row.actor_hash,
        source_ip=row.source_ip,
        severity=row.severity,
        result=row.result,
    )


def _to_utc_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _sort_key(item: TimelineEventResponse) -> tuple:
    return (-_to_utc_timestamp(item.created_at), item.source, -item.id)


def list_timeline_events(
    db: Session,
    *,
    source: SourceFilter = "all",
    event_types: list[str] | None,
    symbol: str | None,
    page: int,
    page_size: int,
) -> tuple[list[TimelineEventResponse], int]:
    """Merge trade_events and audit_logs for the timeline API (spec §5.2)."""
    et = [e.strip() for e in (event_types or []) if e and e.strip()]

    trade_total = 0
    audit_total = 0
    trade_rows: list[TradeEvent] = []
    audit_rows: list[AuditLog] = []

    fetch_n = page * page_size

    if source in ("trade", "all"):
        tq = db.query(TradeEvent)
        if symbol:
            tq = tq.filter(TradeEvent.symbol == symbol)
        if et:
            tq = tq.filter(TradeEvent.event_type.in_(et))
        trade_total = tq.count()
        trade_rows = (
            tq.order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc()).limit(fetch_n).all()
        )

    if source in ("audit", "all") and not symbol:
        aq = db.query(AuditLog)
        if et:
            aq = aq.filter(AuditLog.action.in_(et))
        audit_total = aq.count()
        audit_rows = aq.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(fetch_n).all()

    total = trade_total + audit_total

    merged = [_trade_row_to_out(r) for r in trade_rows] + [_audit_row_to_out(r) for r in audit_rows]
    merged.sort(key=_sort_key)

    start = (page - 1) * page_size
    return merged[start : start + page_size], total
