"""Scratch (breakeven) trade analysis service.

Identifies trades whose net outcome barely covered their costs
(|net_pnl| <= fees paid on the exit) and measures their frequency,
holding time, and opportunity cost versus decisive trades.  Read-only.

Inspired by Edgewonk's scratch-trade journaling metrics.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ScratchAnalysisService"]


class ScratchAnalysisService:
    """Scratch-trade rate and cost analytics over closed trades."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 5:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        scratch_holds: list[float] = []
        decisive_holds: list[float] = []
        scratch_by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {"scratch": 0, "total": 0})
        by_week: dict[str, dict[str, int]] = defaultdict(lambda: {"scratch": 0, "total": 0})
        scratch_count = 0
        scratch_fee_total = 0.0

        for row in rows:
            net = float(row.net_pnl or 0.0)
            fee = float(row.pnl_fee) if row.pnl_fee is not None else 0.0
            band = max(fee, 1e-9)
            is_scratch = abs(net) <= band

            hold_min = _holding_minutes(row)
            if is_scratch:
                scratch_count += 1
                scratch_fee_total += fee
                if hold_min is not None:
                    scratch_holds.append(hold_min)
            elif hold_min is not None:
                decisive_holds.append(hold_min)

            sym = scratch_by_symbol[row.symbol]
            sym["total"] += 1
            if is_scratch:
                sym["scratch"] += 1

            if row.filled_at:
                week = row.filled_at.date().isocalendar()
                key = f"{week[0]}-W{week[1]:02d}"
                by_week[key]["total"] += 1
                if is_scratch:
                    by_week[key]["scratch"] += 1

        n = len(rows)
        symbol_rows = [
            {
                "symbol": sym,
                "total": v["total"],
                "scratch": v["scratch"],
                "scratch_rate": round(v["scratch"] / v["total"], 4),
            }
            for sym, v in sorted(
                scratch_by_symbol.items(), key=lambda kv: kv[1]["scratch"] / kv[1]["total"], reverse=True
            )
        ]

        weekly = [
            {
                "week": w,
                "total": v["total"],
                "scratch_rate": round(v["scratch"] / v["total"], 4),
            }
            for w, v in sorted(by_week.items())
        ]

        return {
            "days": days,
            "sample_size": n,
            "scratch_count": scratch_count,
            "scratch_rate": round(scratch_count / n, 4),
            "scratch_fee_total": round(scratch_fee_total, 2),
            "avg_scratch_hold_min": (
                round(sum(scratch_holds) / len(scratch_holds), 1) if scratch_holds else None
            ),
            "avg_decisive_hold_min": (
                round(sum(decisive_holds) / len(decisive_holds), 1) if decisive_holds else None
            ),
            "median_scratch_hold_min": round(median(scratch_holds), 1) if scratch_holds else None,
            "by_symbol": symbol_rows,
            "weekly": weekly,
        }

    def _fetch(self, days: int) -> list[OrderRecord]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return list(self._db.execute(stmt).scalars().all())


def _holding_minutes(row: OrderRecord) -> float | None:
    if row.filled_at is None or row.cost_basis_opened_at is None:
        return None
    delta = (row.filled_at - row.cost_basis_opened_at).total_seconds() / 60.0
    return delta if delta >= 0 else None
