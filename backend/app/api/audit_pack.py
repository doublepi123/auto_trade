from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.models import OrderRecord, StrategyConfig
from app.schemas import OrderResponse, RiskHistoryPoint, StrategyResponse, TimelineEventResponse
from app.services.event_list_service import list_timeline_events
from app.services.risk_history_service import RiskHistoryService


router = APIRouter(
    prefix="/api/audit-pack",
    tags=["audit-pack"],
    dependencies=[Depends(require_api_key())],
)


class AuditPackResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    symbol: str
    from_date: date | None
    to_date: date | None
    strategy_config: StrategyResponse | None
    orders: list[OrderResponse] = Field(default_factory=list)
    trade_events: list[TimelineEventResponse] = Field(default_factory=list)
    risk_events: list[TimelineEventResponse] = Field(default_factory=list)
    runtime_snapshots: list[RiskHistoryPoint] = Field(default_factory=list)


def _date_bounds(
    from_date: date | None,
    to_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must be on or before to_date")
    from_dt = (
        datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        if from_date is not None
        else None
    )
    to_dt = (
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        if to_date is not None
        else None
    )
    return from_dt, to_dt


def _list_orders(
    db: Session,
    *,
    symbol: str,
    from_dt: datetime | None,
    to_dt: datetime | None,
    limit: int,
) -> list[OrderResponse]:
    query = db.query(OrderRecord).filter(OrderRecord.symbol == symbol)
    if from_dt is not None:
        query = query.filter(OrderRecord.created_at >= from_dt)
    if to_dt is not None:
        query = query.filter(OrderRecord.created_at < to_dt)
    rows = query.order_by(OrderRecord.created_at.desc(), OrderRecord.id.desc()).limit(limit).all()
    return [OrderResponse.model_validate(row) for row in rows]


@router.get("/export")
def export_audit_pack(
    request: Request,
    symbol: str | None = Query(default=None, max_length=50),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    generated_at = datetime.now(timezone.utc)
    config = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    target_symbol = (symbol or (config.symbol if config is not None else "")).strip().upper()
    from_dt, to_dt = _date_bounds(from_date, to_date)
    strategy_config = StrategyResponse.model_validate(config) if config is not None else None
    orders = _list_orders(
        db,
        symbol=target_symbol,
        from_dt=from_dt,
        to_dt=to_dt,
        limit=limit,
    )
    trade_events, _ = list_timeline_events(
        db,
        source="trade",
        event_types=None,
        symbol=target_symbol,
        page=1,
        page_size=limit,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    risk_events, _ = list_timeline_events(
        db,
        source="risk",
        event_types=None,
        symbol=target_symbol,
        page=1,
        page_size=limit,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    runtime_snapshots = RiskHistoryService(db).get_history(
        symbol=target_symbol,
        limit=limit,
        from_dt=from_dt,
        to_dt=to_dt,
        max_limit=2000,
    ).points
    bundle = AuditPackResponse(
        generated_at=generated_at,
        symbol=target_symbol,
        from_date=from_date,
        to_date=to_date,
        strategy_config=strategy_config,
        orders=orders,
        trade_events=trade_events,
        risk_events=risk_events,
        runtime_snapshots=runtime_snapshots,
    )
    actor_hash, source_ip = extract_actor(request)
    audit.record(
        "AUDIT_PACK_EXPORT",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={
            "symbol": target_symbol,
            "orders": len(orders),
            "trade_events": len(trade_events),
            "risk_events": len(risk_events),
            "runtime_snapshots": len(runtime_snapshots),
        },
        result="SUCCESS",
    )
    safe_symbol = target_symbol.replace(".", "_") or "ALL"
    filename = f"audit_pack_{safe_symbol}_{generated_at.strftime('%Y%m%d')}.json"
    return Response(
        content=bundle.model_dump_json(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
