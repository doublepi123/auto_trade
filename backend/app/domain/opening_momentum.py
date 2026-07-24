from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from statistics import median
from typing import Literal, Sequence


ALGORITHM_VERSION = "cross-sectional-opening-momentum-v2-causal-entry"


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

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * (
            self.one_side_fee_rate * 10_000
            + self.one_side_slippage_bps
        )

    def version_hash(self) -> str:
        payload = {
            "algorithm_version": ALGORITHM_VERSION,
            **asdict(self),
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


def evaluate_opening_momentum(
    observations: Sequence[OpeningMomentumObservation],
    config: OpeningMomentumConfig | None = None,
) -> OpeningMomentumDecision:
    """Rank completed opening returns and apply the frozen entry gates."""

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
            key=lambda row: (-row.opening_return_bps, row.symbol),
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
    observation = by_symbol[candidate.symbol]
    action: Literal["ENTER_LONG", "SKIP"] = "ENTER_LONG"
    reason = "OPENING_LEADER"
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
