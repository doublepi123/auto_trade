from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import AuditLog, LLMInteraction, RiskEvent, TradeEvent
from app.schemas import TimelineEventResponse
from app.services.trade_event_service import decode_event_payload



SourceFilter = Literal["trade", "audit", "llm", "risk", "all"]


def _audit_payload(request_summary: str) -> dict[str, Any]:
    if not request_summary or not request_summary.strip():
        return {}
    try:
        parsed = json.loads(request_summary)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": request_summary}


_EVENT_SEVERITY_MAP: dict[str, str] = {
    "RISK_PAUSED": "WARNING",
    "RISK_AUTO_RESUMED": "INFO",
    "ORDER_REJECTED": "WARNING",
    "ORDER_FAILED": "WARNING",
    "ORDER_TIMEOUT": "WARNING",
    "ORDER_PERSISTENCE_FAILED": "CRITICAL",
    "DAILY_LOSS": "WARNING",
    "ORDER_SKIPPED": "INFO",
    "CONTROL_KILL_SWITCH": "CRITICAL",
    "CONTROL_DISABLE_KILL_SWITCH": "INFO",
    "TRACKED_ENTRY_DRIFT": "WARNING",
}


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
        severity=_EVENT_SEVERITY_MAP.get(event.event_type),
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


def _llm_row_to_out(row: LLMInteraction) -> TimelineEventResponse:
    action = row.order_action or "NONE"
    message = row.error[:200] if (not row.success and row.error) else f"LLM {row.interaction_type} · {row.symbol} · {action}"
    return TimelineEventResponse(
        source="llm",
        id=row.id,
        event_type=row.interaction_type or "analyze",
        symbol=row.symbol or "",
        broker_order_id=row.order_id or "",
        side=action,
        status="SUCCESS" if row.success else "FAILED",
        message=message,
        payload={
            "success": bool(row.success),
            "applied": bool(row.applied),
            "order_action": action,
            "order_status": row.order_status,
            "error": row.error or "",
            "prompt_variant": row.prompt_variant,
        },
        created_at=row.created_at,
        severity="INFO" if row.success else "WARNING",
        result=("APPLIED" if row.applied else "VIEWED"),
    )


def _risk_row_to_out(row: RiskEvent) -> TimelineEventResponse:
    return TimelineEventResponse(
        source="risk",
        id=row.id,
        event_type=row.event_type,
        symbol="",
        broker_order_id="",
        side="",
        status="",
        message=(row.reason[:200] if row.reason else row.event_type),
        payload={"reason": row.reason or ""},
        created_at=row.created_at,
        severity="WARNING",
        result=None,
    )


def _to_utc_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _sort_key(item: TimelineEventResponse) -> tuple[float, str, int]:
    return (-_to_utc_timestamp(item.created_at), item.source, -item.id)


_MAX_MERGED_FETCH = 2000


def _paginate_query(query, page: int, page_size: int):
    return query.offset((page - 1) * page_size).limit(page_size).all()


def _apply_skip_category_filter(query, skip_category: str | None):
    if skip_category:
        query = query.filter(
            func.json_extract(TradeEvent.payload_json, '$.skip_category') == skip_category
        )
    return query


def list_timeline_events(
    db: Session,
    *,
    source: SourceFilter = "all",
    event_types: list[str] | None,
    symbol: str | None,
    skip_category: str | None = None,
    page: int,
    page_size: int,
    query: str | None = None,
) -> tuple[list[TimelineEventResponse], int]:
    """Merge trade_events and audit_logs for the timeline API (spec §5.2).

    ``query`` performs a case-insensitive substring match on the user-facing
    text columns (TradeEvent.message, AuditLog.action, both sides' symbol).
    It is intentionally a narrow OR-of-columns search so it stays cheap on
    SQLite and easy to reason about for the UI.
    """
    et = [e.strip() for e in (event_types or []) if e and e.strip()]
    query_term = (query or "").strip()
    if query_term:
        like = f"%{query_term}%"

    if source == "trade":
        tq = db.query(TradeEvent)
        if symbol:
            tq = tq.filter(TradeEvent.symbol == symbol)
        if et:
            tq = tq.filter(TradeEvent.event_type.in_(et))
        if query_term:
            tq = tq.filter(
                (TradeEvent.message.ilike(like))
                | (TradeEvent.symbol.ilike(like))
                | (TradeEvent.event_type.ilike(like))
            )
        tq = _apply_skip_category_filter(tq, skip_category)
        total = tq.count()
        rows = _paginate_query(tq.order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc()), page, page_size)
        return [_trade_row_to_out(r) for r in rows], total

    if source == "audit":
        aq = db.query(AuditLog)
        if et:
            aq = aq.filter(AuditLog.action.in_(et))
        if query_term:
            aq = aq.filter(
                (AuditLog.action.ilike(like))
                | (AuditLog.request_summary.ilike(like))
            )
        total = aq.count()
        rows = _paginate_query(aq.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()), page, page_size)
        return [_audit_row_to_out(r) for r in rows], total

    if source == "llm":
        lq = db.query(LLMInteraction)
        if symbol:
            lq = lq.filter(LLMInteraction.symbol == symbol)
        if et:
            lq = lq.filter(LLMInteraction.interaction_type.in_(et))
        if query_term:
            lq = lq.filter(
                (LLMInteraction.symbol.ilike(like))
                | (LLMInteraction.interaction_type.ilike(like))
                | (LLMInteraction.error.ilike(like))
            )
        total = lq.count()
        rows = _paginate_query(lq.order_by(LLMInteraction.created_at.desc(), LLMInteraction.id.desc()), page, page_size)
        return [_llm_row_to_out(r) for r in rows], total

    if source == "risk":
        rq = db.query(RiskEvent)
        if et:
            rq = rq.filter(RiskEvent.event_type.in_(et))
        if symbol:
            rq = rq.filter(RiskEvent.reason.contains(symbol))
        if query_term:
            rq = rq.filter(
                (RiskEvent.event_type.ilike(like))
                | (RiskEvent.reason.ilike(like))
            )
        total = rq.count()
        rows = _paginate_query(rq.order_by(RiskEvent.created_at.desc(), RiskEvent.id.desc()), page, page_size)
        return [_risk_row_to_out(r) for r in rows], total

    # source == "all": merge with capped fetch to avoid O(n²) deep pagination
    trade_total = 0
    audit_total = 0
    trade_rows: list[TradeEvent] = []
    audit_rows: list[AuditLog] = []

    fetch_n = min(page * page_size, _MAX_MERGED_FETCH)

    tq = db.query(TradeEvent)
    if symbol:
        tq = tq.filter(TradeEvent.symbol == symbol)
    if et:
        tq = tq.filter(TradeEvent.event_type.in_(et))
    if query_term:
        tq = tq.filter(
            (TradeEvent.message.ilike(like))
            | (TradeEvent.symbol.ilike(like))
            | (TradeEvent.event_type.ilike(like))
        )
    tq = _apply_skip_category_filter(tq, skip_category)
    trade_total = tq.count()
    trade_rows = tq.order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc()).limit(fetch_n).all()

    llm_total = 0
    risk_total = 0
    llm_rows: list[LLMInteraction] = []
    risk_rows: list[RiskEvent] = []
    if skip_category:
        # skip_category is trade-specific — skip the other sources entirely
        audit_total = 0
        audit_rows = []
    else:
        aq = db.query(AuditLog)
        if symbol:
            aq = aq.filter(AuditLog.request_summary.contains(symbol))
        if et:
            aq = aq.filter(AuditLog.action.in_(et))
        if query_term:
            aq = aq.filter(
                (AuditLog.action.ilike(like))
                | (AuditLog.request_summary.ilike(like))
            )
        audit_total = aq.count()
        audit_rows = aq.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(fetch_n).all()

        lq = db.query(LLMInteraction)
        if symbol:
            lq = lq.filter(LLMInteraction.symbol == symbol)
        if et:
            lq = lq.filter(LLMInteraction.interaction_type.in_(et))
        if query_term:
            lq = lq.filter(
                (LLMInteraction.symbol.ilike(like))
                | (LLMInteraction.interaction_type.ilike(like))
                | (LLMInteraction.error.ilike(like))
            )
        llm_total = lq.count()
        llm_rows = lq.order_by(LLMInteraction.created_at.desc(), LLMInteraction.id.desc()).limit(fetch_n).all()

        rq = db.query(RiskEvent)
        if et:
            rq = rq.filter(RiskEvent.event_type.in_(et))
        if symbol:
            rq = rq.filter(RiskEvent.reason.contains(symbol))
        if query_term:
            rq = rq.filter(
                (RiskEvent.event_type.ilike(like))
                | (RiskEvent.reason.ilike(like))
            )
        risk_total = rq.count()
        risk_rows = rq.order_by(RiskEvent.created_at.desc(), RiskEvent.id.desc()).limit(fetch_n).all()

    total = trade_total + audit_total + llm_total + risk_total
    # Each source query is already individually capped via .limit(fetch_n),
    # so the merged list below is bounded; report the true total so the
    # frontend pagination control is accurate (not clamped to _MAX_MERGED_FETCH).

    merged = (
        [_trade_row_to_out(r) for r in trade_rows]
        + [_audit_row_to_out(r) for r in audit_rows]
        + [_llm_row_to_out(r) for r in llm_rows]
        + [_risk_row_to_out(r) for r in risk_rows]
    )
    merged.sort(key=_sort_key)


    start = (page - 1) * page_size
    if start >= len(merged):
        return [], total
    return merged[start : start + page_size], total
