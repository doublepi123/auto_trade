"""Read-only primary candidacy: distinguish evidence, symbol gates and costs."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import mean
from types import FunctionType
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.strategy_v2.selection_power import assess_selection_power, reach_gate_operating_point
from app.domain.universe_selection.tradeability import TradeabilityInput, rank_tradeability
from app.models import StrategyConfig, StrategyV2ShadowTrade, UniverseSelectionCandidate, UniverseSelectionRun
from app.schemas import (
    EntryWindowPoolResponse, PrimaryCandidacyCandidate, PrimaryCandidacyEdgePick,
    PrimaryCandidacyGateParameters, PrimaryCandidacyGatesOnlyPick,
    PrimaryCandidacyPoolGate, PrimaryCandidacyPower, PrimaryCandidacyResponse,
    PrimaryCandidacyTradeabilityPick, PrimaryCandidacyTradeabilityRow, UniverseSelectionMetrics,
)
from app.services import auto_primary_switch_service as switch
from app.services.entry_window_overlap_service import EntryWindowOverlapService
from app.services.range_fitness_service import RangeFitnessService

PrimaryCandidacyReport = PrimaryCandidacyResponse


def _gate_functions(parameters: PrimaryCandidacyGateParameters) -> tuple[FunctionType, FunctionType]:
    """Bind existing gate code to a request-local settings snapshot.

    The shared functions have no settings argument. Copying their namespace
    preserves their exact comparisons without mutating the live module or
    duplicating its gates. The freshness helper needs the same local binding.
    """
    snapshot = settings.model_copy(update={
        **{f"auto_primary_switch_{key}": value for key, value in parameters.model_dump().items()},
        "auto_primary_switch_require_signal_edge": True,
    })
    namespace = {**vars(switch), "settings": snapshot}
    namespace["_reference_is_fresh"] = FunctionType(switch._reference_is_fresh.__code__, namespace)
    return (
        FunctionType(switch.classify_candidate_row.__code__, namespace),
        FunctionType(switch.AutoPrimarySwitchService._signal_edge_block_reason.__code__, namespace),
    )


class PrimaryCandidacyService:
    def __init__(self, db: Session, *,
        signal_edge_assessor: Callable[[], object | None] | None = None,
        entry_window_assessor: Callable[..., object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._signal_edge_assessor = signal_edge_assessor
        self._entry_window_assessor = entry_window_assessor
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def assess(self, *, delta_bps: float = 20.0, alpha: float = 0.05,
        power: float = 0.8, include_entry_windows: bool = False,
        lookback_days: int | None = None, min_samples: int | None = None,
        incumbent_trend_pct: float | None = None, candidate_trend_pct: float | None = None,
        reach_lookback_days: int | None = None, min_reach_rate_pct: float | None = None,
        min_closed_trades: int | None = None, max_price_age_seconds: int | None = None,
    ) -> PrimaryCandidacyReport:
        """Compute the whole report even when switching or evidence gates block."""
        overrides = {
            "lookback_days": lookback_days, "min_samples": min_samples,
            "incumbent_trend_pct": incumbent_trend_pct, "candidate_trend_pct": candidate_trend_pct,
            "reach_lookback_days": reach_lookback_days, "min_reach_rate_pct": min_reach_rate_pct,
            "min_closed_trades": min_closed_trades, "max_price_age_seconds": max_price_age_seconds,
        }
        parameters = PrimaryCandidacyGateParameters.model_validate({
            key: value if value is not None else getattr(settings, f"auto_primary_switch_{key}")
            for key, value in overrides.items()
        })
        parameters = parameters.model_copy(update={"reach_lookback_days": max(parameters.lookback_days, parameters.reach_lookback_days)})
        anchor = switch._as_utc(self._clock())
        # Prevent even an incidental autoflush of caller-owned pending rows.
        with self._db.no_autoflush:
            return self._report(parameters, anchor, delta_bps=delta_bps, alpha=alpha,
                power=power, include_entry_windows=include_entry_windows)

    def _report(self, p: PrimaryCandidacyGateParameters, anchor: datetime, *,
        delta_bps: float, alpha: float, power: float, include_entry_windows: bool,
    ) -> PrimaryCandidacyReport:
        config = self._db.scalar(select(StrategyConfig).order_by(StrategyConfig.id.desc()))
        incumbent = (config.symbol or "").strip().upper() if config else ""
        fitness = RangeFitnessService(self._db).assess(lookback_days=p.lookback_days,
            min_samples=p.min_samples, trend_unsuitable_pct=p.incumbent_trend_pct,
            range_suitable_pct=p.candidate_trend_pct, reach_lookback_days=p.reach_lookback_days, now=anchor)
        current = next((row for row in fitness if row.symbol == incumbent), None)
        incumbent_status: Literal["EVIDENCE_THIN", "ACCEPTABLE", "TREND_UNSUITABLE"] = "EVIDENCE_THIN"
        if current is not None and current.samples >= p.min_samples:
            incumbent_status = "ACCEPTABLE" if current.trend_blocked_pct < p.incumbent_trend_pct else "TREND_UNSUITABLE"
        run = self._db.scalar(select(UniverseSelectionRun).where(UniverseSelectionRun.status == "COMPLETE").order_by(UniverseSelectionRun.id.desc()))
        selections = list(self._db.scalars(select(UniverseSelectionCandidate).where(UniverseSelectionCandidate.run_id == run.id)).all()) if run else []
        eligible = {row.symbol.strip().upper(): (row.market or "US").strip().upper() for row in selections if row.selected}
        classify, assess_edge = _gate_functions(p)
        try:
            block = self._signal_edge_assessor() if self._signal_edge_assessor is not None else assess_edge(switch.AutoPrimarySwitchService(self._db))
        except ValueError as exc:
            block = switch._SignalEdgeBlock(str(exc), True)
        if block is not None and not isinstance(block, switch._SignalEdgeBlock):
            block = switch._SignalEdgeBlock("Signal edge assessor returned an unsupported result", True)
        pool = PrimaryCandidacyPoolGate(status="PASS" if block is None else ("UNASSESSABLE" if block.unassessable else "BLOCKED"),
            detail="Pool signal edge and promotion evidence pass" if block is None else block.detail,
            enforced_by_switch=settings.auto_primary_switch_require_signal_edge)
        returns: dict[str, list[float]] = defaultdict(list)
        days: dict[str, set[date]] = defaultdict(set)
        for trade in self._db.scalars(select(StrategyV2ShadowTrade).where(
            StrategyV2ShadowTrade.status != "OPEN", StrategyV2ShadowTrade.exit_at >= anchor - timedelta(days=p.reach_lookback_days),
            StrategyV2ShadowTrade.exit_at <= anchor,
        )):
            if trade.exit_at is None or trade.net_pnl is None or trade.entry_price <= 0 or trade.quantity <= 0:
                continue
            bps = trade.net_pnl / (trade.entry_price * trade.quantity) * 10000
            if isfinite(bps):
                returns[trade.symbol].append(bps)
                days[trade.symbol].add(trade.exit_at.date())
        power_result = assess_selection_power(per_trade_returns_bps=[v for values in returns.values() for v in values],
            held_by_symbol={symbol: len(values) for symbol, values in sorted(returns.items())},
            delta_bps=delta_bps, alpha=alpha, power=power)
        required = power_result.required_one_sample
        power_report = PrimaryCandidacyPower.model_validate({**asdict(power_result),
            "reach_gate_operating_point": asdict(reach_gate_operating_point(n=p.min_closed_trades,
                min_rate_pct=p.min_reach_rate_pct, loser_reach_p=0.22, winner_reach_p=0.85))})
        candidates: list[PrimaryCandidacyCandidate] = []
        for row in fitness if run else []:
            gate: switch.CandidateGateVerdict = classify(row, incumbent=incumbent, eligible=eligible, anchor=anchor)
            values = returns.get(row.symbol, [])
            candidates.append(PrimaryCandidacyCandidate.model_validate({**asdict(row),
                "gate_reasons": list(gate.reasons), "passes_all_symbol_gates": gate.passes,
                "reference_age_seconds": gate.reference_age_seconds, "trades_held": len(values),
                "mean_net_bps": mean(values) if values else None, "distinct_days": len(days.get(row.symbol, set())),
                "power_share_pct": len(values) / required * 100 if required else None}))
        passers = sorted((row for row in candidates if row.passes_all_symbol_gates), key=lambda row: (row.trend_blocked_pct, row.symbol))
        withheld: list[Literal["POOL_SIGNAL_EDGE_BLOCKED", "POOL_SIGNAL_EDGE_UNASSESSABLE", "UNPOWERED", "POWER_UNMEASURABLE"]] = []
        if pool.status == "BLOCKED":
            withheld.append("POOL_SIGNAL_EDGE_BLOCKED")
        elif pool.status == "UNASSESSABLE":
            withheld.append("POOL_SIGNAL_EDGE_UNASSESSABLE")
        if power_result.verdict == "UNPOWERED":
            withheld.append("UNPOWERED")
        elif power_result.verdict == "UNMEASURABLE":
            withheld.append("POWER_UNMEASURABLE")
        edge_pick = None
        gates_pick = None
        if passers:
            best = passers[0]
            pick = PrimaryCandidacyEdgePick(symbol=best.symbol, trend_blocked_pct=best.trend_blocked_pct,
                closed_trades=best.closed_trades, reach_rate_pct=best.reach_rate_pct)
            edge_pick = pick if not withheld else None
            gates_pick = PrimaryCandidacyGatesOnlyPick.model_validate({**pick.model_dump(),
                "passing_count": len(passers), "withheld_from_edge_pick_because": withheld,
                "trades_held": best.trades_held, "required_trades_one_sample": required, "power_share_pct": best.power_share_pct})
        trade_inputs: list[TradeabilityInput] = []
        for selection in selections:
            metrics = UniverseSelectionMetrics.model_validate_json(selection.metrics_json)
            trade_inputs.append(TradeabilityInput(symbol=selection.symbol, market=selection.market,
                price=metrics.price, avg_dollar_volume=metrics.avg_dollar_volume,
                relative_spread_bps=metrics.relative_spread_bps, atr_pct_14d=metrics.atr_pct_14d,
                opportunity_to_cost_ratio=metrics.opportunity_to_cost_ratio))
        ranked = rank_tradeability(trade_inputs, min_price=settings.universe_selection_min_price,
            max_spread_bps=settings.universe_selection_max_spread_bps, min_avg_dollar_volume=settings.universe_selection_min_avg_dollar_volume)
        tradeability = [PrimaryCandidacyTradeabilityRow.model_validate({**asdict(row), "metrics_as_of": run.as_of_date}) for row in ranked] if run else []
        cheapest = next((row for row in tradeability if row.rank == 1), None)
        trade_pick = PrimaryCandidacyTradeabilityPick.model_validate(cheapest, from_attributes=True) if cheapest else None
        windows = None
        if include_entry_windows and eligible:
            assessor = self._entry_window_assessor or EntryWindowOverlapService(self._db).assess_pool
            result = assessor(symbols=sorted(eligible), lookback_days=p.reach_lookback_days, now=anchor)
            windows = EntryWindowPoolResponse.model_validate({"generated_at": anchor,
                **{name: getattr(result, name) for name in ("lookback_days", "symbols_total", "symbols_with_any_window", "symbols", "days")}}, from_attributes=True)
        return PrimaryCandidacyReport(generated_at=anchor, incumbent=incumbent, incumbent_status=incumbent_status,
            switch_enabled=settings.auto_primary_switch_enabled, gate_parameters=p, pool_gate=pool,
            power=power_report, candidates=candidates, tradeability=tradeability,
            verdict="NO_SELECTION_RUN" if run is None else ("SELECTION_SUPPORTED" if edge_pick else "SELECTION_NOT_SUPPORTED_BY_EVIDENCE"),
            edge_pick=edge_pick, gates_only_pick=gates_pick, tradeability_pick=trade_pick, entry_windows=windows,
            edge_pick_withheld_reason=None if edge_pick else ("No COMPLETE selection run is available." if run is None else
                "Selection is not supported: " + (", ".join(withheld) if passers else "no candidate passes all symbol gates") + "."))
