from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.models import StrategyConfig
from app.runner import get_runner
from app.schemas import ReportResponse
from app.services.report_schedule_service import ReportScheduleService
from app.services.report_service import ReportService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_api_key())])


@router.post("/schedule/run")
def run_scheduled_report_now(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict[str, object]:
    """Manually trigger a scheduled-report send now (also the UI 'test' button).

    Uses the configured ``report_schedule_symbol`` (falls back to the active
    strategy symbol) and dispatches through the runner's notifier. Audited.
    """
    actor_hash, source_ip = extract_actor(request)
    cfg = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    symbol = ((getattr(cfg, "report_schedule_symbol", "") if cfg else "") or (cfg.symbol if cfg else "") or "").strip().upper()
    title, content = ReportScheduleService(db).build_summary(symbol or "")
    runner = get_runner()
    notifier = getattr(runner, "notifier", None)
    sent = False
    error: str | None = None
    if notifier is None:
        error = "notifier not initialized"
    else:
        try:
            sent = bool(notifier.send(title, content, severity="INFO"))
        except Exception as exc:  # noqa: BLE001 — surface to the UI
            logger.exception("manual scheduled report send failed")
            error = str(exc)[:256]
    audit.record(
        "REPORT_SCHEDULE_SEND",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"symbol": symbol, "sent": sent},
        result="SUCCESS" if sent else "FAILED",
    )
    return {"sent": sent, "symbol": symbol, "title": title, "error": error}



@router.get("/daily", response_model=ReportResponse)
def get_daily_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US", pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    date: str = Query(..., description="Target date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_daily_report(symbol, date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("daily report generation failed")
        raise HTTPException(status_code=500, detail="Report generation failed") from exc
    return ReportResponse.model_validate(report)


@router.get("/weekly", response_model=ReportResponse)
def get_weekly_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US", pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    week_start: str = Query(..., description="Week start date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_weekly_report(symbol, week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("weekly report generation failed")
        raise HTTPException(status_code=500, detail="Report generation failed") from exc
    return ReportResponse.model_validate(report)


@router.get("/monthly", response_model=ReportResponse)
def get_monthly_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US", pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    month: str = Query(..., description="Month (YYYY-MM)", pattern=r'^\d{4}-\d{2}$'),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_monthly_report(symbol, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("monthly report generation failed")
        raise HTTPException(status_code=500, detail="Report generation failed") from exc
    return ReportResponse.model_validate(report)


@router.get("/range", response_model=ReportResponse)
def get_range_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US", pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_range_report(symbol, from_date, to_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("range report generation failed")
        raise HTTPException(status_code=500, detail="Report generation failed") from exc
    return ReportResponse.model_validate(report)


@router.get("/export")
def export_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US", pattern=r'^[A-Z0-9\-]{1,12}\.[A-Z]{2,4}$'),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    format: str = Query("json", description="Export format: json or csv"),
    db: Session = Depends(get_db),
):
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    try:
        svc = ReportService(db)
        buf = svc.export_report(symbol, from_date, to_date, format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("report export failed")
        raise HTTPException(status_code=500, detail="Report export failed") from exc
    media_type = "application/json" if format == "json" else "text/csv"
    safe_symbol = re.sub(r'[^a-zA-Z0-9]', '', symbol.replace('.', '_'))
    filename = f"report_{safe_symbol}_{from_date}_{to_date}.{format}"
    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
