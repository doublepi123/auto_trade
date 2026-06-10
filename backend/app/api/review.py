from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.runner import get_runner
from app.services.review_service import ReviewService

router = APIRouter(prefix="/api/review", tags=["review"])
logger = logging.getLogger(__name__)


@router.get("")
def get_review(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    db: Session = Depends(get_db),
):
    try:
        svc = ReviewService(db)
        data = svc.get_review(symbol, from_date, to_date)
        return data
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("review aggregation failed")
        raise HTTPException(status_code=500, detail="Review aggregation failed") from None


@router.get("/export")
def export_review(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL.US"),
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    format: str = Query("json", description="Export format: json or csv"),
    db: Session = Depends(get_db),
):
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    try:
        svc = ReviewService(db)
        diagnostics = get_runner().diagnostics()
        diagnostics["symbol_runtimes"] = [
            runtime for runtime in diagnostics.get("symbol_runtimes", [])
            if runtime.get("symbol") == symbol
        ]
        buf = svc.export_review(symbol, from_date, to_date, format, diagnostics=diagnostics)
        media_type = "application/json" if format == "json" else "text/csv"
        safe_symbol = re.sub(r'[^a-zA-Z0-9]', '', symbol.replace('.', '_'))
        filename = f"review_{safe_symbol}_{from_date}_{to_date}.{format}"
        return StreamingResponse(
            buf,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("review export failed")
        raise HTTPException(status_code=500, detail="Review export failed") from None
