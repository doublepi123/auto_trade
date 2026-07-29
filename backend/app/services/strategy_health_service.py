"""Strategy health monitor: live vs shadow drift detection.

Compares the recent performance of live trading (closed round trips from the
order ledger) against the forward-only Strategy v2 shadow trades
(:class:`StrategyV2ShadowTrade`). When the two diverge beyond configured
thresholds the report flags a drift so a human can investigate before any
shadow path is considered for promotion.

Health thresholds (absolute percentage-point drift):
  * ``HEALTHY``            — drift < 10 %
  * ``WARNING``            — 10 % <= drift < 30 %
  * ``DEGRADED``           — drift >= 30 %
  * ``INSUFFICIENT_DATA``  — either side has too few trades to be meaningful.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.trades import _active_fee_rates
from app.models import StrategyV2ShadowTrade
from app.services.daily_pnl_service import ClosedRoundTrip, DailyPnlService


def _ensure_utc(ts: datetime) -> datetime:
    """Coerce a possibly-naive datetime to aware UTC.

    SQLite does not round-trip timezone information on ``DateTime`` columns
    deterministically, so dates loaded from the ledger can arrive naive while
    in-memory bounds (``datetime.now(timezone.utc) - timedelta(...)``) are
    aware. Comparing the two raises ``TypeError``; this normalises both sides
    to aware UTC before any comparison.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)

# A trade is a "win" when its net PnL is strictly positive (matches the
# risk controller's win/loss classification).
_WIN_PNL_EPS = 1e-9

# Minimum sample size per side below which we refuse to score health.
_MIN_TRADES_FOR_VERDICT = 3

# Drift thresholds expressed as absolute percentage points (0.10 = 10 %).
_DRIFT_HEALTHY = 0.10
_DRIFT_DEGRADED = 0.30

# A shadow trades-per-day below this is treated as "too sparse to compare".
_MIN_SHADOW_TRADES_PER_DAY = 0.1


@dataclass(frozen=True)
class _TradeMetrics:
    """Aggregated win-rate / PnL / holding metrics for one side (live or shadow)."""

    win_rate: float
    avg_pnl: float
    trade_count: int
    avg_holding_minutes: float
    profit_factor: float


@dataclass(frozen=True)
class _ShadowRow:
    """Materialised shadow-trade fields, detached from the ORM session.

    ``StrategyV2ShadowTrade`` rows share the service session with the live
    ledger replay; loading them into a plain dataclass up-front avoids
    DetachedInstanceError when the live-side replay commits mid-report.
    """

    net_pnl: float
    entry_at: datetime | None
    exit_at: datetime | None
    holding_seconds: float | None


class StrategyHealthService:
    """Compare live trading performance against the Strategy v2 shadow."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_health_report(self, symbol: str | None = None) -> dict[str, Any]:
        """Return a single-shot health verdict for the trailing 30-day window.

        Computes win-rate / avg-PnL / trade-count / avg-holding /
        profit-factor for each side, then derives three drift metrics
        (win-rate, avg-PnL, trade-frequency, all in percentage points) and a
        single rolled-up ``health_status`` plus a list of human-readable
        ``alerts``.
        """
        from_dt = datetime.now(timezone.utc) - timedelta(days=30)

        live_trips = self._load_live_trips(symbol=symbol, from_dt=from_dt)
        shadow_trades = self._load_shadow_trades(symbol=symbol, from_dt=from_dt)

        live_metrics = self._summarize_live(live_trips)
        shadow_metrics = self._summarize_shadow(shadow_trades)

        drift = self._compute_drift(live_metrics, shadow_metrics)
        health_status, alerts = self._classify_health(
            live_metrics=live_metrics,
            shadow_metrics=shadow_metrics,
            drift=drift,
            from_dt=from_dt,
        )

        return {
            "symbol": symbol,
            "period_days": 30,
            "live_metrics": live_metrics,
            "shadow_metrics": shadow_metrics,
            "drift": drift,
            "health_status": health_status,
            "alerts": alerts,
        }

    def get_performance_trend(
        self, symbol: str | None = None, weeks: int = 8
    ) -> list[dict[str, Any]]:
        """Weekly win-rate / avg-PnL / trade-count comparison live vs shadow.

        Buckets both sides into ISO-week (Mon-Sun UTC) buckets ending at the
        current week, returning one row per week in chronological order.
        """
        weeks = max(1, int(weeks))
        now = datetime.now(timezone.utc)
        # Align to the start of the current ISO week (Monday).
        start_of_current_week = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        window_start = start_of_current_week - timedelta(weeks=weeks - 1)

        live_trips = self._load_live_trips(symbol=symbol, from_dt=window_start)
        shadow_trades = self._load_shadow_trades(symbol=symbol, from_dt=window_start)

        # Build per-week buckets.
        buckets: dict[str, dict[str, list[float]]] = {}
        for i in range(weeks):
            week_start = window_start + timedelta(weeks=i)
            key = week_start.date().isoformat()
            buckets[key] = {"live_pnl": [], "shadow_pnl": []}

        for trip in live_trips:
            key = self._week_key(trip.exit_at, window_start, weeks)
            if key and key in buckets:
                buckets[key]["live_pnl"].append(float(trip.net_pnl))

        for trade in shadow_trades:
            if trade.exit_at is None:
                continue
            key = self._week_key(trade.exit_at, window_start, weeks)
            if key and key in buckets:
                buckets[key]["shadow_pnl"].append(trade.net_pnl)

        rows: list[dict[str, Any]] = []
        for key in sorted(buckets):
            live_pnls = buckets[key]["live_pnl"]
            shadow_pnls = buckets[key]["shadow_pnl"]
            rows.append(
                {
                    "week_start": key,
                    "live_win_rate": self._win_rate(live_pnls),
                    "shadow_win_rate": self._win_rate(shadow_pnls),
                    "live_avg_pnl": round(sum(live_pnls) / len(live_pnls), 2)
                    if live_pnls
                    else 0.0,
                    "shadow_avg_pnl": round(sum(shadow_pnls) / len(shadow_pnls), 2)
                    if shadow_pnls
                    else 0.0,
                    "live_trades": len(live_pnls),
                    "shadow_trades": len(shadow_pnls),
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Live side (closed round trips)
    # ------------------------------------------------------------------
    def _load_live_trips(
        self, *, symbol: str | None, from_dt: datetime
    ) -> list[ClosedRoundTrip]:
        fee_rate_us, fee_rate_hk = _active_fee_rates(self._db)
        return DailyPnlService(self._db).pair_round_trips(
            symbol=symbol,
            from_dt=from_dt,
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
        )

    def _summarize_live(self, trips: list[ClosedRoundTrip]) -> dict[str, Any]:
        if not trips:
            return self._empty_metrics()
        pnls = [float(t.net_pnl) for t in trips]
        holding_minutes = [
            float(t.holding_seconds) / 60.0 for t in trips if t.holding_seconds
        ]
        metrics = _TradeMetrics(
            win_rate=self._win_rate(pnls),
            avg_pnl=round(sum(pnls) / len(pnls), 2),
            trade_count=len(pnls),
            avg_holding_minutes=round(sum(holding_minutes) / len(holding_minutes), 2)
            if holding_minutes
            else 0.0,
            profit_factor=self._profit_factor(pnls),
        )
        return self._metrics_to_dict(metrics)

    # ------------------------------------------------------------------
    # Shadow side (Strategy v2 forward shadow trades)
    # ------------------------------------------------------------------
    def _load_shadow_trades(
        self, *, symbol: str | None, from_dt: datetime
    ) -> list[_ShadowRow]:
        """Load closed shadow trades as plain rows (detached from the session).

        Materialising the fields we need up-front avoids DetachedInstanceError
        when :meth:`_load_live_trips` (which calls
        ``DailyPnlService.pair_round_trips``) commits the shared session and
        expires ORM state mid-report.
        """
        query = select(StrategyV2ShadowTrade).where(
            StrategyV2ShadowTrade.status == "CLOSED",
            StrategyV2ShadowTrade.exit_at.is_not(None),
            StrategyV2ShadowTrade.exit_at >= from_dt,
        )
        if symbol:
            query = query.where(StrategyV2ShadowTrade.symbol == symbol.strip().upper())
        query = query.order_by(StrategyV2ShadowTrade.exit_at.asc())
        rows: list[_ShadowRow] = []
        for trade in self._db.execute(query).scalars().all():
            rows.append(
                _ShadowRow(
                    net_pnl=float(trade.net_pnl or 0.0),
                    entry_at=trade.entry_at,
                    exit_at=trade.exit_at,
                    holding_seconds=(
                        float(trade.holding_seconds)
                        if trade.holding_seconds is not None
                        else None
                    ),
                )
            )
        return rows

    def _summarize_shadow(self, trades: list[_ShadowRow]) -> dict[str, Any]:
        if not trades:
            return self._empty_metrics()
        pnls = [t.net_pnl for t in trades]
        holding_minutes = [
            self._holding_minutes(t) for t in trades if t.exit_at is not None
        ]
        metrics = _TradeMetrics(
            win_rate=self._win_rate(pnls),
            avg_pnl=round(sum(pnls) / len(pnls), 2),
            trade_count=len(pnls),
            avg_holding_minutes=round(sum(holding_minutes) / len(holding_minutes), 2)
            if holding_minutes
            else 0.0,
            profit_factor=self._profit_factor(pnls),
        )
        return self._metrics_to_dict(metrics)

    @staticmethod
    def _holding_minutes(trade: _ShadowRow) -> float:
        """Minutes between entry and exit (0 if either side is missing)."""
        if trade.entry_at is None or trade.exit_at is None:
            return 0.0
        if trade.holding_seconds is not None:
            return trade.holding_seconds / 60.0
        delta = trade.exit_at - trade.entry_at
        return max(0.0, delta.total_seconds() / 60.0)

    # ------------------------------------------------------------------
    # Drift + health verdict
    # ------------------------------------------------------------------
    def _compute_drift(
        self,
        live: dict[str, Any],
        shadow: dict[str, Any],
    ) -> dict[str, Any]:
        """Percentage-point drift of each metric (live vs shadow)."""
        return {
            "win_rate_drift": round(live["win_rate"] - shadow["win_rate"], 4),
            "pnl_drift": round(live["avg_pnl"] - shadow["avg_pnl"], 2),
            "trade_frequency_drift": round(
                self._relative_drift(live["trade_count"], shadow["trade_count"]), 4
            ),
        }

    def _classify_health(
        self,
        *,
        live_metrics: dict[str, Any],
        shadow_metrics: dict[str, Any],
        drift: dict[str, Any],
        from_dt: datetime,
    ) -> tuple[str, list[str]]:
        alerts: list[str] = []
        live_n = live_metrics["trade_count"]
        shadow_n = shadow_metrics["trade_count"]

        # --- Insufficient data guards -------------------------------------
        if live_n < _MIN_TRADES_FOR_VERDICT and shadow_n < _MIN_TRADES_FOR_VERDICT:
            return "INSUFFICIENT_DATA", [
                "both live and shadow have fewer than "
                f"{_MIN_TRADES_FOR_VERDICT} trades in the trailing window"
            ]
        if shadow_n < _MIN_TRADES_FOR_VERDICT:
            return "INSUFFICIENT_DATA", [
                f"shadow has only {shadow_n} trades in the trailing window "
                f"(need >= {_MIN_TRADES_FOR_VERDICT}); cannot score drift"
            ]
        if live_n < _MIN_TRADES_FOR_VERDICT:
            return "INSUFFICIENT_DATA", [
                f"live has only {live_n} trades in the trailing window "
                f"(need >= {_MIN_TRADES_FOR_VERDICT}); cannot score drift"
            ]

        # Shadow must also be dense enough across the window, otherwise a
        # handful of shadow trades early in the window would masquerade as a
        # stable baseline.
        days_in_window = max(
            1.0,
            (datetime.now(timezone.utc) - from_dt).total_seconds() / 86400.0,
        )
        shadow_per_day = shadow_n / days_in_window
        if shadow_per_day < _MIN_SHADOW_TRADES_PER_DAY:
            return "INSUFFICIENT_DATA", [
                f"shadow trade frequency too sparse ({shadow_per_day:.3f}/day); "
                "baseline is not representative"
            ]

        # --- Drift magnitude classification -------------------------------
        # Use the largest absolute percentage-point drift across win-rate and
        # trade-frequency as the governing magnitude.
        win_drift = abs(drift["win_rate_drift"])
        freq_drift = abs(drift["trade_frequency_drift"])
        governing = max(win_drift, freq_drift)

        if governing < _DRIFT_HEALTHY:
            status = "HEALTHY"
        elif governing < _DRIFT_DEGRADED:
            status = "WARNING"
            alerts.append(
                f"win-rate drift {win_drift * 100:.1f} pp exceeds {_DRIFT_HEALTHY * 100:.0f} pp"
            )
        else:
            status = "DEGRADED"
            alerts.append(
                f"drift {governing * 100:.1f} pp exceeds "
                f"{_DRIFT_DEGRADED * 100:.0f} pp threshold"
            )

        # Always surface the PnL drift as an informational alert when material.
        if abs(drift["pnl_drift"]) > 1.0:
            alerts.append(
                f"avg PnL drift {drift['pnl_drift']:+.2f} (live {live_metrics['avg_pnl']}"
                f" vs shadow {shadow_metrics['avg_pnl']})"
            )

        return status, alerts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _win_rate(pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        wins = sum(1 for p in pnls if p > _WIN_PNL_EPS)
        return round(wins / len(pnls), 4)

    @staticmethod
    def _profit_factor(pnls: list[float]) -> float:
        """Gross profit / gross loss (Infinity when there are no losers)."""
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        if gross_loss <= 0:
            # No losers: profit factor is undefined; mirror standard backtest
            # tooling by reporting the gross profit (Infinity only when there
            # are also winners; 0 when the side is entirely break-even).
            return float("inf") if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 4)

    @staticmethod
    def _relative_drift(a: int, b: int) -> float:
        """Signed relative drift of two counts vs the shadow baseline ``b``.

        Returns ``(a - b) / b`` as a fraction (0.0 when ``b`` is 0). The live
        side is the subject; the shadow baseline is the reference, so a
        positive value means live is more active than shadow.
        """
        if b <= 0:
            return 0.0
        return (a - b) / b

    @staticmethod
    def _metrics_to_dict(metrics: _TradeMetrics) -> dict[str, Any]:
        # JSON cannot represent Infinity — coerce to a large finite number so
        # the response stays serialisable for the Pydantic router model.
        pf = metrics.profit_factor
        if pf == float("inf"):
            pf = 1e9
        return {
            "win_rate": metrics.win_rate,
            "avg_pnl": metrics.avg_pnl,
            "trade_count": metrics.trade_count,
            "avg_holding_minutes": metrics.avg_holding_minutes,
            "profit_factor": pf,
        }

    @staticmethod
    def _empty_metrics() -> dict[str, Any]:
        return {
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "trade_count": 0,
            "avg_holding_minutes": 0.0,
            "profit_factor": 0.0,
        }

    @staticmethod
    def _week_key(
        ts: datetime, window_start: datetime, weeks: int
    ) -> str | None:
        """Return the ISO date of the Monday of the week containing ``ts``.

        ``None`` when ``ts`` falls outside ``[window_start, window_start +
        weeks)`` so the caller can drop out-of-window points cleanly.

        SQLite does not persist timezone information on ``DateTime`` columns
        reliably, so both bounds are coerced to aware-UTC before comparison to
        avoid ``TypeError: can't compare offset-naive and offset-aware``.
        """
        ts_utc = _ensure_utc(ts)
        start_utc = _ensure_utc(window_start)
        if ts_utc < start_utc:
            return None
        offset_weeks = int((ts_utc - start_utc).days // 7)
        if offset_weeks >= weeks:
            return None
        week_start = start_utc + timedelta(weeks=offset_weeks)
        return week_start.date().isoformat()
