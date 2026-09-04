"""Signal edge evidence — read-only view over Strategy v2 shadow trades.

Answers "does this signal have edge at all?" before anyone tunes its exits.
The computation itself lives in ``app.domain.strategy_v2.signal_edge``; this
layer only supplies the evidence and shapes the response.

Read-only: never mutates shadow evidence, strategy config, or the order path.
It produces evidence for human review, never an automatic promotion.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from app.models import (
    StrategyV2ShadowConfig,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.services.strategy_v2_shadow_service import _ALGORITHM_VERSION

EXIT_TARGET = "PROFIT_TARGET"
EXIT_STOP = "PRICE_STOP"
# Time-barrier exits: the position closed on elapsed time, not on a price
# barrier, so it carries no first-passage outcome and leaves that denominator.
EXIT_TIME_BARRIER = frozenset({"MAX_HOLD", "EOD_FLATTEN"})


class _BarrierSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    stop_loss_pct: float = Field(gt=0)
    profit_target_pct: float = Field(gt=0)
    algorithm_version: str | None = None


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
        matching_versions = self._matching_barrier_versions(
            normalized,
            (resolved_stop, resolved_target),
            current_algorithm_only=normalized is None,
        )

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
        query = select(
            StrategyV2ShadowTrade.symbol,
            StrategyV2ShadowTrade.config_version,
            StrategyV2ShadowTrade.exit_reason,
            StrategyV2ShadowTrade.exit_at,
            StrategyV2ShadowTrade.gross_pnl,
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
        time_exit_excluded = 0
        barrier_mismatch_excluded = 0
        matched_trades = 0
        provenance_excluded_trades = 0
        missing_pnl_excluded = 0
        gross_observations: list[tuple[date, float]] = []
        net_observations: list[tuple[date, float]] = []
        for (
            trade_symbol,
            config_version,
            exit_reason,
            exit_at,
            gross_pnl,
            net_pnl,
            entry_price,
            quantity,
        ) in self._db.execute(query).all():
            if (str(trade_symbol), str(config_version)) not in matching_versions:
                provenance_excluded_trades += 1
                if exit_reason == EXIT_TARGET or exit_reason == EXIT_STOP:
                    barrier_mismatch_excluded += 1
                continue
            matched_trades += 1
            if exit_reason == EXIT_TARGET:
                target_hits += 1
            elif exit_reason == EXIT_STOP:
                stop_hits += 1
            elif exit_reason in EXIT_TIME_BARRIER:
                time_exit_excluded += 1
            if exit_at is None:
                missing_pnl_excluded += 1
                continue
            notional = float(entry_price or 0) * float(quantity or 0)
            if (
                not math.isfinite(notional)
                or notional <= 0
                or gross_pnl is None
                or net_pnl is None
            ):
                missing_pnl_excluded += 1
                continue
            # Percent return keeps symbols of different price levels comparable;
            # raw dollar PnL would let one expensive symbol dominate the mean.
            gross_return = float(gross_pnl) / notional * 100.0
            net_return = float(net_pnl) / notional * 100.0
            if not math.isfinite(gross_return) or not math.isfinite(net_return):
                missing_pnl_excluded += 1
                continue
            trading_day = exit_at.date()
            gross_observations.append((trading_day, gross_return))
            net_observations.append((trading_day, net_return))

        first_passage = assess_first_passage(
            target_hits=target_hits,
            stop_hits=stop_hits,
            stop_pct=resolved_stop,
            target_pct=resolved_target,
            alpha=alpha,
            barrier_mismatch_excluded=barrier_mismatch_excluded,
            matched_versions=len(matching_versions),
            matched_trades=matched_trades,
            provenance_excluded_trades=provenance_excluded_trades,
            missing_pnl_excluded=missing_pnl_excluded,
            time_exit_excluded=time_exit_excluded,
        )
        gross = clustered_t_test(gross_observations, t_critical=t_critical)
        net = clustered_t_test(net_observations, t_critical=t_critical)
        verdict = assess_signal_edge(
            first_passage=first_passage,
            clustered=net,
            gross=gross,
            min_resolved_trades=min_resolved_trades,
            min_distinct_days=min_distinct_days,
        )
        return verdict, resolved_stop, resolved_target, normalized

    def _matching_barrier_versions(
        self,
        symbol: str | None,
        barriers: tuple[float, float],
        *,
        current_algorithm_only: bool,
    ) -> frozenset[tuple[str, str]]:
        # Historical attribution must come from immutable version snapshots.
        # Falling back to mutable current config could attribute barriers to a
        # trade that never used them; missing snapshots stay excluded and reported.
        query = select(
            StrategyV2ShadowVersion.symbol,
            StrategyV2ShadowVersion.config_version,
            StrategyV2ShadowVersion.config_json,
        )
        if symbol:
            query = query.where(StrategyV2ShadowVersion.symbol == symbol)

        stop_pct, target_pct = barriers
        matching: set[tuple[str, str]] = set()
        for row_symbol, config_version, config_json in self._db.execute(query).all():
            try:
                snapshot = _BarrierSnapshot.model_validate_json(config_json)
            except ValidationError:
                continue
            if (
                current_algorithm_only
                and snapshot.algorithm_version != _ALGORITHM_VERSION
            ):
                continue
            if math.isclose(
                snapshot.stop_loss_pct,
                stop_pct,
                rel_tol=0.0,
                abs_tol=1e-12,
            ) and math.isclose(
                snapshot.profit_target_pct,
                target_pct,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                matching.add((str(row_symbol), str(config_version)))
        return frozenset(matching)

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
        else:
            rows = self._db.execute(
                query.where(
                    StrategyV2ShadowConfig.enabled.is_(True),
                    StrategyV2ShadowConfig.symbol.like("%.US"),
                ).order_by(StrategyV2ShadowConfig.symbol.asc())
            ).all()
            barrier_pairs = {
                (float(config_stop), float(config_target))
                for config_stop, config_target in rows
            }
            if not barrier_pairs:
                raise ValueError(
                    "no enabled current US/v5 shadow config is registered"
                )
            if len(barrier_pairs) != 1:
                cohorts = ", ".join(
                    f"{stop:.12g}/{target:.12g}"
                    for stop, target in sorted(barrier_pairs)
                )
                raise ValueError(
                    "enabled current US/v5 shadow configs span multiple "
                    + f"barrier cohorts: {cohorts}"
                )
            config_stop, config_target = next(iter(barrier_pairs))
        return (
            float(stop_pct if stop_pct is not None else config_stop),
            float(target_pct if target_pct is not None else config_target),
        )


__all__ = ["SignalEdgeService"]
