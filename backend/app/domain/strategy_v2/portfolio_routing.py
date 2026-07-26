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
    "RISK_GROUP_LOO_OBS_75BPS_POOL",
    "SECTOR_LOO_OBS_75BPS_POOL",
    "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
    "SELECTED_ZSCORE_OBS_75BPS_POOL",
    "ROTATION_ZSCORE_OBS_75BPS_POOL",
    "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
]

VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS = 75.0
RISK_GROUP_RELATIVE_MIN_PEERS = 3
RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS = 2


@dataclass(frozen=True)
class PortfolioRoutingCandidate:
    symbol: str
    signal_decision_id: int
    source_config_version: str
    selection_selected: bool = False
    selection_rank: int | None = None
    selection_score: float | None = None
    rotation_selected: bool = False
    rotation_rank: int | None = None
    rotation_score: float | None = None
    rotation_target_weight_pct: float | None = None
    quant_source: str = ""
    quant_action: str = ""
    quant_score: float | None = None
    quant_confidence: float | None = None
    residual_1m_bps: float | None = None
    residual_5m_bps: float | None = None
    zscore_1m: float | None = None
    zscore_5m: float | None = None
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
        if self.rotation_rank is not None and self.rotation_rank <= 0:
            raise ValueError("portfolio routing rotation rank must be positive")
        if (
            self.rotation_target_weight_pct is not None
            and not 0 < self.rotation_target_weight_pct <= 100
        ):
            raise ValueError(
                "portfolio routing rotation target weight must be in "
                "(0, 100]"
            )
        for value in (
            self.selection_score,
            self.rotation_score,
            self.rotation_target_weight_pct,
            self.quant_score,
            self.quant_confidence,
            self.residual_1m_bps,
            self.residual_5m_bps,
            self.zscore_1m,
            self.zscore_5m,
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
    def zscore_observed_cost_fixed_75bps_eligible(self) -> bool:
        if not self.observed_cost_fixed_75bps_vwap_edge_eligible:
            return False
        if self.zscore_1m is None or self.zscore_5m is None:
            return False
        return self.zscore_1m < 0 and self.zscore_5m < 0

    @property
    def zscore_observed_cost_fixed_75bps_score(self) -> float:
        if not self.zscore_observed_cost_fixed_75bps_eligible:
            return -1.0
        return min(
            -float(self.zscore_1m or 0.0),
            -float(self.zscore_5m or 0.0),
        )

    @property
    def rotation_weighted_zscore_score(self) -> float:
        if (
            not self.zscore_observed_cost_fixed_75bps_eligible
            or self.rotation_target_weight_pct is None
        ):
            return -1.0
        return (
            self.zscore_observed_cost_fixed_75bps_score
            * self.rotation_target_weight_pct
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

    @property
    def risk_group_leave_one_out_observed_cost_fixed_75bps_eligible(
        self,
    ) -> bool:
        if (
            not self.risk_group
            or self.risk_group_peer_count
            < RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS
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
    def risk_group_leave_one_out_observed_cost_fixed_75bps_score_bps(
        self,
    ) -> float:
        if (
            not self
            .risk_group_leave_one_out_observed_cost_fixed_75bps_eligible
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
    if policy == "SELECTED_ZSCORE_OBS_75BPS_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.selection_selected
            and candidate.selection_rank is not None
            and candidate.zscore_observed_cost_fixed_75bps_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.zscore_observed_cost_fixed_75bps_score,
                -candidate.observed_cost_fixed_75bps_vwap_edge_score_bps,
                candidate.selection_rank or 10_000,
                candidate.symbol,
            ),
        ))
    if policy == "ROTATION_ZSCORE_OBS_75BPS_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.rotation_selected
            and candidate.rotation_rank is not None
            and candidate.zscore_observed_cost_fixed_75bps_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.zscore_observed_cost_fixed_75bps_score,
                -candidate.observed_cost_fixed_75bps_vwap_edge_score_bps,
                candidate.rotation_rank or 10_000,
                candidate.symbol,
            ),
        ))
    if policy == "ROTATION_IV_WEIGHTED_ZSCORE_POOL":
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if candidate.rotation_selected
            and candidate.rotation_rank is not None
            and candidate.rotation_target_weight_pct is not None
            and candidate.zscore_observed_cost_fixed_75bps_eligible
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -candidate.rotation_weighted_zscore_score,
                -candidate.zscore_observed_cost_fixed_75bps_score,
                -candidate.observed_cost_fixed_75bps_vwap_edge_score_bps,
                candidate.rotation_rank or 10_000,
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
    if policy in {
        "RISK_GROUP_LOO_OBS_75BPS_POOL",
        "SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
    }:
        eligible = [
            candidate
            for candidate in by_symbol.values()
            if (
                candidate
                .risk_group_leave_one_out_observed_cost_fixed_75bps_eligible
            )
            and (
                policy != "SELECTED_SECTOR_LOO_OBS_75BPS_POOL"
                or candidate.selection_selected
            )
        ]
        return tuple(sorted(
            eligible,
            key=lambda candidate: (
                -(
                    candidate
                    .risk_group_leave_one_out_observed_cost_fixed_75bps_score_bps
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


def portfolio_candidate_rejection_reasons(
    candidate: PortfolioRoutingCandidate,
    *,
    policy: PortfolioRoutingPolicy,
    primary_symbol: str,
) -> tuple[str, ...]:
    """Explain why one causal candidate is excluded by a routing policy."""

    if rank_portfolio_candidates(
        (candidate,),
        policy=policy,
        primary_symbol=primary_symbol,
    ):
        return ()

    reasons: list[str] = []
    if policy == "FIXED_PRIMARY":
        reasons.append("NOT_PRIMARY_SYMBOL")
    if policy in {
        "SELECTED_UNIVERSE",
        "SELECTED_VWAP_EDGE",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_ZSCORE_OBS_75BPS_POOL",
    }:
        if not candidate.selection_selected:
            reasons.append("NOT_SELECTED_UNIVERSE")
        if candidate.selection_rank is None:
            reasons.append("MISSING_SELECTION_RANK")
    if policy in {
        "ROTATION_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
    }:
        if not candidate.rotation_selected:
            reasons.append("NOT_ROTATION_SELECTED")
        if candidate.rotation_rank is None:
            reasons.append("MISSING_ROTATION_RANK")
    if (
        policy == "ROTATION_IV_WEIGHTED_ZSCORE_POOL"
        and candidate.rotation_target_weight_pct is None
    ):
        reasons.append("MISSING_ROTATION_TARGET_WEIGHT")
    if policy in {"QUANT_CANDIDATE", "QUANT_WATCH_PLUS"}:
        if candidate.quant_source != "quant_v5":
            reasons.append("QUANT_SOURCE_NOT_CURRENT")
        accepted_actions = (
            {"CANDIDATE"}
            if policy == "QUANT_CANDIDATE"
            else {"CANDIDATE", "WATCH"}
        )
        if candidate.quant_action not in accepted_actions:
            reasons.append("QUANT_ACTION_NOT_ELIGIBLE")

    if policy in {"SELECTED_VWAP_EDGE", "VWAP_EDGE_POOL"}:
        reasons.extend(_vwap_band_rejection_reasons(
            candidate,
            cost_bps=candidate.round_trip_cost_bps,
            max_discount_bps=candidate.stop_distance_bps,
            observed_cost_required=False,
        ))
    if policy == "VWAP_EDGE_75BPS_POOL":
        reasons.extend(_vwap_band_rejection_reasons(
            candidate,
            cost_bps=candidate.round_trip_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
            observed_cost_required=False,
        ))
    if policy == "VWAP_EDGE_OBSERVED_COST_POOL":
        reasons.extend(_vwap_band_rejection_reasons(
            candidate,
            cost_bps=candidate.effective_observed_cost_bps,
            max_discount_bps=candidate.stop_distance_bps,
            observed_cost_required=True,
        ))
    if policy in {
        "VWAP_EDGE_OBS_COST_75BPS_POOL",
        "RISK_GROUP_REL_OBS_75BPS_POOL",
        "RISK_GROUP_LOO_OBS_75BPS_POOL",
        "SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
    }:
        reasons.extend(_vwap_band_rejection_reasons(
            candidate,
            cost_bps=candidate.effective_observed_cost_bps,
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
            observed_cost_required=True,
        ))
    if policy in {
        "SELECTED_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_ZSCORE_OBS_75BPS_POOL",
        "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
    }:
        if candidate.zscore_1m is None:
            reasons.append("MISSING_ZSCORE_1M")
        elif candidate.zscore_1m >= 0:
            reasons.append("ZSCORE_1M_NOT_NEGATIVE")
        if candidate.zscore_5m is None:
            reasons.append("MISSING_ZSCORE_5M")
        elif candidate.zscore_5m >= 0:
            reasons.append("ZSCORE_5M_NOT_NEGATIVE")
    if policy in {
        "RISK_GROUP_REL_OBS_75BPS_POOL",
        "RISK_GROUP_LOO_OBS_75BPS_POOL",
        "SECTOR_LOO_OBS_75BPS_POOL",
        "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
    }:
        minimum_peers = (
            RISK_GROUP_RELATIVE_MIN_PEERS
            if policy == "RISK_GROUP_REL_OBS_75BPS_POOL"
            else RISK_GROUP_LEAVE_ONE_OUT_MIN_PEERS
        )
        if not candidate.risk_group:
            reasons.append("MISSING_RELATIVE_GROUP")
        if candidate.risk_group_peer_count < minimum_peers:
            reasons.append("INSUFFICIENT_RELATIVE_PEERS")
        reasons.extend(_residual_band_rejection_reasons(
            residual_1m_bps=candidate.risk_group_relative_1m_bps,
            residual_5m_bps=candidate.risk_group_relative_5m_bps,
            cost_bps=(
                candidate.effective_observed_cost_bps
                if candidate.effective_observed_cost_bps is not None
                else candidate.round_trip_cost_bps
            ),
            max_discount_bps=VWAP_EDGE_FIXED_MAX_DISCOUNT_BPS,
            prefix="RELATIVE_VWAP",
        ))

    return tuple(dict.fromkeys(reasons or ["POLICY_INELIGIBLE"]))


def _vwap_band_rejection_reasons(
    candidate: PortfolioRoutingCandidate,
    *,
    cost_bps: float | None,
    max_discount_bps: float | None,
    observed_cost_required: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.round_trip_cost_bps is None:
        reasons.append("MISSING_FROZEN_COST")
    if (
        observed_cost_required
        and candidate.observed_round_trip_cost_bps is None
    ):
        reasons.append("MISSING_OBSERVED_COST")
    if max_discount_bps is None:
        reasons.append("MISSING_MAX_DISCOUNT")
    reasons.extend(_residual_band_rejection_reasons(
        residual_1m_bps=candidate.residual_1m_bps,
        residual_5m_bps=candidate.residual_5m_bps,
        cost_bps=(
            cost_bps
            if cost_bps is not None
            else candidate.round_trip_cost_bps
        ),
        max_discount_bps=max_discount_bps,
        prefix="VWAP",
    ))
    return tuple(reasons)


def _residual_band_rejection_reasons(
    *,
    residual_1m_bps: float | None,
    residual_5m_bps: float | None,
    cost_bps: float | None,
    max_discount_bps: float | None,
    prefix: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if residual_1m_bps is None:
        reasons.append(f"MISSING_{prefix}_RESIDUAL_1M")
    if residual_5m_bps is None:
        reasons.append(f"MISSING_{prefix}_RESIDUAL_5M")
    if cost_bps is None or max_discount_bps is None:
        return tuple(reasons)
    if cost_bps >= max_discount_bps:
        reasons.append("COST_NOT_BELOW_MAX_DISCOUNT")
        return tuple(reasons)
    for horizon, residual in (
        ("1M", residual_1m_bps),
        ("5M", residual_5m_bps),
    ):
        if residual is None:
            continue
        if residual < -max_discount_bps:
            reasons.append(f"{prefix}_{horizon}_BELOW_MAX_DISCOUNT")
        elif residual > -cost_bps:
            reasons.append(f"{prefix}_{horizon}_NOT_DISCOUNTED_AFTER_COST")
    return tuple(reasons)


def _metric(value: float | None) -> float:
    return float(value) if value is not None else -1.0
