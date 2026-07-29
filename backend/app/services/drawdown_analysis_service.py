from __future__ import annotations

"""Drawdown analysis built on the canonical closed-round-trip ledger.

The system has no ``Trade`` / ``DailyPnl`` tables: realized performance is
derived from the order ledger via :class:`DailyPnlService.pair_round_trips`.
This service layers a cumulative-PnL equity curve on top of those round trips
and computes drawdown statistics (peak-to-trough depth, duration and recovery
behaviour) over a trailing window.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.api.trades import _active_fee_rates
from app.services.daily_pnl_service import ClosedRoundTrip, DailyPnlService

# A trade is considered "in drawdown" when its cumulative PnL sits strictly
# below the running peak observed so far. Recovery happens once cumulative PnL
# returns to (or above) the peak that preceded the drawdown.
_IN_DRAWDOWN_EPS = 1e-9


class DrawdownAnalysisService:
    """Compute peak-to-trough drawdown statistics from closed round trips."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_drawdown_summary(self, symbol: str | None = None, days: int = 90) -> dict[str, Any]:
        """Return an aggregate drawdown summary over the trailing window.

        The equity curve is built from cumulative realized net PnL of closed
        round trips ordered by exit time. ``days`` bounds the *exit* time of the
        trips included in the window (the same convention as the trades API),
        while the running peak/drawdown is tracked only across the in-window
        trips so the reported max drawdown reflects the requested horizon.
        """
        days = max(1, int(days))
        from_dt = datetime.now(timezone.utc) - timedelta(days=days)
        trips = self._load_round_trips(symbol=symbol, from_dt=from_dt)

        if not trips:
            return self._empty_summary(symbol=symbol, days=days)

        snapshots = self._build_equity_curve(trips)
        # The final snapshot IS the window summary (peak/drawdown tracked only
        # across in-window trips, so the last point's cumulative PnL == total).
        current = snapshots[-1]

        max_dd = 0.0
        max_dd_pct = 0.0
        max_dd_date: datetime | None = None
        max_dd_duration_days = 0
        recovery_count = 0
        recovery_days: list[int] = []

        # Running drawdown segment state.
        in_drawdown = False
        drawdown_started_at: datetime | None = None
        drawdown_peak = 0.0

        for snap in snapshots:
            if snap["drawdown"] < -_IN_DRAWDOWN_EPS:
                if not in_drawdown:
                    in_drawdown = True
                    drawdown_started_at = snap["timestamp"]
                    drawdown_peak = snap["peak_pnl"]
                assert drawdown_started_at is not None  # set the line above
                current_duration = self._whole_days_between(
                    drawdown_started_at, snap["timestamp"]
                )
                if current_duration > max_dd_duration_days:
                    max_dd_duration_days = current_duration
            else:
                if in_drawdown and drawdown_started_at is not None:
                    # Recovered: cumulative PnL returned to / above the peak.
                    recovery_count += 1
                    recovery_days.append(
                        self._whole_days_between(drawdown_started_at, snap["timestamp"])
                    )
                    in_drawdown = False
                    drawdown_started_at = None
                    drawdown_peak = 0.0

            if snap["drawdown"] < max_dd:
                max_dd = snap["drawdown"]
                max_dd_date = snap["timestamp"]
            if snap["drawdown_pct"] is not None and snap["drawdown_pct"] < max_dd_pct:
                max_dd_pct = snap["drawdown_pct"]

        # If still in drawdown at the end of the window, max duration reflects
        # the last in-window timestamp (no recovery credited).
        avg_recovery_days = (
            round(sum(recovery_days) / len(recovery_days), 2) if recovery_days else 0.0
        )

        peak_pnl = current["peak_pnl"]
        current_pnl = current["cumulative_pnl"]
        current_drawdown = current["drawdown"]
        current_drawdown_pct = current["drawdown_pct"]

        return {
            "symbol": symbol,
            "period_days": days,
            "peak_pnl": round(peak_pnl, 2),
            "current_pnl": round(current_pnl, 2),
            "current_drawdown": round(current_drawdown, 2),
            "current_drawdown_pct": (
                round(current_drawdown_pct, 4) if current_drawdown_pct is not None else 0.0
            ),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "max_drawdown_date": max_dd_date.isoformat() if max_dd_date else None,
            "max_drawdown_duration_days": max_dd_duration_days,
            "recovery_count": recovery_count,
            "avg_recovery_days": avg_recovery_days,
            "is_in_drawdown": bool(in_drawdown),
        }

    def get_drawdown_timeline(self, symbol: str | None = None, days: int = 90) -> list[dict[str, Any]]:
        """Return one snapshot per closed round trip, in chronological order.

        Each entry carries the cumulative PnL, the running peak, the absolute
        and percentage drawdown at that point, and an ``is_in_drawdown`` flag.
        """
        days = max(1, int(days))
        from_dt = datetime.now(timezone.utc) - timedelta(days=days)
        trips = self._load_round_trips(symbol=symbol, from_dt=from_dt)
        snapshots = self._build_equity_curve(trips)
        return [
            {
                "date": snap["timestamp"].isoformat(),
                "timestamp": snap["timestamp"].isoformat(),
                "cumulative_pnl": round(snap["cumulative_pnl"], 2),
                "peak_pnl": round(snap["peak_pnl"], 2),
                "drawdown": round(snap["drawdown"], 2),
                "drawdown_pct": (
                    round(snap["drawdown_pct"], 4) if snap["drawdown_pct"] is not None else 0.0
                ),
                "is_in_drawdown": snap["is_in_drawdown"],
            }
            for snap in snapshots
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _load_round_trips(
        self, *, symbol: str | None, from_dt: datetime
    ) -> list[ClosedRoundTrip]:
        fee_rate_us, fee_rate_hk = _active_fee_rates(self._db)
        trips = DailyPnlService(self._db).pair_round_trips(
            symbol=symbol,
            from_dt=from_dt,
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
        )
        # Defensive: only keep trips whose exit actually falls in the window.
        # ``pair_round_trips`` upper-bounds by ``to_dt`` only, and we did not
        # pass one; an explicit filter keeps the reported horizon honest.
        return [t for t in trips if t.exit_at >= from_dt]

    @staticmethod
    def _build_equity_curve(trips: list[ClosedRoundTrip]) -> list[dict[str, Any]]:
        """Reduce closed round trips into a per-trade cumulative-PnL curve.

        Returns chronological snapshots (sorted by exit time) each annotated
        with the running peak, the absolute drawdown, the drawdown as a
        fraction of the peak, and an ``is_in_drawdown`` flag.
        """
        ordered = sorted(trips, key=lambda t: t.exit_at)
        snapshots: list[dict[str, Any]] = []
        cumulative = 0.0
        peak = 0.0
        for trip in ordered:
            cumulative += float(trip.net_pnl)
            if cumulative > peak:
                peak = cumulative
            drawdown = cumulative - peak
            drawdown_pct = (drawdown / peak) if peak > 0 else None
            snapshots.append(
                {
                    "timestamp": trip.exit_at,
                    "cumulative_pnl": cumulative,
                    "peak_pnl": peak,
                    "drawdown": drawdown,
                    "drawdown_pct": drawdown_pct,
                    # In drawdown whenever strictly below the running peak.
                    "is_in_drawdown": drawdown < -_IN_DRAWDOWN_EPS,
                }
            )
        return snapshots

    @staticmethod
    def _whole_days_between(start: datetime, end: datetime) -> int:
        """Calendar-day count between two timezone-aware datetimes.

        ``max(0, ...)`` guards against same-instant points returning a negative
        number due to floating point noise — a zero-day drawdown is legitimate.
        """
        delta = end - start
        return max(0, int(delta.total_seconds() // 86400))

    @staticmethod
    def _empty_summary(*, symbol: str | None, days: int) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "period_days": days,
            "peak_pnl": 0.0,
            "current_pnl": 0.0,
            "current_drawdown": 0.0,
            "current_drawdown_pct": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_date": None,
            "max_drawdown_duration_days": 0,
            "recovery_count": 0,
            "avg_recovery_days": 0.0,
            "is_in_drawdown": False,
        }
