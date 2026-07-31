"""Rolling VaR/CVaR service.

Computes historical Value-at-Risk and Conditional VaR (Expected Shortfall)
over a rolling window of trade PnLs.  Read-only.

Inspired by VectorBT's risk analytics and QuantStats' VaR tearsheet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RollingVarService"]


class RollingVarService:
    """Rolling historical VaR and CVaR computation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        window: int = 30,
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < window:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "window": window,
                "confidence": confidence,
                "sample_size": len(pnls),
                "error": f"Need at least {window} trades.",
            }

        points: list[dict[str, Any]] = []
        for i in range(window - 1, len(pnls)):
            w = sorted(pnls[i - window + 1 : i + 1])
            idx = int((1 - confidence) * window)
            var = -w[max(idx, 0)]
            tail = w[: max(idx, 1)]
            cvar = -sum(tail) / len(tail) if tail else var
            points.append(
                {
                    "index": i,
                    "var": round(var, 2),
                    "cvar": round(cvar, 2),
                }
            )

        latest = points[-1] if points else None
        vars_list = [p["var"] for p in points]
        cvars_list = [p["cvar"] for p in points]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "window": window,
            "confidence": confidence,
            "sample_size": len(pnls),
            "points": points[-50:],
            "summary": {
                "latest_var": latest["var"] if latest else 0,
                "latest_cvar": latest["cvar"] if latest else 0,
                "var_mean": round(sum(vars_list) / len(vars_list), 2),
                "var_max": round(max(vars_list), 2),
                "cvar_mean": round(sum(cvars_list) / len(cvars_list), 2),
                "cvar_max": round(max(cvars_list), 2),
            },
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]
