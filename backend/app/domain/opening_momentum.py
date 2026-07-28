from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from statistics import median
from typing import Literal, Mapping, Sequence


ALGORITHM_VERSION = (
    "cross-sectional-opening-momentum-v3-preopen-frozen-universe"
)
REVERSAL_ALGORITHM_VERSION = "cross-sectional-opening-reversal-v1"


OpeningRangeBreakoutRanking = Literal[
    "BREAKOUT_DEPTH",
    "OPENING_RETURN",
]


@dataclass(frozen=True)
class OpeningMomentumConfig:
    """Frozen parameters for the prospective opening-momentum shadow."""

    signal_minutes: int = 30
    execution_delay_minutes: int = 1
    holding_minutes: int = 30
    minimum_universe_size: int = 8
    minimum_market_return_bps: float = -25.0
    minimum_candidate_return_bps: float = 0.0
    minimum_excess_return_bps: float = 25.0
    one_side_fee_rate: float = 0.0005
    one_side_slippage_bps: float = 2.0
    stop_loss_pct: float | None = None

    def __post_init__(self) -> None:
        numeric_values = (
            self.minimum_market_return_bps,
            self.minimum_candidate_return_bps,
            self.minimum_excess_return_bps,
            self.one_side_fee_rate,
            self.one_side_slippage_bps,
        )
        if any(not math.isfinite(value) for value in numeric_values):
            raise ValueError("opening momentum parameters must be finite")
        if self.signal_minutes <= 0 or self.signal_minutes > 120:
            raise ValueError("signal_minutes must be in [1, 120]")
        if (
            self.execution_delay_minutes < 1
            or self.execution_delay_minutes > 5
        ):
            raise ValueError(
                "execution_delay_minutes must be in [1, 5]"
            )
        if self.holding_minutes <= 0 or self.holding_minutes > 120:
            raise ValueError("holding_minutes must be in [1, 120]")
        if self.minimum_universe_size < 2:
            raise ValueError("minimum_universe_size must be at least 2")
        if self.minimum_excess_return_bps < 0:
            raise ValueError("minimum_excess_return_bps must be non-negative")
        if not 0 <= self.one_side_fee_rate <= 0.1:
            raise ValueError("one_side_fee_rate must be in [0, 0.1]")
        if not 0 <= self.one_side_slippage_bps <= 50:
            raise ValueError("one_side_slippage_bps must be in [0, 50]")
        if self.stop_loss_pct is not None and (
            not math.isfinite(self.stop_loss_pct)
            or not 0 < self.stop_loss_pct <= 20
        ):
            raise ValueError("stop_loss_pct must be in (0, 20] when set")

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * (
            self.one_side_fee_rate * 10_000
            + self.one_side_slippage_bps
        )

    def version_hash(self) -> str:
        config_payload = asdict(self)
        if self.stop_loss_pct is None:
            # Preserve every pre-stop config hash so adding the optional
            # execution field cannot orphan already-collected evidence.
            config_payload.pop("stop_loss_pct")
        payload = {
            "algorithm_version": ALGORITHM_VERSION,
            **config_payload,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OpeningMomentumObservation:
    symbol: str
    session_open: float
    signal_close: float
    entry_open: float | None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        object.__setattr__(self, "symbol", symbol)
        prices = (self.session_open, self.signal_close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ValueError("session_open and signal_close must be positive")
        if self.entry_open is not None and (
            not math.isfinite(self.entry_open) or self.entry_open <= 0
        ):
            raise ValueError("entry_open must be positive when present")

    @property
    def opening_return_bps(self) -> float:
        return (self.signal_close / self.session_open - 1) * 10_000


@dataclass(frozen=True)
class OpeningMomentumRank:
    symbol: str
    opening_return_bps: float


@dataclass(frozen=True)
class OpeningMomentumDecision:
    action: Literal["ENTER_LONG", "SKIP"]
    reason: str
    universe_size: int
    market_return_bps: float | None
    candidate_symbol: str | None
    candidate_return_bps: float | None
    excess_return_bps: float | None
    entry_price: float | None
    ranking: tuple[OpeningMomentumRank, ...]


def _rank_opening_observations(
    observations: Sequence[OpeningMomentumObservation],
) -> tuple[
    dict[str, OpeningMomentumObservation],
    tuple[OpeningMomentumRank, ...],
]:
    by_symbol: dict[str, OpeningMomentumObservation] = {}
    for item in observations:
        if item.symbol in by_symbol:
            raise ValueError(f"duplicate opening observation: {item.symbol}")
        by_symbol[item.symbol] = item

    ranking = tuple(
        OpeningMomentumRank(
            symbol=item.symbol,
            opening_return_bps=item.opening_return_bps,
        )
        for item in sorted(
            by_symbol.values(),
            key=lambda row: (-row.opening_return_bps, row.symbol),
        )
    )
    return by_symbol, ranking


def _evaluate_ranked_opening_momentum(
    *,
    by_symbol: Mapping[str, OpeningMomentumObservation],
    ranking: tuple[OpeningMomentumRank, ...],
    candidate: OpeningMomentumRank | None,
    params: OpeningMomentumConfig,
    entry_reason: str,
    missing_candidate_reason: str,
) -> OpeningMomentumDecision:
    if len(ranking) < params.minimum_universe_size:
        return OpeningMomentumDecision(
            action="SKIP",
            reason="INSUFFICIENT_UNIVERSE",
            universe_size=len(ranking),
            market_return_bps=None,
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    market_return_bps = median(
        item.opening_return_bps for item in ranking
    )
    if candidate is None:
        return OpeningMomentumDecision(
            action="SKIP",
            reason=missing_candidate_reason,
            universe_size=len(ranking),
            market_return_bps=market_return_bps,
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    excess_return_bps = (
        candidate.opening_return_bps - market_return_bps
    )
    observation = by_symbol[candidate.symbol]
    action: Literal["ENTER_LONG", "SKIP"] = "ENTER_LONG"
    reason = entry_reason
    if market_return_bps < params.minimum_market_return_bps:
        action = "SKIP"
        reason = "MARKET_FILTER"
    elif (
        candidate.opening_return_bps
        <= params.minimum_candidate_return_bps
    ):
        action = "SKIP"
        reason = "CANDIDATE_NOT_POSITIVE"
    elif excess_return_bps < params.minimum_excess_return_bps:
        action = "SKIP"
        reason = "EXCESS_RETURN_FILTER"
    elif observation.entry_open is None:
        action = "SKIP"
        reason = "ENTRY_BAR_MISSING"

    return OpeningMomentumDecision(
        action=action,
        reason=reason,
        universe_size=len(ranking),
        market_return_bps=market_return_bps,
        candidate_symbol=candidate.symbol,
        candidate_return_bps=candidate.opening_return_bps,
        excess_return_bps=excess_return_bps,
        entry_price=(
            observation.entry_open
            if action == "ENTER_LONG"
            else None
        ),
        ranking=ranking,
    )


def evaluate_opening_momentum(
    observations: Sequence[OpeningMomentumObservation],
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Rank completed opening returns and apply the frozen entry gates."""

    params = config or OpeningMomentumConfig()
    by_symbol, ranking = _rank_opening_observations(observations)
    return _evaluate_ranked_opening_momentum(
        by_symbol=by_symbol,
        ranking=ranking,
        candidate=ranking[0] if ranking else None,
        params=params,
        entry_reason="OPENING_LEADER",
        missing_candidate_reason="CANDIDATE_MISSING",
    )


def evaluate_opening_range_breakout(
    observations: Sequence[OpeningMomentumObservation],
    *,
    opening_range_high_by_symbol: Mapping[str, float],
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Select the strongest close-confirmed opening-range breakout."""

    return _evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=opening_range_high_by_symbol,
        eligible_symbols=None,
        candidate_ranking="BREAKOUT_DEPTH",
        entry_reason="FIVE_MINUTE_OPENING_RANGE_BREAKOUT",
        config=config,
    )


def evaluate_stocks_in_play_opening_range_breakout(
    observations: Sequence[OpeningMomentumObservation],
    *,
    opening_range_high_by_symbol: Mapping[str, float],
    opening_activity_ratio_by_symbol: Mapping[str, float],
    maximum_stocks_in_play: int = 20,
    minimum_opening_activity_ratio: float | None = None,
    candidate_ranking: OpeningRangeBreakoutRanking = "BREAKOUT_DEPTH",
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Restrict ORB candidates to the most active opening names."""

    if maximum_stocks_in_play <= 0:
        raise ValueError("maximum_stocks_in_play must be positive")
    if minimum_opening_activity_ratio is not None and (
        not math.isfinite(minimum_opening_activity_ratio)
        or minimum_opening_activity_ratio <= 0
    ):
        raise ValueError(
            "minimum_opening_activity_ratio must be positive and finite"
        )
    if candidate_ranking not in {"BREAKOUT_DEPTH", "OPENING_RETURN"}:
        raise ValueError("candidate_ranking is unsupported")
    normalized_activity: dict[str, float] = {}
    for raw_symbol, raw_ratio in opening_activity_ratio_by_symbol.items():
        symbol = raw_symbol.strip().upper()
        if not symbol:
            raise ValueError("opening activity symbol is required")
        if symbol in normalized_activity:
            raise ValueError(f"duplicate opening activity ratio: {symbol}")
        ratio = float(raw_ratio)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError(
                "opening activity ratios must contain positive finite values"
            )
        normalized_activity[symbol] = ratio

    observation_symbols = {item.symbol for item in observations}
    if not observation_symbols.issubset(normalized_activity):
        params = config or OpeningMomentumConfig()
        _, ranking = _rank_opening_observations(observations)
        return OpeningMomentumDecision(
            action="SKIP",
            reason="OPENING_ACTIVITY_DATA_INCOMPLETE",
            universe_size=len(ranking),
            market_return_bps=(
                median(item.opening_return_bps for item in ranking)
                if len(ranking) >= params.minimum_universe_size
                else None
            ),
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    stocks_in_play = {
        symbol
        for symbol, _ in sorted(
            (
                (symbol, normalized_activity[symbol])
                for symbol in observation_symbols
                if (
                    minimum_opening_activity_ratio is None
                    or normalized_activity[symbol]
                    >= minimum_opening_activity_ratio
                )
            ),
            key=lambda item: (-item[1], item[0]),
        )[:maximum_stocks_in_play]
    }
    return _evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=opening_range_high_by_symbol,
        eligible_symbols=stocks_in_play,
        candidate_ranking=candidate_ranking,
        entry_reason=(
            "OPENING_RETURN_RERANKED_STOCKS_IN_PLAY_"
            "FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
            if candidate_ranking == "OPENING_RETURN"
            else "STOCKS_IN_PLAY_FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
        ),
        config=config,
    )


def _evaluate_opening_range_breakout(
    observations: Sequence[OpeningMomentumObservation],
    *,
    opening_range_high_by_symbol: Mapping[str, float],
    eligible_symbols: set[str] | None,
    candidate_ranking: OpeningRangeBreakoutRanking,
    entry_reason: str,
    config: OpeningMomentumConfig | None,
) -> OpeningMomentumDecision:
    normalized_highs: dict[str, float] = {}
    for raw_symbol, raw_high in opening_range_high_by_symbol.items():
        symbol = raw_symbol.strip().upper()
        if not symbol:
            raise ValueError("opening range symbol is required")
        if symbol in normalized_highs:
            raise ValueError(f"duplicate opening range high: {symbol}")
        high = float(raw_high)
        if not math.isfinite(high) or high <= 0:
            raise ValueError(
                "opening range highs must contain positive finite values"
            )
        normalized_highs[symbol] = high

    params = config or OpeningMomentumConfig()
    by_symbol, ranking = _rank_opening_observations(observations)
    if len(ranking) < params.minimum_universe_size:
        return OpeningMomentumDecision(
            action="SKIP",
            reason="INSUFFICIENT_UNIVERSE",
            universe_size=len(ranking),
            market_return_bps=None,
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    market_return_bps = median(
        item.opening_return_bps for item in ranking
    )
    eligible = [
        item
        for item in by_symbol.values()
        if (
            (eligible_symbols is None or item.symbol in eligible_symbols)
            and (
                opening_range_high := normalized_highs.get(item.symbol)
            ) is not None
            and item.signal_close > opening_range_high
        )
    ]
    if not eligible:
        return OpeningMomentumDecision(
            action="SKIP",
            reason="OPENING_RANGE_BREAKOUT_MISSING",
            universe_size=len(ranking),
            market_return_bps=market_return_bps,
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    def candidate_key(
        item: OpeningMomentumObservation,
    ) -> tuple[float, float, str]:
        breakout_depth = (
            item.signal_close / normalized_highs[item.symbol] - 1
        )
        if candidate_ranking == "OPENING_RETURN":
            return (
                -item.opening_return_bps,
                -breakout_depth,
                item.symbol,
            )
        return (
            -breakout_depth,
            -item.opening_return_bps,
            item.symbol,
        )

    candidate_observation = min(eligible, key=candidate_key)
    candidate_return_bps = candidate_observation.opening_return_bps
    excess_return_bps = candidate_return_bps - market_return_bps
    if candidate_observation.entry_open is None:
        action: Literal["ENTER_LONG", "SKIP"] = "SKIP"
        reason = "ENTRY_BAR_MISSING"
        entry_price = None
    else:
        action = "ENTER_LONG"
        reason = entry_reason
        entry_price = candidate_observation.entry_open

    return OpeningMomentumDecision(
        action=action,
        reason=reason,
        universe_size=len(ranking),
        market_return_bps=market_return_bps,
        candidate_symbol=candidate_observation.symbol,
        candidate_return_bps=candidate_return_bps,
        excess_return_bps=excess_return_bps,
        entry_price=entry_price,
        ranking=ranking,
    )


def evaluate_opening_momentum_path_eligible(
    observations: Sequence[OpeningMomentumObservation],
    *,
    path_efficiency_by_symbol: Mapping[str, float],
    minimum_path_efficiency: float,
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Select the strongest candidate whose completed path is eligible."""

    if (
        not math.isfinite(minimum_path_efficiency)
        or not 0 <= minimum_path_efficiency <= 1
    ):
        raise ValueError("minimum_path_efficiency must be in [0, 1]")
    normalized_efficiencies: dict[str, float] = {}
    for raw_symbol, raw_efficiency in path_efficiency_by_symbol.items():
        symbol = raw_symbol.strip().upper()
        if not symbol:
            raise ValueError("path efficiency symbol is required")
        if symbol in normalized_efficiencies:
            raise ValueError(f"duplicate path efficiency: {symbol}")
        efficiency = float(raw_efficiency)
        if not math.isfinite(efficiency) or not 0 <= efficiency <= 1:
            raise ValueError(
                "path efficiencies must contain finite values in [0, 1]"
            )
        normalized_efficiencies[symbol] = efficiency

    params = config or OpeningMomentumConfig()
    by_symbol, ranking = _rank_opening_observations(observations)
    candidate = next(
        (
            item
            for item in ranking
            if normalized_efficiencies.get(item.symbol, -1.0)
            >= minimum_path_efficiency
        ),
        None,
    )
    return _evaluate_ranked_opening_momentum(
        by_symbol=by_symbol,
        ranking=ranking,
        candidate=candidate,
        params=params,
        entry_reason="PATH_ELIGIBLE_OPENING_LEADER",
        missing_candidate_reason="PATH_ELIGIBLE_CANDIDATE_MISSING",
    )


def evaluate_opening_reversal(
    observations: Sequence[OpeningMomentumObservation],
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Select the opening laggard for a causal, long-only reversal shadow."""

    params = config or OpeningMomentumConfig()
    by_symbol: dict[str, OpeningMomentumObservation] = {}
    for item in observations:
        if item.symbol in by_symbol:
            raise ValueError(f"duplicate opening observation: {item.symbol}")
        by_symbol[item.symbol] = item

    ranking = tuple(
        OpeningMomentumRank(
            symbol=item.symbol,
            opening_return_bps=item.opening_return_bps,
        )
        for item in sorted(
            by_symbol.values(),
            key=lambda row: (row.opening_return_bps, row.symbol),
        )
    )
    if len(ranking) < params.minimum_universe_size:
        return OpeningMomentumDecision(
            action="SKIP",
            reason="INSUFFICIENT_UNIVERSE",
            universe_size=len(ranking),
            market_return_bps=None,
            candidate_symbol=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            entry_price=None,
            ranking=ranking,
        )

    market_return_bps = median(
        item.opening_return_bps for item in ranking
    )
    candidate = ranking[0]
    excess_return_bps = (
        candidate.opening_return_bps - market_return_bps
    )
    minimum_lag_bps = params.minimum_excess_return_bps
    maximum_candidate_return_bps = -abs(
        params.minimum_candidate_return_bps
    )
    observation = by_symbol[candidate.symbol]
    action: Literal["ENTER_LONG", "SKIP"] = "ENTER_LONG"
    reason = "OPENING_LAGGARD_REVERSAL"
    if market_return_bps < params.minimum_market_return_bps:
        action = "SKIP"
        reason = "MARKET_FILTER"
    elif (
        candidate.opening_return_bps
        >= maximum_candidate_return_bps
    ):
        action = "SKIP"
        reason = "CANDIDATE_NOT_NEGATIVE"
    elif -excess_return_bps < minimum_lag_bps:
        action = "SKIP"
        reason = "RELATIVE_LOSS_FILTER"
    elif observation.entry_open is None:
        action = "SKIP"
        reason = "ENTRY_BAR_MISSING"

    return OpeningMomentumDecision(
        action=action,
        reason=reason,
        universe_size=len(ranking),
        market_return_bps=market_return_bps,
        candidate_symbol=candidate.symbol,
        candidate_return_bps=candidate.opening_return_bps,
        excess_return_bps=excess_return_bps,
        entry_price=(
            observation.entry_open
            if action == "ENTER_LONG"
            else None
        ),
        ranking=ranking,
    )


def shadow_round_trip_return_bps(
    *,
    entry_price: float,
    exit_price: float,
    config: OpeningMomentumConfig | None = None,
) -> tuple[float, float]:
    """Return raw and cost-adjusted long returns in basis points."""

    params = config or OpeningMomentumConfig()
    if any(
        not math.isfinite(value) or value <= 0
        for value in (entry_price, exit_price)
    ):
        raise ValueError("entry_price and exit_price must be positive")
    gross_return_bps = (exit_price / entry_price - 1) * 10_000
    return gross_return_bps, (
        gross_return_bps - params.round_trip_cost_bps
    )


def opening_path_efficiency(
    *,
    opening_price: float,
    closing_prices: Sequence[float],
) -> float:
    """Measure how directly completed bars travel from open to signal close."""

    closes = tuple(float(value) for value in closing_prices)
    if not math.isfinite(opening_price) or opening_price <= 0:
        raise ValueError("opening_price must be positive")
    if not closes:
        raise ValueError("closing_prices must contain at least one price")
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("closing_prices must be positive")

    previous_price = opening_price
    path_distance = 0.0
    for close in closes:
        path_distance += abs(close - previous_price)
        previous_price = close
    if path_distance <= 0:
        return 0.0
    return min(
        1.0,
        abs(closes[-1] - opening_price) / path_distance,
    )
