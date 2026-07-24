from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Sequence


OPENING_CONTINUATION_UNIVERSE_VERSION = (
    "opening-continuation-universe-v1"
)

_SOFT_EXCLUSION_REASONS = frozenset(
    {"SECTOR_CAP", "BELOW_SELECTION_CUTOFF"}
)


@dataclass(frozen=True)
class OpeningMomentumUniverseConfig:
    max_selected: int = 12
    max_per_sector: int = 2
    liquidity_weight: float = 0.30
    spread_weight: float = 0.25
    opportunity_weight: float = 0.25
    momentum_weight: float = 0.10
    trend_efficiency_weight: float = 0.10

    def __post_init__(self) -> None:
        if self.max_selected < 1:
            raise ValueError("max_selected must be positive")
        if self.max_per_sector < 1:
            raise ValueError("max_per_sector must be positive")
        weights = (
            self.liquidity_weight,
            self.spread_weight,
            self.opportunity_weight,
            self.momentum_weight,
            self.trend_efficiency_weight,
        )
        if any(
            not math.isfinite(value) or value < 0
            for value in weights
        ):
            raise ValueError(
                "opening momentum universe weights must be finite "
                "and non-negative"
            )
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-12):
            raise ValueError(
                "opening momentum universe weights must sum to one"
            )

    def version_hash(self) -> str:
        payload = {
            "policy_version": OPENING_CONTINUATION_UNIVERSE_VERSION,
            **asdict(self),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OpeningMomentumUniverseCandidate:
    symbol: str
    sector: str
    avg_dollar_volume: float | None
    relative_spread_bps: float | None
    opportunity_to_cost_ratio: float | None
    momentum_5d_pct: float | None
    trend_efficiency_10d: float | None
    exclusion_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpeningMomentumUniverseSelection:
    symbol: str
    selected: bool
    rank: int | None
    score: float
    exclusion_reasons: tuple[str, ...]


def opening_momentum_variant_config_version(
    opening_config_version: str,
    universe_config: OpeningMomentumUniverseConfig,
) -> str:
    payload = (
        f"{opening_config_version}:"
        f"{universe_config.version_hash()}"
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def select_opening_momentum_universe(
    candidates: Sequence[OpeningMomentumUniverseCandidate],
    config: OpeningMomentumUniverseConfig | None = None,
) -> list[OpeningMomentumUniverseSelection]:
    selection_config = config or OpeningMomentumUniverseConfig()
    by_symbol: dict[str, OpeningMomentumUniverseCandidate] = {}
    hard_reasons: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        if not candidate.symbol:
            raise ValueError("candidate symbol must not be empty")
        if candidate.symbol in by_symbol:
            raise ValueError(
                f"duplicate candidate symbol: {candidate.symbol}"
            )
        by_symbol[candidate.symbol] = candidate
        reasons = tuple(
            reason
            for reason in candidate.exclusion_reasons
            if reason not in _SOFT_EXCLUSION_REASONS
        )
        metrics = (
            candidate.avg_dollar_volume,
            candidate.relative_spread_bps,
            candidate.opportunity_to_cost_ratio,
            candidate.momentum_5d_pct,
            candidate.trend_efficiency_10d,
        )
        if not reasons and any(
            value is None or not math.isfinite(value)
            for value in metrics
        ):
            reasons = ("DATA_INCOMPLETE_METRICS",)
        hard_reasons[candidate.symbol] = reasons

    eligible = [
        candidate
        for candidate in candidates
        if not hard_reasons[candidate.symbol]
    ]
    liquidity = _percentile_ranks(
        {
            candidate.symbol: math.log10(
                max(float(candidate.avg_dollar_volume or 0.0), 1.0)
            )
            for candidate in eligible
        },
        higher_is_better=True,
    )
    spread = _percentile_ranks(
        {
            candidate.symbol: float(
                candidate.relative_spread_bps or 0.0
            )
            for candidate in eligible
        },
        higher_is_better=False,
    )
    opportunity = _percentile_ranks(
        {
            candidate.symbol: float(
                candidate.opportunity_to_cost_ratio or 0.0
            )
            for candidate in eligible
        },
        higher_is_better=True,
    )
    momentum = _percentile_ranks(
        {
            candidate.symbol: float(
                candidate.momentum_5d_pct or 0.0
            )
            for candidate in eligible
        },
        higher_is_better=True,
    )
    trend_efficiency = _percentile_ranks(
        {
            candidate.symbol: float(
                candidate.trend_efficiency_10d or 0.0
            )
            for candidate in eligible
        },
        higher_is_better=True,
    )

    scores: dict[str, float] = {}
    for candidate in eligible:
        symbol = candidate.symbol
        scores[symbol] = 100 * (
            selection_config.liquidity_weight * liquidity[symbol]
            + selection_config.spread_weight * spread[symbol]
            + selection_config.opportunity_weight
            * opportunity[symbol]
            + selection_config.momentum_weight * momentum[symbol]
            + selection_config.trend_efficiency_weight
            * trend_efficiency[symbol]
        )

    eligible.sort(
        key=lambda candidate: (
            -scores[candidate.symbol],
            candidate.symbol,
        )
    )
    selected_symbols: list[str] = []
    sector_counts: dict[str, int] = {}
    for candidate in eligible:
        if (
            sector_counts.get(candidate.sector, 0)
            >= selection_config.max_per_sector
        ):
            continue
        selected_symbols.append(candidate.symbol)
        sector_counts[candidate.sector] = (
            sector_counts.get(candidate.sector, 0) + 1
        )
        if len(selected_symbols) >= selection_config.max_selected:
            break

    selected_set = set(selected_symbols)
    ranks = {
        symbol: index + 1
        for index, symbol in enumerate(selected_symbols)
    }
    result: list[OpeningMomentumUniverseSelection] = []
    for candidate in candidates:
        symbol = candidate.symbol
        reasons = list(hard_reasons[symbol])
        if not reasons and symbol not in selected_set:
            if (
                sector_counts.get(candidate.sector, 0)
                >= selection_config.max_per_sector
            ):
                reasons.append("SECTOR_CAP")
            else:
                reasons.append("BELOW_SELECTION_CUTOFF")
        result.append(
            OpeningMomentumUniverseSelection(
                symbol=symbol,
                selected=symbol in selected_set,
                rank=ranks.get(symbol),
                score=scores.get(symbol, 0.0),
                exclusion_reasons=tuple(reasons),
            )
        )
    return sorted(
        result,
        key=lambda row: (
            not row.selected,
            row.rank or 10_000,
            -row.score,
            row.symbol,
        ),
    )


def _percentile_ranks(
    values: dict[str, float],
    *,
    higher_is_better: bool,
) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(
        values,
        key=lambda symbol: (
            -values[symbol] if higher_is_better else values[symbol],
            symbol,
        ),
    )
    if len(ordered) == 1:
        return {ordered[0]: 1.0}
    denominator = len(ordered) - 1
    return {
        symbol: 1.0 - index / denominator
        for index, symbol in enumerate(ordered)
    }
