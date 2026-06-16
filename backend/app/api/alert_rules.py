from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.runner import get_runner
from app.schemas import AlertEvaluateResult, AlertFiringOut, AlertFiringPage, AlertRuleCreate, AlertRuleOut, AlertRulePage
from app.services.alert_rule_service import AlertRuleService

router = APIRouter(
    prefix="/api/alert-rules",
    tags=["alert-rules"],
    dependencies=[Depends(require_api_key())],
)

alert_firings_router = APIRouter(
    prefix="/api/alert-firings",
    tags=["alert-firings"],
    dependencies=[Depends(require_api_key())],
)

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _day_start(value: str | None) -> datetime | None:
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _day_end(value: str | None) -> datetime | None:
    """Inclusive upper bound: midnight UTC of the day AFTER ``value``.

    A naive ``_day_start(to_date)`` (midnight) would exclude every firing later
    that same day, so 'history up to 2026-06-16' silently dropped the 15:00 fire.
    """
    start = _day_start(value)
    return start + timedelta(days=1) if start is not None else None


@router.post("", response_model=AlertRuleOut)
def create_alert_rule(
    payload: AlertRuleCreate,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AlertRuleOut:
    actor_hash, source_ip = extract_actor(request)
    out = AlertRuleService(db).create(payload)
    audit.record(
        "ALERT_RULE_CREATE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"id": out.id, "name": out.name, "rule_type": out.rule_type},
        result="SUCCESS",
    )
    return out


@router.get("", response_model=AlertRulePage)
def list_alert_rules(
    enabled: bool | None = Query(default=None),
    db=Depends(get_db),
) -> AlertRulePage:
    items = AlertRuleService(db).list_rules(enabled_only=(enabled is True))
    return AlertRulePage(items=items, total=len(items))


@router.get("/{rule_id}", response_model=AlertRuleOut)
def get_alert_rule(rule_id: int, db=Depends(get_db)) -> AlertRuleOut:
    out = AlertRuleService(db).get(rule_id)
    if out is None:
        raise HTTPException(status_code=404, detail="alert rule not found")
    return out


@router.put("/{rule_id}", response_model=AlertRuleOut)
def update_alert_rule(
    rule_id: int,
    payload: AlertRuleCreate,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AlertRuleOut:
    actor_hash, source_ip = extract_actor(request)
    out = AlertRuleService(db).update(rule_id, payload)
    if out is None:
        audit.record(
            "ALERT_RULE_UPDATE",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"id": rule_id, "found": False},
            result="NOT_FOUND",
        )
        raise HTTPException(status_code=404, detail="alert rule not found")
    audit.record(
        "ALERT_RULE_UPDATE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"id": rule_id, "name": out.name, "rule_type": out.rule_type},
        result="SUCCESS",
    )
    return out


@router.delete("/{rule_id}", status_code=204)
def delete_alert_rule(
    rule_id: int,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    actor_hash, source_ip = extract_actor(request)
    existed = AlertRuleService(db).delete(rule_id)
    audit.record(
        "ALERT_RULE_DELETE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"id": rule_id, "existed": existed},
        result="SUCCESS",
    )
    return Response(status_code=204)


@router.post("/evaluate", response_model=AlertEvaluateResult)
def evaluate_alert_rules(db=Depends(get_db)) -> AlertEvaluateResult:
    """Manually run the alert-rule evaluator (also runs on a background cron)."""
    return AlertRuleService(db).evaluate(get_runner())


@router.get("/{rule_id}/history", response_model=AlertFiringPage)
def alert_rule_firing_history(
    rule_id: int,
    from_date: str | None = Query(default=None, description="Lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    limit: int = Query(default=100, ge=1, le=500),
    db=Depends(get_db),
) -> AlertFiringPage:
    """Append-only firing timeline for one rule (most-recent first).

    ``AlertRule.last_fired_at`` only keeps the latest fire; this returns the
    full history so a trader can see how often a rule actually fires.
    """
    items = AlertRuleService(db).history(
        rule_id,
        from_dt=_day_start(from_date),
        to_dt=_day_end(to_date),
        limit=limit,
    )
    return AlertFiringPage(items=[AlertFiringOut.model_validate(f) for f in items], total=len(items))


@alert_firings_router.get("", response_model=AlertFiringPage)
def list_alert_firings(
    rule_id: int | None = Query(default=None, description="Filter to one rule (default: all)"),
    limit: int = Query(default=100, ge=1, le=500),
    db=Depends(get_db),
) -> AlertFiringPage:
    """Cross-rule firing timeline (most-recent first). Read-only."""
    items = AlertRuleService(db).list_firings(rule_id=rule_id, limit=limit)
    return AlertFiringPage(items=[AlertFiringOut.model_validate(f) for f in items], total=len(items))
