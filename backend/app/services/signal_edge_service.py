"""Signal edge evidence — read-only view over Strategy v2 shadow trades.

Answers "does this signal have edge at all?" before anyone tunes its exits.
The computation itself lives in ``app.domain.strategy_v2.signal_edge``; this
layer only supplies the evidence and shapes the response.

Read-only: never mutates shadow evidence, strategy config, or the order path.
It produces evidence for human review, never an automatic promotion.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.strategy_v2.signal_edge import (
    DEFAULT_ALPHA,
    DEFAULT_MIN_DISTINCT_DAYS,
    DEFAULT_MIN_RESOLVED_TRADES,
    DEFAULT_T_CRITICAL,
    SignalEdgeVerdict,
    assess_first_passage,
    assess_signal_edge,
    clustered_t_test,
)
from app.models import StrategyV2ShadowConfig, StrategyV2ShadowTrade

EXIT_TARGET = "PROFIT_TARGET"
EXIT_STOP = "PRICE_STOP"


class SignalEdgeService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def assess(
        self,
        *,
        symbol: str | None = None,
        lookback_days: int = 90,
        stop_pct: float | None = None,
        target_pct: float | None = None,
        alpha: float = DEFAULT_ALPHA,
        t_critical: float = DEFAULT_T_CRITICAL,
        min_resolved_trades: int = DEFAULT_MIN_RESOLVED_TRADES,
        min_distinct_days: int = DEFAULT_MIN_DISTINCT_DAYS,
        now: datetime | None = None,
    ) -> tuple[SignalEdgeVerdict, float, float, str | None]:
        """Return the verdict plus the barrier distances it was judged against."""
        if lookback_days <= 0:
            raise ValueError("lookback_days must be positive")

        normalized = (symbol or "").strip().upper() or None
        resolved_stop, resolved_target = self._barriers(
            normalized, stop_pct, target_pct
        )

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
        query = select(
            StrategyV2ShadowTrade.exit_reason,
            StrategyV2ShadowTrade.exit_at,
            StrategyV2ShadowTrade.net_pnl,
            StrategyV2ShadowTrade.entry_price,
            StrategyV2ShadowTrade.quantity,
        ).where(
            StrategyV2ShadowTrade.status != "OPEN",
            StrategyV2ShadowTrade.exit_at.is_not(None),
            StrategyV2ShadowTrade.exit_at >= cutoff,
        )
        if normalized:
            query = query.where(StrategyV2ShadowTrade.symbol == normalized)

        target_hits = 0
        stop_hits = 0
        observations: list[tuple[date, float]] = []
        for exit_reason, exit_at, net_pnl, entry_price, quantity in self._db.execute(
            query
        ).all():
            if exit_reason == EXIT_TARGET:
                target_hits += 1
            elif exit_reason == EXIT_STOP:
                stop_hits += 1
            if exit_at is None or net_pnl is None:
                continue
            notional = float(entry_price or 0) * float(quantity or 0)
            if notional <= 0:
                continue
            # Percent return keeps symbols of different price levels comparable;
            # raw dollar PnL would let one expensive symbol dominate the mean.
            observations.append((exit_at.date(), float(net_pnl) / notional * 100.0))

        first_passage = assess_first_passage(
            target_hits=target_hits,
            stop_hits=stop_hits,
            stop_pct=resolved_stop,
            target_pct=resolved_target,
            alpha=alpha,
        )
        clustered = clustered_t_test(observations, t_critical=t_critical)
        verdict = assess_signal_edge(
            first_passage=first_passage,
            clustered=clustered,
            min_resolved_trades=min_resolved_trades,
            min_distinct_days=min_distinct_days,
        )
        return verdict, resolved_stop, resolved_target, normalized

    def _barriers(
        self,
        symbol: str | None,
        stop_pct: float | None,
        target_pct: float | None,
    ) -> tuple[float, float]:
        """Resolve the barrier distances the trades were actually judged by.

        Explicit overrides win; otherwise the shadow config the trades ran under
        is authoritative. Judging recorded outcomes against barriers they never
        used would silently compare unlike things.
        """
        if stop_pct is not None and target_pct is not None:
            return float(stop_pct), float(target_pct)

        query = select(
            StrategyV2ShadowConfig.stop_loss_pct,
            StrategyV2ShadowConfig.profit_target_pct,
        )
        if symbol:
            query = query.where(StrategyV2ShadowConfig.symbol == symbol)
        row = self._db.execute(query.limit(1)).first()
        if row is None:
            raise ValueError(
                "no shadow config available to derive barrier distances; "
                "pass stop_pct and target_pct explicitly"
            )
        config_stop, config_target = row
        return (
            float(stop_pct if stop_pct is not None else config_stop),
            float(target_pct if target_pct is not None else config_target),
        )


__all__ = ["SignalEdgeService"]
