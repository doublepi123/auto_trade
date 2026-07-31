"""PnL distribution shape analysis service.

Computes skewness, kurtosis, and a simple normality heuristic on the
realized PnL distribution to characterize tail risk.  Read-only.

Inspired by VectorBT's return distribution analytics and QuantStats'
distribution fitting tearsheet.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["DistributionShapeService"]


class DistributionShapeService:
    """Statistical shape descriptors of the PnL distribution."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 10:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 10 closed trades.",
            }

        n = len(pnls)
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / n
        std = math.sqrt(var) if var > 0 else 0.0

        # skewness (Fisher)
        if std > 0:
            skew = sum(((p - mean) / std) ** 3 for p in pnls) / n
            kurt = sum(((p - mean) / std) ** 4 for p in pnls) / n - 3.0
        else:
            skew = 0.0
            kurt = 0.0

        # percentiles
        sorted_pnls = sorted(pnls)
        p5 = sorted_pnls[int(n * 0.05)]
        p25 = sorted_pnls[int(n * 0.25)]
        p50 = sorted_pnls[int(n * 0.50)]
        p75 = sorted_pnls[int(n * 0.75)]
        p95 = sorted_pnls[int(n * 0.95)]

        # simple normality heuristic (Jarque-Bera-like)
        jb = (n / 6.0) * (skew**2 + (kurt**2) / 4.0)
        is_normal_like = jb < 5.99  # chi2(2) 5% critical value

        # tail characterization
        tail_label = "fat-tailed" if kurt > 1.0 else "thin-tailed" if kurt < -1.0 else "near-normal"
        asymmetry = "right-skewed" if skew > 0.5 else "left-skewed" if skew < -0.5 else "symmetric"

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "mean": round(mean, 2),
            "std": round(std, 2),
            "skewness": round(skew, 4),
            "kurtosis": round(kurt, 4),
            "jarque_bera": round(jb, 4),
            "is_normal_like": is_normal_like,
            "tail_label": tail_label,
            "asymmetry": asymmetry,
            "percentiles": {
                "p5": round(p5, 2),
                "p25": round(p25, 2),
                "p50": round(p50, 2),
                "p75": round(p75, 2),
                "p95": round(p95, 2),
            },
            "iqr": round(p75 - p25, 2),
            "interpretation": _interpret(skew, kurt, is_normal_like),
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]


def _interpret(skew: float, kurt: float, normal: bool) -> str:
    parts: list[str] = []
    if normal:
        parts.append("Distribution is approximately normal (JB test)")
    else:
        parts.append("Distribution deviates significantly from normal")
    if skew > 0.5:
        parts.append("positive skew — occasional large wins")
    elif skew < -0.5:
        parts.append("negative skew — occasional large losses (tail risk)")
    if kurt > 1.0:
        parts.append("excess kurtosis — fat tails, extreme outcomes more likely")
    return "; ".join(parts) + "."
