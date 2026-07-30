"""Composite risk score service.

Computes a multi-factor risk score per symbol combining drawdown severity,
loss-streak length, PnL volatility, and fee drag into a single 0-100
composite.  Read-only.

Inspired by QuantConnect's portfolio risk models and Lean's risk management
framework.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RiskScoreService"]


class RiskScoreService:
    """Multi-factor composite risk scoring per symbol."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(self, lookback_days: int = 180) -> dict[str, Any]:
        rows = self._fetch(lookback_days)
        if len(rows) < 5:
            return {
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "symbols": [],
                "error": "Need at least 5 closed trades.",
            }

        by_symbol: dict[str, list[float]] = defaultdict(list)
        for sym, pnl in rows:
            by_symbol[sym].append(pnl)

        results: list[dict[str, Any]] = []
        for sym, pnls in by_symbol.items():
            if len(pnls) < 3:
                continue
            score = self._score_symbol(pnls)
            results.append({"symbol": sym, **score})

        results.sort(key=lambda x: x["composite_score"], reverse=True)

        return {
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "symbols": results,
            "avg_composite": round(
                sum(r["composite_score"] for r in results) / len(results), 2
            )
            if results
            else 0,
        }

    def _score_symbol(self, pnls: list[float]) -> dict[str, Any]:
        n = len(pnls)
        total = sum(pnls)
        mean = total / n

        # Factor 1: PnL volatility (normalized std)
        var = sum((p - mean) ** 2 for p in pnls) / n
        std = math.sqrt(var) if var > 0 else 0.0
        vol_score = min(std / max(abs(mean), 1.0), 3.0) / 3.0 * 100

        # Factor 2: Max drawdown severity
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        dd_score = min(max_dd / max(abs(total), 1.0), 2.0) / 2.0 * 100

        # Factor 3: Loss streak length
        max_loss_streak = 0
        cur = 0
        for p in pnls:
            if p < 0:
                cur += 1
                max_loss_streak = max(max_loss_streak, cur)
            else:
                cur = 0
        streak_score = min(max_loss_streak / 8.0, 1.0) * 100

        # Factor 4: Loss ratio (fraction of trades that are losses)
        losses = sum(1 for p in pnls if p < 0)
        loss_ratio = losses / n
        loss_score = loss_ratio * 100

        # Composite: weighted average
        composite = (
            vol_score * 0.30
            + dd_score * 0.30
            + streak_score * 0.20
            + loss_score * 0.20
        )

        risk_level = (
            "high" if composite > 60 else "medium" if composite > 35 else "low"
        )

        return {
            "trade_count": n,
            "total_pnl": round(total, 2),
            "pnl_std": round(std, 2),
            "max_drawdown": round(max_dd, 2),
            "max_loss_streak": max_loss_streak,
            "loss_ratio": round(loss_ratio, 4),
            "factor_scores": {
                "volatility": round(vol_score, 2),
                "drawdown": round(dd_score, 2),
                "streak": round(streak_score, 2),
                "loss_ratio": round(loss_score, 2),
            },
            "composite_score": round(composite, 2),
            "risk_level": risk_level,
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.symbol, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.execute(stmt).all()
        return [
            (r[0], float(r[1])) for r in rows if r[1] is not None
        ]
