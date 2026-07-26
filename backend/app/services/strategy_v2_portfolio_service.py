from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import cast

from sqlalchemy.orm import Session

from app.domain.strategy_v2 import (
    CAUSAL_ENTRY_FILL_OFFSET_BARS,
    PortfolioRoutingCandidate,
    PortfolioRoutingPolicy,
    RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS,
    RISK_GROUP_RELATIVE_MIN_PEERS,
    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
    portfolio_candidate_rejection_reasons,
    rank_portfolio_candidates,
)
from app.domain.universe_selection import (
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    ROTATION_ALGORITHM_VERSION,
    ROTATION_WALK_FORWARD_VERSION,
    parse_frozen_rotation_selection,
    parse_validated_inverse_volatility_targets,
    risk_group_for_sector,
)
from app.models import (
    StrategyV2PortfolioObservation,
    StrategyV2PortfolioRegistration,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistScore,
)
from app.schemas import (
    StrategyV2PortfolioRoutingMetrics,
    StrategyV2PortfolioRoutingReport,
    StrategyV2PortfolioRoutingVariant,
)
from app.services.watchlist_quant_service import CURRENT_QUANT_SOURCES


@dataclass(frozen=True)
class _RoutingSpec:
    policy: PortfolioRoutingPolicy
    algorithm_version: str


_ROUTING_SPECS = (
    _RoutingSpec(
        policy="FIXED_PRIMARY",
        algorithm_version="strategy-v2-portfolio-fixed-primary-v2",
    ),
    _RoutingSpec(
        policy="SELECTED_UNIVERSE",
        algorithm_version="strategy-v2-portfolio-selected-universe-v2",
    ),
    _RoutingSpec(
        policy="QUANT_CANDIDATE",
        algorithm_version="strategy-v2-portfolio-quant-candidate-v2",
    ),
    _RoutingSpec(
        policy="QUANT_WATCH_PLUS",
        algorithm_version="strategy-v2-portfolio-quant-watch-plus-v2",
    ),
    _RoutingSpec(
        policy="SELECTED_VWAP_EDGE",
        algorithm_version=(
            "strategy-v2-portfolio-selected-vwap-edge-v2"
        ),
    ),
    _RoutingSpec(
        policy="VWAP_EDGE_POOL",
        algorithm_version="strategy-v2-portfolio-vwap-edge-pool-v2",
    ),
    _RoutingSpec(
        policy="VWAP_EDGE_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-vwap-edge-75bps-pool-v2"
        ),
    ),
    _RoutingSpec(
        policy="VWAP_EDGE_OBSERVED_COST_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-vwap-observed-cost-pool-v2"
        ),
    ),
    _RoutingSpec(
        policy="VWAP_EDGE_OBS_COST_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-vwap-observed-cost-75bps-pool-v2"
        ),
    ),
    _RoutingSpec(
        policy="RISK_GROUP_REL_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-risk-group-relative-observed-"
            "cost-75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="RISK_GROUP_LOO_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-risk-group-leave-one-out-"
            "observed-cost-75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="SECTOR_LOO_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-sector-leave-one-out-"
            "observed-cost-75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-selected-sector-leave-one-out-"
            "observed-cost-75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="SELECTED_ZSCORE_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-selected-zscore-observed-cost-"
            "75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="ROTATION_ZSCORE_OBS_75BPS_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-rotation-zscore-observed-cost-"
            "75bps-v1"
        ),
    ),
    _RoutingSpec(
        policy="ROTATION_IV_WEIGHTED_ZSCORE_POOL",
        algorithm_version=(
            "strategy-v2-portfolio-rotation-inverse-vol-weighted-"
            "zscore-observed-cost-75bps-v1"
        ),
    ),
)
_EVALUATOR_VERSION = "strategy-v2-single-capital-slot-forward-router-v2"
_CURRENT_ROUTING_ALGORITHM_VERSIONS = tuple(
    spec.algorithm_version for spec in _ROUTING_SPECS
)
_TERMINAL_UNIVERSE_STATUSES = ("COMPLETE", "DEGRADED")
_FIXED_COST_VWAP_EDGE_POLICIES = {
    "SELECTED_VWAP_EDGE",
    "VWAP_EDGE_POOL",
}
_FIXED_75BPS_VWAP_EDGE_POLICIES = {
    "VWAP_EDGE_75BPS_POOL",
}
_OBSERVED_COST_TO_STOP_VWAP_EDGE_POLICIES = {
    "VWAP_EDGE_OBSERVED_COST_POOL",
}
_OBSERVED_COST_TO_75BPS_VWAP_EDGE_POLICIES = {
    "VWAP_EDGE_OBS_COST_75BPS_POOL",
}
_SELECTED_ZSCORE_OBSERVED_COST_POLICIES = {
    "SELECTED_ZSCORE_OBS_75BPS_POOL",
}
_ROTATION_ZSCORE_OBSERVED_COST_POLICIES = {
    "ROTATION_ZSCORE_OBS_75BPS_POOL",
    "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
}
_ROTATION_WEIGHTED_ZSCORE_POLICIES = {
    "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
}
_ZSCORE_OBSERVED_COST_POLICIES = (
    _SELECTED_ZSCORE_OBSERVED_COST_POLICIES
    | _ROTATION_ZSCORE_OBSERVED_COST_POLICIES
)
_RISK_GROUP_INCLUDED_OBSERVED_COST_POLICIES = {
    "RISK_GROUP_REL_OBS_75BPS_POOL",
}
_RISK_GROUP_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES = {
    "RISK_GROUP_LOO_OBS_75BPS_POOL",
}
_SECTOR_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES = {
    "SECTOR_LOO_OBS_75BPS_POOL",
    "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
}
_RELATIVE_OBSERVED_COST_POLICIES = (
    _RISK_GROUP_INCLUDED_OBSERVED_COST_POLICIES
    | _RISK_GROUP_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
    | _SECTOR_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
)
_OBSERVED_COST_VWAP_EDGE_POLICIES = (
    _OBSERVED_COST_TO_STOP_VWAP_EDGE_POLICIES
    | _OBSERVED_COST_TO_75BPS_VWAP_EDGE_POLICIES
    | _RELATIVE_OBSERVED_COST_POLICIES
    | _ZSCORE_OBSERVED_COST_POLICIES
)
_OBSERVED_COST_MAX_AGE = timedelta(minutes=60)
_ENTRY_BIND_TIMEOUT = timedelta(minutes=10)
_MIN_READY_TRADES = 20
_MIN_MATURE_TRADES = 50
_MIN_READY_SESSIONS = 10
_MIN_ROUTED_SYMBOLS = 3
_EPSILON = 1e-9


class StrategyV2PortfolioService:
    """Compare causal cross-symbol routing policies with one capital slot."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_registrations(
        self,
        *,
        primary_symbol: str,
        now: datetime,
    ) -> bool:
        normalized_primary = primary_symbol.strip().upper()
        if not normalized_primary:
            raise ValueError("portfolio routing primary symbol is required")
        current = _as_utc(now)
        eligible_after = current.replace(
            second=0,
            microsecond=0,
        ) + timedelta(minutes=1)
        existing = {
            row.algorithm_version: row
            for row in self.db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.baseline_symbol
                == normalized_primary
            ).all()
        }
        created = False
        for spec in _ROUTING_SPECS:
            digest = self._evaluator_digest(spec)
            row = existing.get(spec.algorithm_version)
            if row is not None:
                if (
                    row.policy != spec.policy
                    or row.evaluator_digest != digest
                ):
                    raise ValueError(
                        "persisted portfolio routing registration differs "
                        "from the frozen evaluator"
                    )
                continue
            self.db.add(StrategyV2PortfolioRegistration(
                baseline_symbol=normalized_primary,
                policy=spec.policy,
                algorithm_version=spec.algorithm_version,
                evaluator_digest=digest,
                registered_at=current,
                eligible_after=eligible_after,
            ))
            created = True
        if created:
            self.db.commit()
        return created

    def advance(self, *, now: datetime) -> None:
        current = _as_utc(now)
        registrations = self.db.query(
            StrategyV2PortfolioRegistration
        ).filter(
            StrategyV2PortfolioRegistration.algorithm_version.in_(
                _CURRENT_ROUTING_ALGORITHM_VERSIONS
            )
        ).order_by(
            StrategyV2PortfolioRegistration.registered_at.asc(),
            StrategyV2PortfolioRegistration.id.asc(),
        ).all()
        for registration in registrations:
            self._sync_selected_observations(
                registration,
                current=current,
            )
            self._advance_registration(
                registration,
                current=current,
            )
        self.db.commit()

    def get_report(
        self,
        primary_symbol: str | None = None,
    ) -> StrategyV2PortfolioRoutingReport:
        normalized = (primary_symbol or "").strip().upper()
        query = self.db.query(StrategyV2PortfolioRegistration)
        query = query.filter(
            StrategyV2PortfolioRegistration.algorithm_version.in_(
                _CURRENT_ROUTING_ALGORITHM_VERSIONS
            )
        )
        if normalized:
            query = query.filter(
                StrategyV2PortfolioRegistration.baseline_symbol
                == normalized
            )
        registrations = query.order_by(
            StrategyV2PortfolioRegistration.registered_at.asc(),
            StrategyV2PortfolioRegistration.id.asc(),
        ).all()
        if not registrations:
            return StrategyV2PortfolioRoutingReport(
                primary_symbol=normalized,
            )
        if not normalized:
            normalized = registrations[-1].baseline_symbol
            registrations = [
                row
                for row in registrations
                if row.baseline_symbol == normalized
            ]

        metrics = {
            row.id: self._metrics(row)
            for row in registrations
        }
        baseline = next(
            (
                metrics[row.id]
                for row in registrations
                if row.policy == "FIXED_PRIMARY"
            ),
            StrategyV2PortfolioRoutingMetrics(),
        )
        variants = [
            self._variant_report(
                row,
                metrics[row.id],
                baseline=baseline,
            )
            for row in registrations
        ]
        return StrategyV2PortfolioRoutingReport(
            primary_symbol=normalized,
            variants=variants,
        )

    def _advance_registration(
        self,
        registration: StrategyV2PortfolioRegistration,
        *,
        current: datetime,
    ) -> None:
        latest_observation = self.db.query(
            StrategyV2PortfolioObservation
        ).filter(
            StrategyV2PortfolioObservation.registration_id
            == registration.id
        ).order_by(
            StrategyV2PortfolioObservation.signal_at.desc(),
            StrategyV2PortfolioObservation.id.desc(),
        ).first()
        lower_bound = (
            latest_observation.signal_at
            if latest_observation is not None
            else registration.eligible_after
        )
        query = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.action == "SUBMIT_ENTRY",
            StrategyV2ShadowDecision.observed_at <= current,
        )
        if latest_observation is None:
            query = query.filter(
                StrategyV2ShadowDecision.bar_at >= lower_bound
            )
        else:
            query = query.filter(
                StrategyV2ShadowDecision.bar_at > lower_bound
            )
        decisions = query.order_by(
            StrategyV2ShadowDecision.bar_at.asc(),
            StrategyV2ShadowDecision.symbol.asc(),
            StrategyV2ShadowDecision.id.asc(),
        ).all()
        grouped: dict[datetime, list[StrategyV2ShadowDecision]] = {}
        for decision in decisions:
            signal_at = _as_utc(decision.bar_at)
            grouped.setdefault(signal_at, []).append(decision)

        for signal_at in sorted(grouped):
            signal_decisions = grouped[signal_at]
            latest_signal_observed_at = max(
                _as_utc(row.observed_at)
                for row in signal_decisions
            )
            first_executable_open = signal_at + timedelta(
                minutes=CAUSAL_ENTRY_FILL_OFFSET_BARS
            )
            causal_signal_decisions = [
                row
                for row in signal_decisions
                if _as_utc(row.observed_at) < first_executable_open
            ]
            context_cutoff = (
                max(
                    _as_utc(row.observed_at)
                    for row in causal_signal_decisions
                )
                if causal_signal_decisions
                else first_executable_open
            )
            routing_observed_at = (
                context_cutoff
                if causal_signal_decisions
                else latest_signal_observed_at
            )
            candidates = self._candidate_context(
                causal_signal_decisions,
                observed_at=context_cutoff,
                include_relative_adjustment=(
                    registration.policy
                    in _RELATIVE_OBSERVED_COST_POLICIES
                ),
                risk_group_leave_one_out=(
                    registration.policy
                    in _RISK_GROUP_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
                ),
                sector_leave_one_out=(
                    registration.policy
                    in _SECTOR_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
                ),
                include_rotation_weight=(
                    registration.policy
                    in _ROTATION_WEIGHTED_ZSCORE_POLICIES
                ),
            )
            ranked = rank_portfolio_candidates(
                candidates,
                policy=_routing_policy(registration.policy),
                primary_symbol=registration.baseline_symbol,
            )
            candidates_json = json.dumps(
                [
                    _candidate_payload(
                        item,
                        include_observed_cost=(
                            registration.policy
                            in _OBSERVED_COST_VWAP_EDGE_POLICIES
                        ),
                        include_relative_adjustment=(
                            registration.policy
                            in _RELATIVE_OBSERVED_COST_POLICIES
                        ),
                        include_zscore=(
                            registration.policy
                            in _ZSCORE_OBSERVED_COST_POLICIES
                        ),
                        include_rotation=(
                            registration.policy
                            in _ROTATION_ZSCORE_OBSERVED_COST_POLICIES
                        ),
                        include_rotation_weight=(
                            registration.policy
                            in _ROTATION_WEIGHTED_ZSCORE_POLICIES
                        ),
                    )
                    for item in ranked
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            occupied = self._occupant_at(
                registration,
                signal_at=signal_at,
            )
            if occupied is not None:
                self.db.add(StrategyV2PortfolioObservation(
                    registration_id=registration.id,
                    signal_at=signal_at,
                    observed_at=routing_observed_at,
                    status="SKIPPED_OCCUPIED",
                    reason=(
                        "SINGLE_CAPITAL_SLOT_OCCUPIED:"
                        f"{occupied.selected_symbol}"
                    ),
                    candidate_count=len(ranked),
                    candidates_json=candidates_json,
                ))
                self.db.flush()
                continue
            if not ranked:
                diagnostic_candidates_json = json.dumps(
                    [
                        _candidate_payload(
                            item,
                            include_observed_cost=(
                                registration.policy
                                in _OBSERVED_COST_VWAP_EDGE_POLICIES
                            ),
                            include_relative_adjustment=(
                                registration.policy
                                in _RELATIVE_OBSERVED_COST_POLICIES
                            ),
                            include_zscore=(
                                registration.policy
                                in _ZSCORE_OBSERVED_COST_POLICIES
                            ),
                            include_rotation=(
                                registration.policy
                                in _ROTATION_ZSCORE_OBSERVED_COST_POLICIES
                            ),
                            include_rotation_weight=(
                                registration.policy
                                in _ROTATION_WEIGHTED_ZSCORE_POLICIES
                            ),
                            rejection_reasons=(
                                portfolio_candidate_rejection_reasons(
                                    item,
                                    policy=_routing_policy(
                                        registration.policy
                                    ),
                                    primary_symbol=(
                                        registration.baseline_symbol
                                    ),
                                )
                            ),
                        )
                        for item in sorted(
                            candidates,
                            key=lambda candidate: candidate.symbol,
                        )
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.db.add(StrategyV2PortfolioObservation(
                    registration_id=registration.id,
                    signal_at=signal_at,
                    observed_at=routing_observed_at,
                    status="NO_ELIGIBLE",
                    reason=(
                        f"NO_ELIGIBLE_{registration.policy}"
                        if candidates
                        else "NO_CAUSAL_SIGNALS"
                    ),
                    candidate_count=0,
                    candidates_json=diagnostic_candidates_json,
                ))
                self.db.flush()
                continue

            selected = ranked[0]
            observation = StrategyV2PortfolioObservation(
                registration_id=registration.id,
                signal_at=signal_at,
                observed_at=routing_observed_at,
                status="PENDING_ENTRY",
                reason="ROUTED_CAUSAL_ENTRY",
                candidate_count=len(ranked),
                candidates_json=candidates_json,
                selected_symbol=selected.symbol,
                source_config_version=selected.source_config_version,
                source_signal_decision_id=selected.signal_decision_id,
                selection_rank=selected.selection_rank,
                selection_score=selected.selection_score,
                quant_source=selected.quant_source,
                quant_action=selected.quant_action,
                quant_score=selected.quant_score,
                quant_confidence=selected.quant_confidence,
            )
            self.db.add(observation)
            self.db.flush()
            self._sync_observation(observation, current=current)

    def _candidate_context(
        self,
        decisions: list[StrategyV2ShadowDecision],
        *,
        observed_at: datetime,
        include_relative_adjustment: bool,
        risk_group_leave_one_out: bool,
        sector_leave_one_out: bool,
        include_rotation_weight: bool,
    ) -> list[PortfolioRoutingCandidate]:
        by_symbol: dict[str, StrategyV2ShadowDecision] = {}
        for decision in decisions:
            current = by_symbol.get(decision.symbol)
            if current is None or decision.id > current.id:
                by_symbol[decision.symbol] = decision
        selection_run = self._latest_selection_run(
            observed_at=observed_at,
        )
        selection = self._selection_rows_for_run(
            tuple(by_symbol),
            run=selection_run,
        )
        session_dates = {
            decision.session_date
            for decision in by_symbol.values()
        }
        rotation_targets = (
            self._validated_inverse_volatility_targets(
                selection_run,
                session_date=next(iter(session_dates)),
            )
            if include_rotation_weight and len(session_dates) == 1
            else {}
        )
        quant = self._quant_context(
            tuple(by_symbol),
            observed_at=observed_at,
        )
        vwap_edges = self._vwap_edge_context(
            tuple(by_symbol.values()),
            observed_at=observed_at,
        )
        relative_adjustments = (
            (
                self._sector_leave_one_out_context(
                    tuple(by_symbol.values()),
                    vwap_edges=vwap_edges,
                    observed_at=observed_at,
                )
                if sector_leave_one_out
                else (
                    self._risk_group_leave_one_out_context(
                        tuple(by_symbol.values()),
                        vwap_edges=vwap_edges,
                        observed_at=observed_at,
                    )
                    if risk_group_leave_one_out
                    else self._risk_group_relative_context(
                        tuple(by_symbol.values()),
                        vwap_edges=vwap_edges,
                        observed_at=observed_at,
                    )
                )
            )
            if include_relative_adjustment
            else {}
        )
        candidates: list[PortfolioRoutingCandidate] = []
        for symbol, decision in sorted(by_symbol.items()):
            selection_row = selection.get(symbol)
            quant_row = quant.get(symbol)
            vwap_edge = vwap_edges.get(decision.id)
            relative_adjustment = relative_adjustments.get(
                decision.id
            )
            (
                rotation_selected,
                rotation_rank,
                rotation_score,
            ) = self._rotation_selection_values(selection_row)
            rotation_target = rotation_targets.get(symbol)
            rotation_target_weight_pct = (
                rotation_target[2]
                if (
                    rotation_target is not None
                    and rotation_selected
                    and rotation_rank == rotation_target[0]
                    and rotation_score is not None
                    and math.isclose(
                        rotation_score,
                        rotation_target[1],
                        rel_tol=0.0,
                        abs_tol=_EPSILON,
                    )
                )
                else None
            )
            candidates.append(PortfolioRoutingCandidate(
                symbol=symbol,
                signal_decision_id=decision.id,
                source_config_version=decision.config_version,
                selection_selected=(
                    bool(selection_row.selected)
                    if selection_row is not None
                    else False
                ),
                selection_rank=(
                    selection_row.rank
                    if selection_row is not None
                    else None
                ),
                selection_score=(
                    float(selection_row.score)
                    if selection_row is not None
                    else None
                ),
                rotation_selected=rotation_selected,
                rotation_rank=rotation_rank,
                rotation_score=rotation_score,
                rotation_target_weight_pct=(
                    rotation_target_weight_pct
                ),
                quant_source=(
                    quant_row.source
                    if quant_row is not None
                    else ""
                ),
                quant_action=(
                    quant_row.recommended_action
                    if quant_row is not None
                    else ""
                ),
                quant_score=(
                    float(quant_row.score)
                    if quant_row is not None
                    else None
                ),
                quant_confidence=(
                    float(quant_row.confidence)
                    if quant_row is not None
                    else None
                ),
                residual_1m_bps=(
                    vwap_edge[0]
                    if vwap_edge is not None
                    else None
                ),
                residual_5m_bps=(
                    vwap_edge[1]
                    if vwap_edge is not None
                    else None
                ),
                zscore_1m=decision.zscore_1m,
                zscore_5m=decision.zscore_5m,
                round_trip_cost_bps=(
                    vwap_edge[2]
                    if vwap_edge is not None
                    else None
                ),
                observed_round_trip_cost_bps=(
                    self._fresh_observed_cost_bps(
                        quant_row,
                        observed_at=observed_at,
                    )
                ),
                stop_distance_bps=(
                    vwap_edge[3]
                    if vwap_edge is not None
                    else None
                ),
                risk_group=(
                    relative_adjustment[0]
                    if relative_adjustment is not None
                    else ""
                ),
                risk_group_peer_count=(
                    relative_adjustment[1]
                    if relative_adjustment is not None
                    else 0
                ),
                risk_group_relative_1m_bps=(
                    relative_adjustment[2]
                    if relative_adjustment is not None
                    else None
                ),
                risk_group_relative_5m_bps=(
                    relative_adjustment[3]
                    if relative_adjustment is not None
                    else None
                ),
            ))
        return candidates

    def _risk_group_relative_context(
        self,
        decisions: tuple[StrategyV2ShadowDecision, ...],
        *,
        vwap_edges: dict[int, tuple[float, float, float, float]],
        observed_at: datetime,
    ) -> dict[int, tuple[str, int, float, float]]:
        if not decisions:
            return {}
        signal_times = {_as_utc(row.bar_at) for row in decisions}
        if len(signal_times) != 1:
            raise ValueError(
                "risk-group routing context requires one signal minute"
            )
        signal_at = next(iter(signal_times))
        rows = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.bar_at == signal_at,
            StrategyV2ShadowDecision.observed_at <= observed_at,
        ).order_by(
            StrategyV2ShadowDecision.observed_at.desc(),
            StrategyV2ShadowDecision.id.desc(),
        ).all()
        latest_by_symbol: dict[str, StrategyV2ShadowDecision] = {}
        for row in rows:
            latest_by_symbol.setdefault(row.symbol, row)
        selection = self._selection_context(
            tuple(latest_by_symbol),
            observed_at=observed_at,
        )
        residuals_by_group: dict[
            str,
            list[tuple[float, float]],
        ] = {}
        for symbol, row in latest_by_symbol.items():
            selection_row = selection.get(symbol)
            if selection_row is None:
                continue
            residuals = self._decision_residuals_bps(row)
            if residuals is None:
                continue
            risk_group = risk_group_for_sector(selection_row.sector)
            if not risk_group:
                continue
            residuals_by_group.setdefault(
                risk_group,
                [],
            ).append(residuals)

        medians = {
            risk_group: (
                len(values),
                median(value[0] for value in values),
                median(value[1] for value in values),
            )
            for risk_group, values in residuals_by_group.items()
        }
        result: dict[int, tuple[str, int, float, float]] = {}
        for decision in decisions:
            selection_row = selection.get(decision.symbol)
            edge = vwap_edges.get(decision.id)
            if selection_row is None or edge is None:
                continue
            risk_group = risk_group_for_sector(selection_row.sector)
            group = medians.get(risk_group)
            if group is None:
                continue
            peer_count, median_1m, median_5m = group
            result[decision.id] = (
                risk_group,
                peer_count,
                edge[0] - median_1m,
                edge[1] - median_5m,
            )
        return result

    def _risk_group_leave_one_out_context(
        self,
        decisions: tuple[StrategyV2ShadowDecision, ...],
        *,
        vwap_edges: dict[int, tuple[float, float, float, float]],
        observed_at: datetime,
    ) -> dict[int, tuple[str, int, float, float]]:
        return self._leave_one_out_context(
            decisions,
            vwap_edges=vwap_edges,
            observed_at=observed_at,
            use_refined_sector=False,
        )

    def _sector_leave_one_out_context(
        self,
        decisions: tuple[StrategyV2ShadowDecision, ...],
        *,
        vwap_edges: dict[int, tuple[float, float, float, float]],
        observed_at: datetime,
    ) -> dict[int, tuple[str, int, float, float]]:
        return self._leave_one_out_context(
            decisions,
            vwap_edges=vwap_edges,
            observed_at=observed_at,
            use_refined_sector=True,
        )

    def _leave_one_out_context(
        self,
        decisions: tuple[StrategyV2ShadowDecision, ...],
        *,
        vwap_edges: dict[int, tuple[float, float, float, float]],
        observed_at: datetime,
        use_refined_sector: bool,
    ) -> dict[int, tuple[str, int, float, float]]:
        if not decisions:
            return {}
        signal_times = {_as_utc(row.bar_at) for row in decisions}
        if len(signal_times) != 1:
            raise ValueError(
                "relative routing context requires one signal minute"
            )
        signal_at = next(iter(signal_times))
        rows = self.db.query(StrategyV2ShadowDecision).filter(
            StrategyV2ShadowDecision.bar_at == signal_at,
            StrategyV2ShadowDecision.observed_at <= observed_at,
        ).order_by(
            StrategyV2ShadowDecision.observed_at.desc(),
            StrategyV2ShadowDecision.id.desc(),
        ).all()
        latest_by_symbol: dict[str, StrategyV2ShadowDecision] = {}
        for row in rows:
            latest_by_symbol.setdefault(row.symbol, row)
        selection = self._selection_context(
            tuple(latest_by_symbol),
            observed_at=observed_at,
        )
        residuals_by_group: dict[
            str,
            dict[str, tuple[float, float]],
        ] = {}
        for symbol, row in latest_by_symbol.items():
            selection_row = selection.get(symbol)
            if selection_row is None:
                continue
            residuals = self._decision_residuals_bps(row)
            if residuals is None:
                continue
            reference_group = (
                selection_row.sector.strip()
                if use_refined_sector
                else risk_group_for_sector(selection_row.sector)
            )
            if not reference_group:
                continue
            residuals_by_group.setdefault(
                reference_group,
                {},
            )[symbol] = residuals

        result: dict[int, tuple[str, int, float, float]] = {}
        for decision in decisions:
            selection_row = selection.get(decision.symbol)
            edge = vwap_edges.get(decision.id)
            if selection_row is None or edge is None:
                continue
            reference_group = (
                selection_row.sector.strip()
                if use_refined_sector
                else risk_group_for_sector(selection_row.sector)
            )
            group = residuals_by_group.get(reference_group)
            if group is None:
                continue
            peers = [
                residuals
                for symbol, residuals in group.items()
                if symbol != decision.symbol
            ]
            if not peers:
                continue
            result[decision.id] = (
                reference_group,
                len(peers),
                edge[0] - median(value[0] for value in peers),
                edge[1] - median(value[1] for value in peers),
            )
        return result

    @staticmethod
    def _decision_residuals_bps(
        decision: StrategyV2ShadowDecision,
    ) -> tuple[float, float] | None:
        try:
            features = json.loads(decision.features_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(features, dict):
            return None
        try:
            residual_1m = float(features["residual_1m"]) * 10_000
            residual_5m = float(features["residual_5m"]) * 10_000
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(residual_1m) or not math.isfinite(
            residual_5m
        ):
            return None
        return residual_1m, residual_5m

    @staticmethod
    def _fresh_observed_cost_bps(
        row: WatchlistScore | None,
        *,
        observed_at: datetime,
    ) -> float | None:
        if (
            row is None
            or row.estimated_round_trip_cost_bps is None
            or _as_utc(row.created_at)
            < observed_at - _OBSERVED_COST_MAX_AGE
        ):
            return None
        return float(row.estimated_round_trip_cost_bps)

    def _vwap_edge_context(
        self,
        decisions: tuple[StrategyV2ShadowDecision, ...],
        *,
        observed_at: datetime,
    ) -> dict[int, tuple[float, float, float, float]]:
        if not decisions:
            return {}
        symbols = tuple({row.symbol for row in decisions})
        config_versions = tuple({
            row.config_version for row in decisions
        })
        versions = self.db.query(StrategyV2ShadowVersion).filter(
            StrategyV2ShadowVersion.symbol.in_(symbols),
            StrategyV2ShadowVersion.config_version.in_(config_versions),
            StrategyV2ShadowVersion.activated_at <= observed_at,
        ).all()
        by_key = {
            (row.symbol, row.config_version): row
            for row in versions
        }
        result: dict[int, tuple[float, float, float, float]] = {}
        for decision in decisions:
            version = by_key.get((
                decision.symbol,
                decision.config_version,
            ))
            if version is None:
                continue
            values = self._vwap_edge_values(decision, version)
            if values is not None:
                result[decision.id] = values
        return result

    @staticmethod
    def _vwap_edge_values(
        decision: StrategyV2ShadowDecision,
        version: StrategyV2ShadowVersion,
    ) -> tuple[float, float, float, float] | None:
        try:
            config = json.loads(version.config_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(config, dict):
            return None
        try:
            features = json.loads(decision.features_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(features, dict):
            return None
        fee_key = (
            "estimated_fee_rate_us"
            if decision.market == "US"
            else "estimated_fee_rate_hk"
            if decision.market == "HK"
            else ""
        )
        if not fee_key:
            return None
        try:
            fee_rate = float(config[fee_key])
            slippage_bps = float(config["slippage_bps"])
            stop_loss_pct = float(config["stop_loss_pct"])
            residual_1m = float(features["residual_1m"])
            residual_5m = float(features["residual_5m"])
        except (KeyError, TypeError, ValueError):
            return None
        values = (
            fee_rate,
            slippage_bps,
            stop_loss_pct,
            residual_1m,
            residual_5m,
        )
        if (
            any(not math.isfinite(value) for value in values)
            or fee_rate < 0
            or slippage_bps < 0
            or stop_loss_pct <= 0
        ):
            return None
        residual_1m_bps = residual_1m * 10_000
        residual_5m_bps = residual_5m * 10_000
        round_trip_cost_bps = 2 * (
            fee_rate * 10_000 + slippage_bps
        )
        stop_distance_bps = stop_loss_pct * 100
        result = (
            residual_1m_bps,
            residual_5m_bps,
            round_trip_cost_bps,
            stop_distance_bps,
        )
        if any(not math.isfinite(value) for value in result):
            return None
        return result

    def _selection_context(
        self,
        symbols: tuple[str, ...],
        *,
        observed_at: datetime,
    ) -> dict[str, UniverseSelectionCandidate]:
        run = self._latest_selection_run(observed_at=observed_at)
        return self._selection_rows_for_run(symbols, run=run)

    def _latest_selection_run(
        self,
        *,
        observed_at: datetime,
    ) -> UniverseSelectionRun | None:
        return self.db.query(UniverseSelectionRun).filter(
            UniverseSelectionRun.status.in_(
                _TERMINAL_UNIVERSE_STATUSES
            ),
            UniverseSelectionRun.completed_at.is_not(None),
            UniverseSelectionRun.completed_at <= observed_at,
        ).order_by(
            UniverseSelectionRun.completed_at.desc(),
            UniverseSelectionRun.id.desc(),
        ).first()

    def _selection_rows_for_run(
        self,
        symbols: tuple[str, ...],
        *,
        run: UniverseSelectionRun | None,
    ) -> dict[str, UniverseSelectionCandidate]:
        if not symbols or run is None:
            return {}
        return {
            row.symbol: row
            for row in self.db.query(
                UniverseSelectionCandidate
            ).filter(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.symbol.in_(symbols),
            ).all()
        }

    @staticmethod
    def _validated_inverse_volatility_targets(
        run: UniverseSelectionRun | None,
        *,
        session_date: date,
    ) -> dict[str, tuple[int, float, float]]:
        if run is None or run.status != "COMPLETE":
            return {}
        return parse_validated_inverse_volatility_targets(
            run.parameters_json,
            run_as_of_date=run.as_of_date,
            session_date=session_date,
        )

    @staticmethod
    def _rotation_selection_values(
        row: UniverseSelectionCandidate | None,
    ) -> tuple[bool, int | None, float | None]:
        if row is None:
            return False, None, None
        selection = parse_frozen_rotation_selection(row.metrics_json)
        if selection is None:
            return False, None, None
        rank, score = selection
        return True, rank, score

    def _quant_context(
        self,
        symbols: tuple[str, ...],
        *,
        observed_at: datetime,
    ) -> dict[str, WatchlistScore]:
        if not symbols:
            return {}
        rows = self.db.query(WatchlistScore).filter(
            WatchlistScore.symbol.in_(symbols),
            WatchlistScore.source.in_(CURRENT_QUANT_SOURCES),
            WatchlistScore.created_at <= observed_at,
        ).order_by(
            WatchlistScore.created_at.desc(),
            WatchlistScore.id.desc(),
        ).all()
        latest: dict[str, WatchlistScore] = {}
        seen: set[str] = set()
        for row in rows:
            if row.symbol in seen:
                continue
            seen.add(row.symbol)
            if _as_utc(row.expires_at) <= observed_at:
                continue
            latest[row.symbol] = row
        return latest

    def _sync_selected_observations(
        self,
        registration: StrategyV2PortfolioRegistration,
        *,
        current: datetime,
    ) -> None:
        rows = self.db.query(
            StrategyV2PortfolioObservation
        ).filter(
            StrategyV2PortfolioObservation.registration_id
            == registration.id,
            StrategyV2PortfolioObservation.status.in_(
                ("PENDING_ENTRY", "OPEN")
            ),
        ).order_by(
            StrategyV2PortfolioObservation.signal_at.asc(),
            StrategyV2PortfolioObservation.id.asc(),
        ).all()
        for row in rows:
            self._sync_observation(row, current=current)

    def _sync_observation(
        self,
        row: StrategyV2PortfolioObservation,
        *,
        current: datetime,
    ) -> None:
        source: StrategyV2ShadowTrade | None = None
        if row.source_trade_id is not None:
            source = self.db.get(
                StrategyV2ShadowTrade,
                row.source_trade_id,
            )
            if source is None:
                raise ValueError(
                    "portfolio routing source trade linkage is incomplete"
                )
        elif row.status == "PENDING_ENTRY":
            expected_entry = _as_utc(row.signal_at) + timedelta(
                minutes=CAUSAL_ENTRY_FILL_OFFSET_BARS
            )
            source = self.db.query(StrategyV2ShadowTrade).join(
                StrategyV2ShadowDecision,
                StrategyV2ShadowDecision.id
                == StrategyV2ShadowTrade.entry_decision_id,
            ).filter(
                StrategyV2ShadowTrade.symbol == row.selected_symbol,
                StrategyV2ShadowTrade.config_version
                == row.source_config_version,
                StrategyV2ShadowTrade.entry_at == expected_entry,
                StrategyV2ShadowDecision.observed_at <= current,
            ).order_by(
                StrategyV2ShadowTrade.id.asc()
            ).first()
            if source is None:
                if current >= expected_entry + _ENTRY_BIND_TIMEOUT:
                    row.status = "MISSED"
                    row.reason = "SOURCE_ENTRY_MISSING"
                    self.db.add(row)
                return
            self._validate_source_trade(row, source)
            row.source_trade_id = source.id
            row.entry_at = source.entry_at
            row.entry_price = source.entry_price
            row.status = "OPEN"
            row.reason = "SOURCE_ENTRY_BOUND"
        elif row.status == "OPEN":
            raise ValueError(
                "open portfolio routing observation has no source trade"
            )

        if source is not None and source.status == "CLOSED":
            self._close_from_source(row, source)
        self.db.add(row)
        self.db.flush()

    def _validate_source_trade(
        self,
        row: StrategyV2PortfolioObservation,
        source: StrategyV2ShadowTrade,
    ) -> None:
        if (
            row.source_signal_decision_id is None
            or source.entry_decision_id is None
        ):
            raise ValueError(
                "portfolio routing source linkage has a missing decision"
            )
        signal = self.db.get(
            StrategyV2ShadowDecision,
            row.source_signal_decision_id,
        )
        entry = self.db.get(
            StrategyV2ShadowDecision,
            source.entry_decision_id,
        )
        expected_entry = _as_utc(row.signal_at) + timedelta(
            minutes=CAUSAL_ENTRY_FILL_OFFSET_BARS
        )
        if (
            signal is None
            or signal.action != "SUBMIT_ENTRY"
            or signal.symbol != row.selected_symbol
            or signal.config_version != row.source_config_version
            or _as_utc(signal.bar_at) != _as_utc(row.signal_at)
            or _as_utc(signal.observed_at) >= expected_entry
            or entry is None
            or entry.action != "FILL_ENTRY"
            or entry.symbol != row.selected_symbol
            or entry.config_version != row.source_config_version
            or _as_utc(entry.bar_at) != expected_entry
            or _as_utc(source.entry_at) != expected_entry
        ):
            raise ValueError(
                "portfolio routing source trade is not the causal future-bar fill"
            )

    @staticmethod
    def _close_from_source(
        row: StrategyV2PortfolioObservation,
        source: StrategyV2ShadowTrade,
    ) -> None:
        values = (
            source.entry_price,
            source.exit_price,
            source.quantity,
            source.gross_pnl,
            source.net_pnl,
        )
        if (
            source.exit_at is None
            or any(
                value is None or not math.isfinite(float(value))
                for value in values
            )
            or source.entry_price <= 0
            or source.quantity <= 0
        ):
            raise ValueError(
                "closed portfolio routing source trade has incomplete outcome"
            )
        entry_notional = source.entry_price * source.quantity
        row.status = "CLOSED"
        row.reason = "SOURCE_TRADE_CLOSED"
        row.exit_at = source.exit_at
        row.exit_price = source.exit_price
        row.exit_reason = source.exit_reason
        row.gross_return_pct = (
            float(source.gross_pnl or 0.0) / entry_notional * 100
        )
        row.net_return_pct = (
            float(source.net_pnl or 0.0) / entry_notional * 100
        )

    def _occupant_at(
        self,
        registration: StrategyV2PortfolioRegistration,
        *,
        signal_at: datetime,
    ) -> StrategyV2PortfolioObservation | None:
        rows = self.db.query(
            StrategyV2PortfolioObservation
        ).filter(
            StrategyV2PortfolioObservation.registration_id
            == registration.id,
            StrategyV2PortfolioObservation.selected_symbol != "",
            StrategyV2PortfolioObservation.signal_at < signal_at,
        ).order_by(
            StrategyV2PortfolioObservation.signal_at.desc(),
            StrategyV2PortfolioObservation.id.desc(),
        ).all()
        for row in rows:
            if row.status in {"PENDING_ENTRY", "OPEN"}:
                return row
            if (
                row.status == "CLOSED"
                and row.exit_at is not None
                and _as_utc(row.exit_at) > signal_at
            ):
                return row
        return None

    def _metrics(
        self,
        registration: StrategyV2PortfolioRegistration,
    ) -> StrategyV2PortfolioRoutingMetrics:
        rows = self.db.query(
            StrategyV2PortfolioObservation
        ).filter(
            StrategyV2PortfolioObservation.registration_id
            == registration.id
        ).order_by(
            StrategyV2PortfolioObservation.signal_at.asc(),
            StrategyV2PortfolioObservation.id.asc(),
        ).all()
        closed = [
            row
            for row in rows
            if row.status == "CLOSED"
            and row.net_return_pct is not None
        ]
        returns = [
            float(row.net_return_pct or 0.0)
            for row in closed
        ]
        compounded, max_drawdown = _compounded_metrics(returns)
        selected = [
            row
            for row in rows
            if row.selected_symbol
        ]
        rejection_counts: Counter[str] = Counter()
        diagnosed_no_eligible = 0
        no_causal_signal_groups = 0
        for row in rows:
            if row.status != "NO_ELIGIBLE":
                continue
            if row.reason == "NO_CAUSAL_SIGNALS":
                no_causal_signal_groups += 1
            try:
                payloads = json.loads(row.candidates_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(payloads, list) or not payloads:
                continue
            diagnosed_no_eligible += 1
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                reasons = payload.get("rejection_reasons")
                if not isinstance(reasons, list):
                    continue
                rejection_counts.update(
                    reason
                    for reason in reasons
                    if isinstance(reason, str) and reason
                )
        return StrategyV2PortfolioRoutingMetrics(
            signal_groups=len(rows),
            selected_signals=len(selected),
            skipped_occupied=sum(
                row.status == "SKIPPED_OCCUPIED"
                for row in rows
            ),
            no_eligible=sum(
                row.status == "NO_ELIGIBLE"
                for row in rows
            ),
            diagnosed_no_eligible=diagnosed_no_eligible,
            no_causal_signal_groups=no_causal_signal_groups,
            rejection_counts=dict(sorted(rejection_counts.items())),
            pending_entries=sum(
                row.status == "PENDING_ENTRY"
                for row in rows
            ),
            open_trades=sum(row.status == "OPEN" for row in rows),
            missed_entries=sum(row.status == "MISSED" for row in rows),
            closed_trades=len(closed),
            observed_sessions=len({
                _as_utc(row.signal_at).date()
                for row in rows
            }),
            distinct_symbols=len({
                row.selected_symbol
                for row in closed
                if row.selected_symbol
            }),
            win_rate=(
                sum(value > 0 for value in returns) / len(returns)
                if returns
                else 0.0
            ),
            mean_net_return_pct=(
                sum(returns) / len(returns)
                if returns
                else 0.0
            ),
            cumulative_net_return_pct=sum(returns),
            compounded_return_pct=compounded,
            max_drawdown_pct=max_drawdown,
            selections_by_symbol=dict(sorted(
                Counter(
                    row.selected_symbol
                    for row in selected
                ).items()
            )),
            latest_signal_at=(
                rows[-1].signal_at
                if rows
                else None
            ),
        )

    def _variant_report(
        self,
        registration: StrategyV2PortfolioRegistration,
        metrics: StrategyV2PortfolioRoutingMetrics,
        *,
        baseline: StrategyV2PortfolioRoutingMetrics,
    ) -> StrategyV2PortfolioRoutingVariant:
        blockers: list[str] = []
        is_baseline = registration.policy == "FIXED_PRIMARY"
        if metrics.closed_trades < _MIN_READY_TRADES:
            blockers.append("MIN_CLOSED_TRADES")
        if metrics.observed_sessions < _MIN_READY_SESSIONS:
            blockers.append("MIN_OBSERVED_SESSIONS")
        if (
            not is_baseline
            and metrics.distinct_symbols < _MIN_ROUTED_SYMBOLS
        ):
            blockers.append("MIN_DISTINCT_SYMBOLS")
        if (
            metrics.closed_trades
            and metrics.compounded_return_pct <= 0
        ):
            blockers.append("COMPOUNDED_RETURN_NON_POSITIVE")
        if is_baseline:
            blockers.append("BASELINE_COMPARATOR")
        else:
            if baseline.observed_sessions < _MIN_READY_SESSIONS:
                blockers.append("BASELINE_EVIDENCE_INSUFFICIENT")
            elif (
                metrics.compounded_return_pct
                <= baseline.compounded_return_pct + _EPSILON
            ):
                blockers.append("NOT_BETTER_THAN_FIXED_PRIMARY")
            if (
                baseline.closed_trades
                and metrics.max_drawdown_pct
                > baseline.max_drawdown_pct + _EPSILON
            ):
                blockers.append("MAX_DRAWDOWN_WORSE_THAN_FIXED_PRIMARY")
        return StrategyV2PortfolioRoutingVariant(
            registration_id=registration.id,
            policy=_routing_policy(registration.policy),
            algorithm_version=registration.algorithm_version,
            evaluator_digest=registration.evaluator_digest,
            registered_at=registration.registered_at,
            eligible_after=registration.eligible_after,
            edge_filter=(
                "ZSCORE_OBS_COST_TO_75BPS"
                if registration.policy in _ZSCORE_OBSERVED_COST_POLICIES
                else (
                    "SECTOR_LOO_OBS_COST_TO_75BPS"
                    if registration.policy
                    in _SECTOR_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
                    else (
                        "RISK_GROUP_LOO_OBS_COST_TO_75BPS"
                        if registration.policy
                        in _RISK_GROUP_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
                        else (
                            "RISK_GROUP_REL_OBS_COST_TO_75BPS"
                            if registration.policy
                            in _RISK_GROUP_INCLUDED_OBSERVED_COST_POLICIES
                            else (
                                "OBSERVED_COST_TO_STOP_VWAP_DISCOUNT"
                                if registration.policy
                                in _OBSERVED_COST_TO_STOP_VWAP_EDGE_POLICIES
                                else (
                                    "OBSERVED_COST_TO_75BPS_VWAP_DISCOUNT"
                                    if registration.policy
                                    in _OBSERVED_COST_TO_75BPS_VWAP_EDGE_POLICIES
                                    else (
                                        "COST_TO_75BPS_VWAP_DISCOUNT"
                                        if registration.policy
                                        in _FIXED_75BPS_VWAP_EDGE_POLICIES
                                        else (
                                            "COST_TO_STOP_VWAP_DISCOUNT"
                                            if registration.policy
                                            in _FIXED_COST_VWAP_EDGE_POLICIES
                                            else "NONE"
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            ),
            status=(
                "MATURE_EVIDENCE"
                if metrics.closed_trades >= _MIN_MATURE_TRADES
                else "READY_FOR_REVIEW"
                if metrics.closed_trades >= _MIN_READY_TRADES
                else "COLLECTING"
            ),
            metrics=metrics,
            fixed_primary_compounded_return_pct=(
                baseline.compounded_return_pct
            ),
            compounded_return_delta_pct=(
                metrics.compounded_return_pct
                - baseline.compounded_return_pct
            ),
            promotion_ready=not blockers,
            blockers=blockers,
        )

    @staticmethod
    def _evaluator_digest(spec: _RoutingSpec) -> str:
        payload = {
            "evaluator_version": _EVALUATOR_VERSION,
            "algorithm_version": spec.algorithm_version,
            "policy": spec.policy,
            "position_side": "LONG",
            "signal_action": "SUBMIT_ENTRY",
            "fill_rule": "SOURCE_FIRST_CAUSAL_FUTURE_BAR_OPEN",
            "fill_offset_bars": CAUSAL_ENTRY_FILL_OFFSET_BARS,
            "signal_observation_deadline": (
                "STRICTLY_BEFORE_SOURCE_FILL_OPEN"
            ),
            "source_fill_visibility": (
                "ENTRY_DECISION_OBSERVED_NO_LATER_THAN_EVALUATION"
            ),
            "capital_slots": 1,
            "historical_backfill": False,
            "quant_sources": list(CURRENT_QUANT_SOURCES),
            "context_cutoff": (
                "LATEST_CAUSAL_SIGNAL_OBSERVATION_BEFORE_FILL_OPEN"
            ),
            "quant_freshness": (
                "CREATED_BEFORE_AND_UNEXPIRED_AT_CONTEXT_CUTOFF"
            ),
            "universe_freshness": "COMPLETED_BEFORE_CONTEXT_CUTOFF",
        }
        if (
            spec.policy
            in _SECTOR_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
        ):
            if spec.policy == "SELECTED_SECTOR_LOO_OBS_75BPS_POOL":
                payload["candidate_universe"] = (
                    "SELECTED_TRUE_IN_LATEST_COMPLETED_UNIVERSE_RUN_"
                    "BEFORE_CONTEXT_CUTOFF"
                )
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "relative_reference": (
                    "CAUSALLY_OBSERVED_SAME_MINUTE_OTHER_SYMBOL_"
                    "REFINED_SECTOR_MEDIAN_RESIDUALS"
                ),
                "sector_source": (
                    "LATEST_COMPLETED_UNIVERSE_RUN_BEFORE_CONTEXT_"
                    "CUTOFF_EXACT_SECTOR"
                ),
                "relative_formula": (
                    "CANDIDATE_RESIDUAL_MINUS_OTHER_REFINED_SECTOR_"
                    "MEMBERS_MEDIAN"
                ),
                "candidate_excluded_from_reference": True,
                "minimum_sector_peers": (
                    RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS
                ),
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": (
                    "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS"
                ),
                "maximum_discount_bps": (
                    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
                ),
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                    "OTHER_REFINED_SECTOR_MEMBERS_MEDIAN_1M",
                    "OTHER_REFINED_SECTOR_MEMBERS_MEDIAN_5M",
                ],
                "bounds": (
                    "INCLUSIVE_FOR_OWN_AND_RELATIVE_RESIDUALS"
                ),
            }
        elif (
            spec.policy
            in _RISK_GROUP_LEAVE_ONE_OUT_OBSERVED_COST_POLICIES
        ):
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "relative_reference": (
                    "CAUSALLY_OBSERVED_SAME_MINUTE_OTHER_SYMBOL_"
                    "RISK_GROUP_MEDIAN_RESIDUALS"
                ),
                "risk_group_source": (
                    "LATEST_COMPLETED_UNIVERSE_RUN_BEFORE_CONTEXT_"
                    "CUTOFF_WITH_TECH_INDUSTRIES_COLLAPSED"
                ),
                "relative_formula": (
                    "CANDIDATE_RESIDUAL_MINUS_OTHER_RISK_GROUP_"
                    "MEMBERS_MEDIAN"
                ),
                "candidate_excluded_from_reference": True,
                "minimum_risk_group_peers": (
                    RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS
                ),
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": (
                    "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS"
                ),
                "maximum_discount_bps": (
                    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
                ),
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                    "OTHER_RISK_GROUP_MEMBERS_MEDIAN_1M",
                    "OTHER_RISK_GROUP_MEMBERS_MEDIAN_5M",
                ],
                "bounds": (
                    "INCLUSIVE_FOR_OWN_AND_RELATIVE_RESIDUALS"
                ),
            }
        elif spec.policy in _RISK_GROUP_INCLUDED_OBSERVED_COST_POLICIES:
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "relative_reference": (
                    "CAUSALLY_OBSERVED_SAME_MINUTE_RISK_GROUP_"
                    "MEDIAN_RESIDUALS"
                ),
                "risk_group_source": (
                    "LATEST_COMPLETED_UNIVERSE_RUN_BEFORE_CONTEXT_"
                    "CUTOFF_WITH_TECH_INDUSTRIES_COLLAPSED"
                ),
                "relative_formula": (
                    "CANDIDATE_RESIDUAL_MINUS_RISK_GROUP_MEDIAN"
                ),
                "minimum_risk_group_peers": (
                    RISK_GROUP_RELATIVE_MIN_PEERS
                ),
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS",
                "maximum_discount_bps": (
                    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
                ),
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                    "RISK_GROUP_MEDIAN_1M",
                    "RISK_GROUP_MEDIAN_5M",
                ],
                "bounds": "INCLUSIVE_FOR_OWN_AND_RELATIVE_RESIDUALS",
            }
        elif spec.policy in _FIXED_COST_VWAP_EDGE_POLICIES:
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "minimum_discount": "FROZEN_ROUND_TRIP_COST_BPS",
                "maximum_discount": "FROZEN_STOP_DISTANCE_BPS",
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                ],
                "bounds": "INCLUSIVE",
            }
        elif spec.policy in _FIXED_75BPS_VWAP_EDGE_POLICIES:
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "minimum_discount": "FROZEN_ROUND_TRIP_COST_BPS",
                "maximum_discount": "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS",
                "maximum_discount_bps": (
                    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
                ),
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                ],
                "bounds": "INCLUSIVE",
            }
        elif spec.policy in _OBSERVED_COST_TO_STOP_VWAP_EDGE_POLICIES:
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": "FROZEN_STOP_DISTANCE_BPS",
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                ],
                "bounds": "INCLUSIVE",
            }
        elif spec.policy in _ZSCORE_OBSERVED_COST_POLICIES:
            if spec.policy in _ROTATION_ZSCORE_OBSERVED_COST_POLICIES:
                payload["candidate_universe"] = (
                    "ROTATION_SELECTED_TRUE_IN_LATEST_COMPLETED_"
                    "UNIVERSE_RUN_BEFORE_CONTEXT_CUTOFF"
                )
                payload["rotation_algorithm_version"] = (
                    ROTATION_ALGORITHM_VERSION
                )
                if spec.policy in _ROTATION_WEIGHTED_ZSCORE_POLICIES:
                    payload["rotation_weighting"] = {
                        "evaluation_version": (
                            ROTATION_WALK_FORWARD_VERSION
                        ),
                        "variant_name": (
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT.name
                        ),
                        "required_validation": [
                            "FIXED_HOLDOUT_PASSED",
                            "EXPANDING_WALK_FORWARD_PASSED",
                        ],
                        "registration_source": (
                            "EXACT_SIGNAL_SESSION_MONTH_FROM_LATEST_"
                            "COMPLETED_UNIVERSE_RUN_BEFORE_CONTEXT_CUTOFF"
                        ),
                        "registration_keys": [
                            "rotation_weighting_challenger_registration",
                            "rotation_next_weighting_challenger_registration",
                        ],
                        "required_total_weight_pct": 100.0,
                        "maximum_position_weight_pct": (
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
                            .max_position_weight_pct
                        ),
                        "candidate_consistency": (
                            "FROZEN_ROTATION_RANK_AND_SCORE_MUST_MATCH_"
                            "WEIGHTED_REGISTRATION"
                        ),
                        "missing_or_invalid_weight": "FAIL_CLOSED",
                        "execution_scope": (
                            "FORWARD_ONLY_EXPLORATORY_SINGLE_SLOT_ROUTING"
                        ),
                    }
            else:
                payload["candidate_universe"] = (
                    "SELECTED_TRUE_IN_LATEST_COMPLETED_UNIVERSE_RUN_"
                    "BEFORE_CONTEXT_CUTOFF"
                )
            payload["vwap_edge_filter"] = {
                "price_reference": "FROZEN_SIGNAL_FEATURE_RESIDUALS",
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS",
                "maximum_discount_bps": VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                    "FROZEN_SIGNAL_ZSCORE_1M",
                    "FROZEN_SIGNAL_ZSCORE_5M",
                ],
                "bounds": "INCLUSIVE",
                "zscore_sign": "STRICTLY_NEGATIVE_BOTH_HORIZONS",
                "ranking_score": (
                    "MIN_ABSOLUTE_NEGATIVE_ZSCORE_ACROSS_1M_AND_5M_"
                    "TIMES_FROZEN_TARGET_WEIGHT_PCT"
                    if spec.policy in _ROTATION_WEIGHTED_ZSCORE_POLICIES
                    else (
                        "MIN_ABSOLUTE_NEGATIVE_ZSCORE_ACROSS_1M_AND_5M"
                    )
                ),
                "ranking_tiebreaker": (
                    "UNWEIGHTED_ZSCORE_THEN_OBSERVED_COST_ADJUSTED_"
                    "VWAP_DISCOUNT_BPS"
                    if spec.policy in _ROTATION_WEIGHTED_ZSCORE_POLICIES
                    else "OBSERVED_COST_ADJUSTED_VWAP_DISCOUNT_BPS"
                ),
            }
        elif spec.policy in _OBSERVED_COST_TO_75BPS_VWAP_EDGE_POLICIES:
            payload["vwap_edge_filter"] = {
                "price_reference": (
                    "FROZEN_SIGNAL_FEATURE_RESIDUALS"
                ),
                "minimum_discount": (
                    "MAX_FROZEN_ROUND_TRIP_COST_AND_CAUSAL_QUANT_"
                    "ESTIMATED_ROUND_TRIP_COST_BPS"
                ),
                "observed_cost_source": (
                    "LATEST_CURRENT_QUANT_SCORE_BEFORE_CONTEXT_CUTOFF"
                ),
                "observed_cost_freshness": (
                    "UNEXPIRED_AND_AT_MOST_60_MINUTES_OLD_AT_"
                    "CONTEXT_CUTOFF"
                ),
                "missing_observed_cost": "FAIL_CLOSED",
                "maximum_discount": "FIXED_ABSOLUTE_VWAP_DISCOUNT_BPS",
                "maximum_discount_bps": (
                    VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
                ),
                "required_references": [
                    "SESSION_VWAP_1M",
                    "SESSION_VWAP_5M",
                ],
                "bounds": "INCLUSIVE",
            }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_payload(
    candidate: PortfolioRoutingCandidate,
    *,
    include_observed_cost: bool,
    include_relative_adjustment: bool,
    include_zscore: bool,
    include_rotation: bool,
    include_rotation_weight: bool,
    rejection_reasons: tuple[str, ...] | None = None,
) -> dict[str, object]:
    payload = asdict(candidate)
    if not include_observed_cost:
        payload.pop("observed_round_trip_cost_bps", None)
    if not include_relative_adjustment:
        payload.pop("risk_group", None)
        payload.pop("risk_group_peer_count", None)
        payload.pop("risk_group_relative_1m_bps", None)
        payload.pop("risk_group_relative_5m_bps", None)
    if not include_zscore:
        payload.pop("zscore_1m", None)
        payload.pop("zscore_5m", None)
    if not include_rotation:
        payload.pop("rotation_selected", None)
        payload.pop("rotation_rank", None)
        payload.pop("rotation_score", None)
    if not include_rotation_weight:
        payload.pop("rotation_target_weight_pct", None)
    if rejection_reasons is not None:
        payload["rejection_reasons"] = list(rejection_reasons)
    return payload


def _compounded_metrics(returns_pct: list[float]) -> tuple[float, float]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns_pct:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(
                max_drawdown,
                (peak - equity) / peak * 100,
            )
    return (equity - 1) * 100, max_drawdown


def _routing_policy(value: str) -> PortfolioRoutingPolicy:
    if value not in {
        "FIXED_PRIMARY",
        "SELECTED_UNIVERSE",
        "QUANT_CANDIDATE",
        "QUANT_WATCH_PLUS",
        "SELECTED_VWAP_EDGE",
        "VWAP_EDGE_POOL",
        "VWAP_EDGE_75BPS_POOL",
        "VWAP_EDGE_OBSERVED_COST_POOL",
        "VWAP_EDGE_OBS_COST_75BPS_POOL",
        "RISK_GROUP_REL_OBS_75BPS_POOL",
        "RISK_GROUP_LOO_OBS_75BPS_POOL",
        "SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
    }:
        raise ValueError(f"unsupported portfolio routing policy: {value}")
    return cast(PortfolioRoutingPolicy, value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
