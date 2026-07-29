"""Performance attribution service: decomposes realized PnL across dimensions.

Reads closed round-trip exits from the ``OrderRecord`` ledger (the ``orders``
table) — exit-side fills carry ``net_pnl`` / ``exit_reason`` / ``filled_at`` —
and breaks total PnL down by symbol, direction, exit-reason, market session
and calendar day. Also exposes the top absolute-PnL contributors with their
holding-time average.

The service is read-only and side-effect free; it never writes to the session
it receives. All aggregations gracefully handle empty inputs (zero PnL, empty
buckets) and division-by-zero edge cases (win rate 0 when no trades).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["PerformanceAttributionService"]


# Holding-time is reported in minutes; need the UTC tzinfo to compute timedeltas
# safely against tz-aware ``filled_at`` / ``cost_basis_opened_at`` columns.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Bucket:
    """Mutable accumulator for one attribution bucket (symbol / direction / …)."""
    total_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    # Holding-time tracking (minutes); kept only where both entry and exit
    # timestamps are known so the metric stays honest.
    holding_minutes_sum: float = 0.0
    holding_minutes_n: int = 0

    def add(self, pnl: float, holding_minutes: float | None) -> "_Bucket":
        wins = self.win_count + (1 if pnl > 0 else 0)
        if holding_minutes is not None and holding_minutes >= 0:
            return _Bucket(
                total_pnl=self.total_pnl + pnl,
                trade_count=self.trade_count + 1,
                win_count=wins,
                holding_minutes_sum=self.holding_minutes_sum + holding_minutes,
                holding_minutes_n=self.holding_minutes_n + 1,
            )
        return _Bucket(
            total_pnl=self.total_pnl + pnl,
            trade_count=self.trade_count + 1,
            win_count=wins,
            holding_minutes_sum=self.holding_minutes_sum,
            holding_minutes_n=self.holding_minutes_n,
        )


@dataclass
class _Aggregator:
    """All dimension buckets for a single attribution pass."""
    by_symbol: dict[str, _Bucket] = field(default_factory=dict)
    by_direction: dict[str, _Bucket] = field(default_factory=dict)
    by_exit_reason: dict[str, _Bucket] = field(default_factory=dict)
    by_session: dict[str, _Bucket] = field(default_factory=dict)
    by_day: dict[date, _Bucket] = field(default_factory=dict)


class PerformanceAttributionService:
    """Decompose realized PnL across symbols, directions, reasons, sessions, days."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def attribute_pnl(
        self,
        days: int = 30,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Return the full multi-dimensional PnL breakdown for the window.

        Bucket keys are stable strings; values carry ``total_pnl``,
        ``trade_count`` and (where relevant) ``win_count`` / ``avg_pnl``. The
        ``by_day`` list is sorted ascending by date for timeline-friendly UIs.
        """
        days = max(0, int(days))
        closed_exits = self._fetch_closed_exits(days=days, symbol=symbol)
        agg = self._aggregate(closed_exits)

        total_pnl = sum(b.total_pnl for b in agg.by_symbol.values())
        total_trades = sum(b.trade_count for b in agg.by_symbol.values())
        total_wins = sum(b.win_count for b in agg.by_symbol.values())
        win_rate = (total_wins / total_trades) if total_trades else 0.0

        return {
            "period_days": days,
            "total_pnl": round(total_pnl, 4),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "by_symbol": _serialize_buckets(agg.by_symbol, include_avg=True, include_win=True),
            "by_direction": _serialize_buckets(agg.by_direction, include_avg=False, include_win=False),
            "by_exit_reason": _serialize_buckets(agg.by_exit_reason, include_avg=False, include_win=False),
            "by_session": _serialize_buckets(agg.by_session, include_avg=False, include_win=False),
            "by_day": _serialize_day_buckets(agg.by_day),
        }

    def top_contributors(
        self,
        days: int = 30,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the top ``limit`` symbols by absolute PnL contribution.

        Each row carries ``symbol``, ``total_pnl``, ``trade_count``,
        ``win_rate`` and ``avg_holding_minutes`` (``0.0`` when entry/exit
        timestamps are unavailable, so the column is always numeric).
        """
        days = max(0, int(days))
        limit = max(1, int(limit))
        closed_exits = self._fetch_closed_exits(days=days, symbol=None)
        agg = self._aggregate(closed_exits)

        rows: list[dict[str, Any]] = []
        for symbol_name, bucket in agg.by_symbol.items():
            avg_hold = (
                bucket.holding_minutes_sum / bucket.holding_minutes_n
                if bucket.holding_minutes_n
                else 0.0
            )
            win_rate = (bucket.win_count / bucket.trade_count) if bucket.trade_count else 0.0
            rows.append(
                {
                    "symbol": symbol_name,
                    "total_pnl": round(bucket.total_pnl, 4),
                    "trade_count": bucket.trade_count,
                    "win_rate": round(win_rate, 4),
                    "avg_holding_minutes": round(avg_hold, 2),
                }
            )
        # Biggest absolute contributors first (winners and losers both surface),
        # tie-broken by symbol for deterministic ordering.
        rows.sort(key=lambda r: (-abs(r["total_pnl"]), r["symbol"]))
        return rows[:limit]

    # ------------------------------------------------------------------
    # data access
    # ------------------------------------------------------------------

    def _fetch_closed_exits(
        self,
        days: int,
        symbol: str | None,
    ) -> list[OrderRecord]:
        """Return the exit-side fills that closed a round-trip in the window.

        A "closed exit" is an order row whose ``net_pnl`` is non-null — the
        trade-execution pipeline only writes PnL onto the exit leg of a paired
        round-trip. We filter on ``filled_at`` (falling back to ``created_at``
        for legacy rows) within the lookback window, plus an optional symbol.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.net_pnl.is_not(None))
            .where(OrderRecord.filled_at.is_not(None))
            .where(OrderRecord.filled_at >= cutoff)
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        # Newest first is irrelevant for aggregation but keeps the by_day
        # timeline deterministic when timestamps tie.
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        return list(self._db.scalars(stmt).all())

    # ------------------------------------------------------------------
    # aggregation
    # ------------------------------------------------------------------

    def _aggregate(self, exits: list[OrderRecord]) -> _Aggregator:
        agg = _Aggregator()
        for order in exits:
            pnl = _safe_float(getattr(order, "net_pnl", None))
            if pnl is None:
                # net_pnl is the documented PnL source; skip rows where it is
                # unexpectedly NULL after the filter (defensive — should not
                # happen, but never let one bad row break the whole report).
                continue

            holding = _holding_minutes(order)
            symbol = str(getattr(order, "symbol", "") or "")
            direction = _infer_direction(order)
            exit_reason = _infer_exit_reason(order)
            session = _infer_session(symbol)
            day_key = _infer_day(order)

            _merge(agg.by_symbol, symbol, pnl, holding)
            _merge(agg.by_direction, direction, pnl, holding)
            _merge(agg.by_exit_reason, exit_reason, pnl, holding)
            _merge(agg.by_session, session, pnl, holding)
            if day_key is not None:
                _merge(agg.by_day, day_key, pnl, holding)
        return agg


# ----------------------------------------------------------------------
# module-private inference helpers (pure functions, no DB access)
# ----------------------------------------------------------------------


def _merge(
    target: dict[Any, _Bucket],
    key: Any,
    pnl: float,
    holding_minutes: float | None,
) -> None:
    """Insert/merge one PnL observation into a dimension bucket dict."""
    current = target.get(key)
    target[key] = current.add(pnl, holding_minutes) if current is not None else _Bucket().add(pnl, holding_minutes)


def _safe_float(value: Any) -> float | None:
    """Coerce numeric/Decimal to float; return ``None`` for ``None`` input."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _holding_minutes(order: OrderRecord) -> float | None:
    """Return holding time in minutes from entry→exit, or ``None`` if unknown.

    Entry timestamp prefers ``cost_basis_opened_at`` (the authoritative round-
    trip open time persisted by the PnL service) and falls back to ``None``
    rather than ``created_at`` so the metric never confuses order-submission
    latency with position holding time.
    """
    opened = getattr(order, "cost_basis_opened_at", None)
    exited = getattr(order, "filled_at", None)
    if opened is None or exited is None:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if exited.tzinfo is None:
        exited = exited.replace(tzinfo=timezone.utc)
    delta = (exited - opened).total_seconds() / 60.0
    return delta


def _infer_direction(order: OrderRecord) -> str:
    """Direction of the position that was closed.

    A SELL exit closes a LONG; a BUY exit closes a SHORT. The live system is
    long-only by P0 safety default, but SHORT is reported faithfully when the
    data says so (and never silently bucketed under LONG).
    """
    side = str(getattr(order, "side", "") or "").strip().upper()
    if side in ("SELL", "S"):
        return "LONG"
    if side in ("BUY", "B"):
        return "SHORT"
    return "UNKNOWN"


def _infer_exit_reason(order: OrderRecord) -> str:
    """Exit-reason bucket: prefer ``exit_reason`` text, then ``exit_cause``.

    Falls back to ``"UNSPECIFIED"`` so uncategorized exits still appear in the
    breakdown rather than being silently dropped.
    """
    reason = str(getattr(order, "exit_reason", "") or "").strip()
    if reason:
        return reason
    cause = str(getattr(order, "exit_cause", "") or "").strip()
    if cause:
        return cause
    return "UNSPECIFIED"


def _infer_session(symbol: str) -> str:
    """Market session inferred from the symbol suffix (``.HK`` → HK, else US)."""
    upper = symbol.upper()
    if upper.endswith(".HK"):
        return "HK"
    return "US"


def _infer_day(order: OrderRecord) -> date | None:
    """Calendar day of the exit fill (UTC); ``None`` if no fill timestamp."""
    ts = getattr(order, "filled_at", None)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date()


def _serialize_buckets(
    buckets: dict[str, _Bucket],
    *,
    include_avg: bool,
    include_win: bool,
) -> dict[str, dict[str, Any]]:
    """Render a dimension's buckets as a stable JSON-friendly dict of dicts."""
    out: dict[str, dict[str, Any]] = {}
    # Sort by key for deterministic output (matters for snapshot tests and UI).
    for key in sorted(buckets.keys()):
        bucket = buckets[key]
        entry: dict[str, Any] = {
            "total_pnl": round(bucket.total_pnl, 4),
            "trade_count": bucket.trade_count,
        }
        if include_win:
            entry["win_count"] = bucket.win_count
        if include_avg:
            entry["avg_pnl"] = (
                round(bucket.total_pnl / bucket.trade_count, 4)
                if bucket.trade_count
                else 0.0
            )
        out[str(key)] = entry
    return out


def _serialize_day_buckets(buckets: dict[date, _Bucket]) -> list[dict[str, Any]]:
    """Render the day timeline as an ascending list of ``{date, pnl, count}``."""
    rows: list[dict[str, Any]] = []
    for day in sorted(buckets.keys()):
        bucket = buckets[day]
        rows.append(
            {
                "date": day.isoformat(),
                "pnl": round(bucket.total_pnl, 4),
                "trade_count": bucket.trade_count,
            }
        )
    return rows
