"""Re-entry behavior analysis service.

Conditions each same-symbol round trip on the previous trade's outcome
to detect behavioral tilt: does the system trade better right after a
win or right after a loss?  Read-only.

Inspired by Freqtrade's sequential trade-pair analysis and tilt detection
in trading journals.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ReentryAnalysisService"]


class _Bucket:
    __slots__ = ("n", "wins", "pnl")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.pnl = 0.0

    def add(self, pnl: float) -> None:
        self.n += 1
        if pnl > 0:
            self.wins += 1
        self.pnl += pnl

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.n,
            "win_rate": round(self.wins / self.n, 4) if self.n else None,
            "avg_pnl": round(self.pnl / self.n, 2) if self.n else None,
            "total_pnl": round(self.pnl, 2),
        }


class ReentryAnalysisService:
    """Conditional outcome analytics for same-symbol re-entries."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 6:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 6 closed trades.",
            }

        after_win = _Bucket()
        after_loss = _Bucket()
        after_scratch = _Bucket()
        first_of_symbol = _Bucket()
        by_symbol: dict[str, dict[str, _Bucket]] = defaultdict(
            lambda: {"after_win": _Bucket(), "after_loss": _Bucket()}
        )

        prev_by_symbol: dict[str, float] = {}
        for symbol, filled_at, pnl in rows:
            prev = prev_by_symbol.get(symbol)
            if prev is None:
                first_of_symbol.add(pnl)
            elif prev > 0:
                after_win.add(pnl)
                by_symbol[symbol]["after_win"].add(pnl)
            elif prev < 0:
                after_loss.add(pnl)
                by_symbol[symbol]["after_loss"].add(pnl)
            else:
                after_scratch.add(pnl)
            prev_by_symbol[symbol] = pnl

        symbol_rows = [
            {
                "symbol": sym,
                "after_win": buckets["after_win"].as_dict(),
                "after_loss": buckets["after_loss"].as_dict(),
            }
            for sym, buckets in sorted(
                by_symbol.items(),
                key=lambda kv: kv[1]["after_win"].n + kv[1]["after_loss"].n,
                reverse=True,
            )
        ]

        tilt = None
        if after_win.n >= 3 and after_loss.n >= 3:
            aw = after_win.pnl / after_win.n
            al = after_loss.pnl / after_loss.n
            tilt = round(aw - al, 2)

        return {
            "days": days,
            "sample_size": len(rows),
            "after_win": after_win.as_dict(),
            "after_loss": after_loss.as_dict(),
            "after_scratch": after_scratch.as_dict(),
            "first_of_symbol": first_of_symbol.as_dict(),
            "tilt_avg_pnl_diff": tilt,
            "by_symbol": symbol_rows,
        }

    def _fetch(self, days: int) -> list[tuple[str, datetime, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.symbol, OrderRecord.filled_at, OrderRecord.net_pnl)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return [
            (r[0], r[1], float(r[2]))
            for r in self._db.execute(stmt).all()
            if r[1] is not None and r[2] is not None
        ]
