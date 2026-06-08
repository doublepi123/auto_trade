from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import ReportResponse
from app.services.report_service import ReportService

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/daily", response_model=ReportResponse)
def get_daily_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    date: str = Query(..., description="Target date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_daily_report(symbol, date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")
    return ReportResponse.model_validate(report)


@router.get("/weekly", response_model=ReportResponse)
def get_weekly_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    week_start: str = Query(..., description="Week start date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_weekly_report(symbol, week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")
    return ReportResponse.model_validate(report)


@router.get("/monthly", response_model=ReportResponse)
def get_monthly_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    month: str = Query(..., description="Month (YYYY-MM)"),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_monthly_report(symbol, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")
    return ReportResponse.model_validate(report)


@router.get("/range", response_model=ReportResponse)
def get_range_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> ReportResponse:
    try:
        svc = ReportService(db)
        report = svc.get_range_report(symbol, from_date, to_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")
    return ReportResponse.model_validate(report)


@router.get("/export")
def export_report(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
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
        raise HTTPException(status_code=500, detail=f"Report export failed: {exc}")
    media_type = "application/json" if format == "json" else "text/csv"
    filename = f"report_{symbol.replace('.', '_')}_{from_date}_{to_date}.{format}"
    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
