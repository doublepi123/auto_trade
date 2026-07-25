from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence


PortfolioRoutingPolicy = Literal[
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
]

VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS = 75.0
RISK_GROUP_RELATIVE_MIN_PEERS = 3


@dataclass(frozen=True)
class PortfolioRoutingCandidate:
    symbol: str
    signal_decision_id: int
    source_config_version: str
    selection_selected: bool = False
    selection_rank: int | None = None
    selection_score: float | None = None
    quant_source: str = ""
    quant_action: str = ""
    quant_score: float | None = None
    quant_confidence: float | None = None
    residual_1m_bps: float | None = None
    residual_5m_bps: float | None = None
    round_trip_cost_bps: float | None = None
    observed_round_trip_cost_bps: float | None = None
    stop_distance_bps: float | None = None
    risk_group: str = ""
    risk_group_peer_count: int = 0
    risk_group_relative_1m_bps: float | None = None
    risk_group_relative_5m_bps: float | None = None

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("portfolio routing candidate symbol is required")
        if self.signal_decision_id <= 0:
            raise ValueError(
                "portfolio routing signal decision id must be positive"
            )
        if not self.source_config_version:
            raise ValueError(
                "portfolio routing source config version is required"
            )
        if self.selection_rank is not None and self.selection_rank <= 0:
            raise ValueError("portfolio routing selection rank must be positive")
        for value in (
            self.selection_score,
            self.quant_score,
            self.quant_confidence,
            self.residual_1m_bps,
            self.residual_5m_bps,
            self.round_trip_cost_bps,
            self.observed_round_trip_cost_bps,
            self.stop_distance_bps,
            self.risk_group_relative_1m_bps,
            self.risk_group_relative_5m_bps,
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(
                    "portfolio routing candidate metrics must be finite"
                )
        if (
            self.round_trip_cost_bps is not None
            and self.round_trip_cost_bps < 0
        ):
            raise ValueError(
                "portfolio routing round-trip cost must be non-negative"
            )
        if (
            self.observed_round_trip_cost_bps is not None
            and self.observed_round_trip_cost_bps < 0
        ):
            raise ValueError(
                "portfolio routing observed round-trip cost must be "
                "non-negative"
            )
        if (
            self.stop_distance_bps is not None
            and self.stop_distance_bps <= 0
        ):
            raise ValueError(
                "portfolio routing stop distance must be positive"
            )
        if self.risk_group_peer_count < 0:
            raise ValueError(
                "portfolio routing risk-group peer count must not be negative"
            )
        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(
            self,
            "risk_group",
            self.risk_group.strip(),
        )
        object.__setattr__(
            self,
            "quant_action",
            self.quant_action.strip().upper(),
        )

    @property
    def vwap_edge_eligible(self) -> bool:
        return self._vwap_edge_eligible(
            cost_bps=self.round_trip_cost_bps,
            max_discount_bps=self.stop_distance_bps,
        )

    @property
    def vwap_edge_score_bps(self) -> float:
        return self._vwap_edge_score_bps(
            cost_bps=self.round_trip_cost_bps,
            max_discount_bps=self.stop_distance_bps,
        )

    @property
    def fixed_75bps_vwap_edge_eligible(self) -> bool:
        return self._vwap_edge_eligible(
            cost_bps=self.round_trip_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
        )

    @property
    def fixed_75bps_vwap_edge_score_bps(self) -> float:
        return self._vwap_edge_score_bps(
            cost_bps=self.round_trip_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
        )

    @property
    def effective_observed_cost_bps(self) -> float | None:
        if (
            self.round_trip_cost_bps is None
            or self.observed_round_trip_cost_bps is None
        ):
            return None
        return max(
            float(self.round_trip_cost_bps),
            float(self.observed_round_trip_cost_bps),
        )

    @property
    def observed_cost_vwap_edge_eligible(self) -> bool:
        return self._vwap_edge_eligible(
            cost_bps=self.effective_observed_cost_bps,
            max_discount_bps=self.stop_distance_bps,
        )

    @property
    def observed_cost_vwap_edge_score_bps(self) -> float:
        return self._vwap_edge_score_bps(
            cost_bps=self.effective_observed_cost_bps,
            max_discount_bps=self.stop_distance_bps,
        )

    @property
    def observed_cost_fixed_75bps_vwap_edge_eligible(self) -> bool:
        return self._vwap_edge_eligible(
            cost_bps=self.effective_observed_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
        )

    @property
    def observed_cost_fixed_75bps_vwap_edge_score_bps(self) -> float:
        return self._vwap_edge_score_bps(
            cost_bps=self.effective_observed_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
        )

    @property
    def risk_group_relative_observed_cost_fixed_75bps_eligible(
        self,
    ) -> bool:
        if (
            not self.risk_group
            or self.risk_group_peer_count
            < RISK_GROUP_RELATIVE_MIN_PEERS
            or not self.observed_cost_fixed_75bps_vwap_edge_eligible
        ):
            return False
        values = (
            self.risk_group_relative_1m_bps,
            self.risk_group_relative_5m_bps,
            self.effective_observed_cost_bps,
        )
        if any(value is None for value in values):
            return False
        relative_1m = float(
            self.risk_group_relative_1m_bps or 0.0
        )
        relative_5m = float(
            self.risk_group_relative_5m_bps or 0.0
        )
        cost = float(self.effective_observed_cost_bps or 0.0)
        return (
            -VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
            <= relative_1m
            <= -cost
            and -VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS
            <= relative_5m
            <= -cost
        )

    @property
    def risk_group_relative_observed_cost_fixed_75bps_score_bps(
        self,
    ) -> float:
        if (
            not self.risk_group_relative_observed_cost_fixed_75bps_eligible
        ):
            return -1.0
        cost = float(self.effective_observed_cost_bps or 0.0)
        guaranteed_discount = min(
            -float(self.residual_1m_bps or 0.0),
            -float(self.residual_5m_bps or 0.0),
            -float(self.risk_group_relative_1m_bps or 0.0),
            -float(self.risk_group_relative_5m_bps or 0.0),
        )
        return guaranteed_discount - cost

    def _vwap_edge_eligible(
        self,
        *,
        cost_bps: float | None,
        max_discount_bps: float | None,
    ) -> bool:
        values = (
            self.residual_1m_bps,
            self.residual_5m_bps,
            cost_bps,
            max_discount_bps,
        )
        if any(value is None for value in values):
            return False
        residual_1m = float(self.residual_1m_bps or 0.0)
        residual_5m = float(self.residual_5m_bps or 0.0)
        cost = float(cost_bps or 0.0)
        max_discount = float(max_discount_bps or 0.0)
        if cost >= max_discount:
            return False
        return (
            -max_discount <= residual_1m <= -cost
            and -max_discount <= residual_5m <= -cost
        )

    def _vwap_edge_score_bps(
        self,
        *,
        cost_bps: float | None,
        max_discount_bps: float | None,
    ) -> float:
        if not self._vwap_edge_eligible(
            cost_bps=cost_bps,
            max_discount_bps=max_discount_bps,
        ):
            return -1.0
        minimum_discount = min(
            -float(self.residual_1m_bps or 0.0),
            -float(self.residual_5m_bps or 0.0),
        )
        return minimum_discount - float(cost_bps or 0.0)


def rank_portfolio_candidates(
    candidates: Sequence[PortfolioRoutingCandidate],
    *,
    policy: PortfolioRoutingPolicy,
    primary_symbol: str,
) -> tuple[PortfolioRoutingCandidate, ...]:
    """Return a deterministic causal ranking for one signal minute."""

    normalized_primary = primary_symbol.strip().upper()
    if not normalized_primary:
        raise ValueError("portfolio routing primary symbol is required")
    by_symbol: dict[str, PortfolioRoutingCandidate] = {}
    for candidate in candidates:
        if candidate.symbol in by_symbol:
            raise ValueError(
                f"duplicate portfolio routing candidate: {candidate.symbol}"
            )
        by_symbol[candidate.symbol] = candidate

    if policy == "FIXED_PRIMARY":
        primary = by_symbol.get(normalized_primary)
        return (primary,) if primary is not None else ()
    if policy == "SELECTED_UNIVERSE":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.selection_selected
            and candidate.selection_rank is not None
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                candidate.selection_rank or 10_000,
                -_metric(candidate.selection_score),
                candidate.symbol,
            ),
        ))
    if policy == "SELECTED_VWAP_EDGE":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.selection_selected
            and candidate.selection_rank is not None
            and candidate.vwap_edge_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                candidate.selection_rank or 10_000,
                -candidate.vwap_edge_score_bps,
                candidate.symbol,
            ),
        ))
    if policy == "VWAP_EDGE_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.vwap_edge_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.vwap_edge_score_bps,
                0 if candidate.selection_selected else 1,
                (
                    candidate.selection_rank
                    if candidate.selection_rank is not None
                    else 10_000
                ),
                candidate.symbol,
            ),
        ))
    if policy == "VWAP_EDGE_75BPS_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.fixed_75bps_vwap_edge_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.fixed_75bps_vwap_edge_score_bps,
                0 if candidate.selection_selected else 1,
                (
                    candidate.selection_rank
                    if candidate.selection_rank is not None
                    else 10_000
                ),
                candidate.symbol,
            ),
        ))
    if policy == "VWAP_EDGE_OBSERVED_COST_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.observed_cost_vwap_edge_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.observed_cost_vwap_edge_score_bps,
                0 if candidate.selection_selected else 1,
                (
                    candidate.selection_rank
                    if candidate.selection_rank is not None
                    else 10_000
                ),
                candidate.symbol,
            ),
        ))
    if policy == "VWAP_EDGE_OBS_COST_75BPS_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.observed_cost_fixed_75bps_vwap_edge_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.observed_cost_fixed_75bps_vwap_edge_score_bps,
                0 if candidate.selection_selected else 1,
                (
                    candidate.selection_rank
                    if candidate.selection_rank is not None
                    else 10_000
                ),
                candidate.symbol,
            ),
        ))
    if policy == "RISK_GROUP_REL_OBS_75BPS_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if (
                candidate
                .risk_group_relative_observed_cost_fixed_75bps_eligible
            )
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -(
                    candidate
                    .risk_group_relative_observed_cost_fixed_75bps_score_bps
                ),
                0 if candidate.selection_selected else 1,
                (
                    candidate.selection_rank
                    if candidate.selection_rank is not None
                    else 10_000
                ),
                candidate.symbol,
            ),
        ))
    if policy == "QUANT_CANDIDATE":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if (
                candidate.quant_source == "quant_v5"
                and candidate.quant_action == "CANDIDATE"
            )
        ]
    elif policy == "QUANT_WATCH_PLUS":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if (
                candidate.quant_source == "quant_v5"
                and candidate.quant_action in {"CANDIDATE", "WATCH"}
            )
        ]
    else:
        raise ValueError(f"unsupported portfolio routing policy: {policy}")

    return tuple(sorted(
        eligible,
        key=lambda candidate: (
            0 if candidate.quant_action == "CANDIDATE" else 1,
            -_metric(candidate.quant_score),
            -_metric(candidate.quant_confidence),
            (
                candidate.selection_rank
                if candidate.selection_rank is not None
                else 10_000
            ),
            candidate.symbol,
        ),
    ))


def _metric(value: float | None) -> float:
    return float(value) if value is not None else -1.0
