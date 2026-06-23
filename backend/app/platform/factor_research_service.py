"""P196: factor research warehouse.

Persists per (as_of, symbol, factor) snapshots plus forward returns, and
builds IC time series from the panel — the alphalens/Qlib-style factor research
loop. Reuses :mod:`app.platform.factors` so registered factor implementations
drive the computed values; this layer handles persistence + IC aggregation only.

The service supports two flows:

* :meth:`record_snapshot` — persist a single computed factor value + its
  observed forward return (called by a research driver after computing a factor
  and waiting ``horizon_bars`` bars).
* :meth:`compute_ic_series` — query the warehouse for a factor + date range and
  return the per-period IC series (cross-sectional correlation between factor
  rank and forward return), mirroring
  :func:`~app.platform.factors.information_coefficient` over time.

Cross-sectional decile ``rank`` is assigned per (factor_name, as_of) when the
caller asks for ranking via :meth:`rank_snapshot`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import FactorICSeries, FactorSnapshot
from app.platform.factors import pearson

__all__ = ["FactorResearchService", "FactorSnapshotData"]


@dataclass
class FactorSnapshotData:
    factor_name: str
    symbol: str
    as_of: datetime
    factor_value: float
    forward_return: float | None = None
    horizon_bars: int = 1
    rank: int | None = None
    context: dict[str, Any] | None = None


class FactorResearchService:
    """Persistence + IC aggregation for the factor research warehouse."""

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    def _session(self) -> Session:
        return self._db if self._db is not None else SessionLocal()

    def _owns_session(self) -> bool:
        return self._db is None

    def record_snapshot(self, data: FactorSnapshotData) -> FactorSnapshot:
        session = self._session()
        try:
            row = FactorSnapshot(
                factor_name=data.factor_name,
                symbol=data.symbol,
                as_of=data.as_of,
                factor_value=float(data.factor_value),
                forward_return=float(data.forward_return) if data.forward_return is not None else None,
                horizon_bars=int(data.horizon_bars),
                rank=data.rank,
                context_json=json.dumps(data.context or {}),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            if self._owns_session():
                session.close()

    def record_many(self, rows: list[FactorSnapshotData]) -> int:
        session = self._session()
        try:
            for data in rows:
                session.add(
                    FactorSnapshot(
                        factor_name=data.factor_name,
                        symbol=data.symbol,
                        as_of=data.as_of,
                        factor_value=float(data.factor_value),
                        forward_return=float(data.forward_return) if data.forward_return is not None else None,
                        horizon_bars=int(data.horizon_bars),
                        rank=data.rank,
                        context_json=json.dumps(data.context or {}),
                    )
                )
            session.commit()
            return len(rows)
        finally:
            if self._owns_session():
                session.close()

    def rank_snapshot(self, factor_name: str, as_of: datetime) -> int | None:
        """Return the cross-sectional rank of (factor_name, as_of) rows.

        Ranking is 1-indexed descending by factor value (highest factor = 1).
        Returns the count of ranked rows (for the caller to assign per-row).
        """
        session = self._session()
        try:
            rows = (
                session.query(FactorSnapshot)
                .filter(FactorSnapshot.factor_name == factor_name, FactorSnapshot.as_of == as_of)
                .order_by(FactorSnapshot.factor_value.desc())
                .all()
            )
            for idx, row in enumerate(rows, start=1):
                row.rank = idx
            session.commit()
            return len(rows) or None
        finally:
            if self._owns_session():
                session.close()

    def list_snapshots(
        self,
        factor_name: str | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        session = self._session()
        try:
            query = session.query(FactorSnapshot)
            if factor_name:
                query = query.filter(FactorSnapshot.factor_name == factor_name)
            if symbol:
                query = query.filter(FactorSnapshot.symbol == symbol)
            if since:
                query = query.filter(FactorSnapshot.as_of >= since)
            if until:
                query = query.filter(FactorSnapshot.as_of <= until)
            rows = (
                query.order_by(FactorSnapshot.as_of.desc(), FactorSnapshot.id.desc())
                .limit(max(1, min(limit, 10000)))
                .all()
            )
            return [_snapshot_to_dict(r) for r in rows]
        finally:
            if self._owns_session():
                session.close()

    def compute_ic_series(
        self,
        factor_name: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Build the IC time series for ``factor_name`` from persisted snapshots.

        For each ``as_of`` timestamp with >=2 symbols that have both a factor
        value and a forward return, compute the Pearson correlation between the
        cross-sectional factor values and the forward returns. Persist one
        :class:`FactorICSeries` row per timestamp and return the series summary.
        """
        session = self._session()
        try:
            query = session.query(FactorSnapshot).filter(FactorSnapshot.factor_name == factor_name)
            if since:
                query = query.filter(FactorSnapshot.as_of >= since)
            if until:
                query = query.filter(FactorSnapshot.as_of <= until)
            rows = query.order_by(FactorSnapshot.as_of.asc()).all()

            # Group by as_of.
            buckets: dict[datetime, list[FactorSnapshot]] = {}
            for row in rows:
                buckets.setdefault(row.as_of, []).append(row)

            per_period: list[dict[str, Any]] = []
            ics: list[float] = []
            for as_of, group in sorted(buckets.items()):
                valid = [g for g in group if g.forward_return is not None]
                if len(valid) < 2:
                    continue
                xs = [float(g.factor_value) for g in valid]
                ys = [float(g.forward_return or 0.0) for g in valid]
                ic = pearson(xs, ys)
                ics.append(ic)
                num_symbols = len(valid)
                # Persist the IC point.
                session.add(
                    FactorICSeries(
                        factor_name=factor_name,
                        as_of=as_of,
                        mean_ic=ic,
                        std_ic=0.0,
                        ic_ir=0.0,
                        num_symbols=num_symbols,
                    )
                )
                per_period.append(
                    {
                        "as_of": as_of.isoformat() if as_of else None,
                        "ic": ic,
                        "num_symbols": num_symbols,
                    }
                )
            if ics:
                mean_ic = sum(ics) / len(ics)
                var = sum((v - mean_ic) ** 2 for v in ics) / len(ics)
                std_ic = var ** 0.5
                ic_ir = (mean_ic / std_ic) if std_ic > 0 else 0.0
            else:
                mean_ic = std_ic = ic_ir = 0.0
            session.commit()
            return {
                "factor_name": factor_name,
                "mean_ic": mean_ic,
                "std_ic": std_ic,
                "ic_ir": ic_ir,
                "num_periods": len(ics),
                "per_period": per_period,
            }
        finally:
            if self._owns_session():
                session.close()


def _snapshot_to_dict(row: FactorSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "factor_name": row.factor_name,
        "symbol": row.symbol,
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "factor_value": row.factor_value,
        "forward_return": row.forward_return,
        "horizon_bars": row.horizon_bars,
        "rank": row.rank,
        "context": json.loads(row.context_json) if row.context_json else {},
    }