"""Automatic primary-symbol switching driven by range-strategy fitness.

DEFAULT OFF. Enabling this deliberately relaxes the repository's standing rule
that candidate-pool and shadow evidence never switch the live trading symbol
(see README: "不会自动切换主交易标的"). It promotes on fitness evidence alone
and does NOT require the forward profit evidence that
``/api/universe/promotion-readiness`` demands, so a promoted symbol may have no
closed-trade track record.

Two invariants bound the blast radius:

* Only symbols the latest completed selection run marked ``selected`` are
  eligible, so an arbitrary symbol can never become the live primary.
* The switch is executed through ``AppRunner.assert_primary_switch_safe``,
  which refuses while a position, pending order, in-flight trigger, unresolved
  reconciliation, or non-FLAT engine state exists. A switch therefore never
  abandons live exposure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    StrategyConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.range_fitness_service import (
    RangeFitnessService,
    VERDICT_RANGE_SUITABLE,
)

logger = logging.getLogger("auto_trade.auto_primary_switch")

OUTCOME_DISABLED = "DISABLED"
OUTCOME_NO_PRIMARY = "NO_PRIMARY_CONFIGURED"
OUTCOME_INCUMBENT_ACCEPTABLE = "INCUMBENT_ACCEPTABLE"
OUTCOME_INCUMBENT_EVIDENCE_THIN = "INCUMBENT_EVIDENCE_THIN"
OUTCOME_NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
OUTCOME_SWITCH_BLOCKED = "SWITCH_BLOCKED"
OUTCOME_SWITCHED = "SWITCHED"


@dataclass(frozen=True)
class AutoPrimarySwitchResult:
    outcome: str
    incumbent: str = ""
    incumbent_trend_pct: float | None = None
    candidate: str = ""
    candidate_trend_pct: float | None = None
    detail: str = ""


class AutoPrimarySwitchService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def evaluate(self, runner: Any) -> AutoPrimarySwitchResult:
        if not settings.auto_primary_switch_enabled:
            return AutoPrimarySwitchResult(OUTCOME_DISABLED)

        config = self._db.scalar(
            select(StrategyConfig).order_by(StrategyConfig.id.desc())
        )
        incumbent = (config.symbol or "").strip().upper() if config else ""
        if not incumbent:
            return AutoPrimarySwitchResult(OUTCOME_NO_PRIMARY)

        rows = RangeFitnessService(self._db).assess(
            lookback_days=settings.auto_primary_switch_lookback_days,
            min_samples=settings.auto_primary_switch_min_samples,
            trend_unsuitable_pct=(
                settings.auto_primary_switch_incumbent_trend_pct
            ),
            range_suitable_pct=(
                settings.auto_primary_switch_candidate_trend_pct
            ),
        )
        by_symbol = {row.symbol: row for row in rows}

        incumbent_row = by_symbol.get(incumbent)
        if incumbent_row is None or incumbent_row.samples < settings.auto_primary_switch_min_samples:
            return AutoPrimarySwitchResult(
                OUTCOME_INCUMBENT_EVIDENCE_THIN,
                incumbent=incumbent,
                detail="incumbent has insufficient fitness evidence",
            )
        if incumbent_row.trend_blocked_pct < settings.auto_primary_switch_incumbent_trend_pct:
            return AutoPrimarySwitchResult(
                OUTCOME_INCUMBENT_ACCEPTABLE,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
            )

        eligible = self._eligible_candidates()
        best = None
        for row in rows:
            if row.symbol == incumbent or row.symbol not in eligible:
                continue
            if row.verdict != VERDICT_RANGE_SUITABLE:
                continue
            if row.trend_blocked_pct > settings.auto_primary_switch_candidate_trend_pct:
                continue
            # Without a reference price the new symbol would inherit the old
            # symbol's interval, which sits nowhere near its price and would
            # never trigger — a switch into an immediately dead interval.
            if row.last_close_price is None or row.last_close_price <= 0:
                continue
            if best is None or row.trend_blocked_pct < best.trend_blocked_pct:
                best = row
        if best is None:
            return AutoPrimarySwitchResult(
                OUTCOME_NO_ELIGIBLE_CANDIDATE,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                detail="no selected candidate is range-suitable",
            )

        market = eligible[best.symbol]
        try:
            runner.assert_primary_switch_safe(best.symbol, market)
        except Exception as exc:
            return AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail=str(exc),
            )

        reference_price = float(best.last_close_price or 0)
        half_width = reference_price * (
            settings.llm_interval_volatility_threshold_pct / 100
        )
        buy_low = round(reference_price - half_width, 4)
        sell_high = round(reference_price + half_width, 4)
        if buy_low <= 0 or sell_high <= buy_low:
            return AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail="cannot derive a valid interval for the candidate",
            )

        try:
            self._commit_switch(
                runner,
                config,
                best.symbol,
                market,
                buy_low,
                sell_high,
            )
        except Exception as exc:
            logger.exception("automatic primary switch failed to persist")
            return AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail=f"persist failed: {exc}",
            )

        logger.warning(
            "automatic primary switch: %s (trend %.2f%%) -> %s (trend %.2f%%)",
            incumbent,
            incumbent_row.trend_blocked_pct,
            best.symbol,
            best.trend_blocked_pct,
        )
        return AutoPrimarySwitchResult(
            OUTCOME_SWITCHED,
            incumbent=incumbent,
            incumbent_trend_pct=incumbent_row.trend_blocked_pct,
            candidate=best.symbol,
            candidate_trend_pct=best.trend_blocked_pct,
            detail=market,
        )

    def _commit_switch(
        self,
        runner: Any,
        config: Any,
        symbol: str,
        market: str,
        buy_low: float,
        sell_high: float,
    ) -> None:
        """Persist the new primary symbol and reload the live runner.

        The safety gate is already proven by the caller's
        ``assert_primary_switch_safe`` call, so this deliberately does not go
        through ``update_strategy_with_runtime_reload`` — that helper re-runs the
        gate against the process-global runner, which would both duplicate the
        check and ignore the runner this service was handed.

        A reload failure rolls the symbol back so the live engine is never left
        pointing at a half-applied config.
        """
        from app.services.strategy_service import StrategyService

        svc = StrategyService(self._db)
        previous_symbol = (config.symbol or "").strip().upper()
        previous_market = (config.market or "US").strip().upper()
        previous_buy_low = float(config.buy_low or 0)
        previous_sell_high = float(config.sell_high or 0)
        reload_strategy = getattr(runner, "reload_strategy", None)
        svc.update_config({
            "symbol": symbol,
            "market": market,
            "buy_low": buy_low,
            "sell_high": sell_high,
        })
        try:
            if callable(reload_strategy):
                reload_strategy()
        except Exception:
            logger.exception(
                "automatic primary switch reload failed; rolling back to %s",
                previous_symbol,
            )
            svc.update_config({
                "symbol": previous_symbol,
                "market": previous_market,
                "buy_low": previous_buy_low,
                "sell_high": previous_sell_high,
            })
            try:
                if callable(reload_strategy):
                    reload_strategy()
            except Exception:
                logger.critical(
                    "automatic primary switch rollback reload failed",
                    exc_info=True,
                )
            raise

    def _eligible_candidates(self) -> dict[str, str]:
        run = self._db.scalar(
            select(UniverseSelectionRun)
            .where(UniverseSelectionRun.status == "COMPLETE")
            .order_by(UniverseSelectionRun.id.desc())
        )
        if run is None:
            return {}
        rows = self._db.execute(
            select(
                UniverseSelectionCandidate.symbol,
                UniverseSelectionCandidate.market,
            ).where(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.selected.is_(True),
            )
        ).all()
        return {
            str(symbol).strip().upper(): str(market or "US").strip().upper()
            for symbol, market in rows
            if str(symbol or "").strip()
        }
