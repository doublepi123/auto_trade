"""Exit efficiency analysis service.

Measures how well exits capture the favorable excursion of each closed
trade: for winners, the share of MFE actually realized (capture rate)
and the giveback left on the table; for all trades, how deep the
adverse excursion went.  Read-only.

Inspired by Edgewonk's exit-efficiency journal metrics and TraderVue's
MFE/MAE execution reports.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ExitEfficiencyService"]


class ExitEfficiencyService:
    """MFE capture / MAE tolerance analytics over closed trades."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 3:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 3 closed trades with excursion data.",
            }

        captures: list[float] = []
        givebacks: list[float] = []
        maes: list[float] = []
        winner_maes: list[float] = []
        left_on_table = 0
        winners = 0
        by_cause: dict[str, dict[str, float]] = defaultdict(
            lambda: {"trades": 0.0, "capture_sum": 0.0, "capture_n": 0.0, "net_sum": 0.0}
        )

        for row in rows:
            net = float(row.net_pnl or 0.0)
            mfe = float(row.mfe_amount) if row.mfe_amount is not None else None
            mae = float(row.mae_amount) if row.mae_amount is not None else None
            cause = row.exit_cause or "UNKNOWN"

            bucket = by_cause[cause]
            bucket["trades"] += 1
            bucket["net_sum"] += net

            if mae is not None:
                # mae_amount is stored as adverse magnitude (<= 0 expected)
                depth = abs(mae)
                maes.append(depth)
                if net > 0:
                    winner_maes.append(depth)

            if net > 0 and mfe is not None and mfe > 0:
                winners += 1
                capture = min(net / mfe, 1.5)
                captures.append(capture)
                giveback = max(mfe - net, 0.0)
                givebacks.append(giveback)
                bucket["capture_sum"] += capture
                bucket["capture_n"] += 1
                if mfe > 2 * net:
                    left_on_table += 1

        cause_rows = [
            {
                "exit_cause": cause,
                "trades": int(v["trades"]),
                "net_pnl": round(v["net_sum"], 2),
                "avg_capture": round(v["capture_sum"] / v["capture_n"], 4) if v["capture_n"] else None,
            }
            for cause, v in sorted(by_cause.items(), key=lambda kv: kv[1]["trades"], reverse=True)
        ]

        return {
            "days": days,
            "sample_size": len(rows),
            "winners_with_mfe": winners,
            "avg_capture_rate": round(sum(captures) / len(captures), 4) if captures else None,
            "median_capture_rate": round(median(captures), 4) if captures else None,
            "avg_giveback": round(sum(givebacks) / len(givebacks), 2) if givebacks else None,
            "median_giveback": round(median(givebacks), 2) if givebacks else None,
            "left_on_table_count": left_on_table,
            "left_on_table_pct": round(left_on_table / winners, 4) if winners else None,
            "avg_mae_depth": round(sum(maes) / len(maes), 2) if maes else None,
            "avg_winner_mae_depth": (
                round(sum(winner_maes) / len(winner_maes), 2) if winner_maes else None
            ),
            "by_exit_cause": cause_rows,
        }

    def _fetch(self, days: int) -> list[OrderRecord]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord)
            .where(
                OrderRecord.net_pnl.is_not(None),
                OrderRecord.filled_at >= cutoff,
                (OrderRecord.mfe_amount.is_not(None)) | (OrderRecord.mae_amount.is_not(None)),
            )
            .order_by(OrderRecord.filled_at.asc())
        )
        return list(self._db.execute(stmt).scalars().all())
