"""P275: strategy trade quality and SQN diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class StrategyQualityResult:
    n_trades: int
    expectancy: float
    trade_std: float
    sqn: float
    win_rate: float
    payoff_ratio: float
    sample_confidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "expectancy": self.expectancy,
            "trade_std": self.trade_std,
            "sqn": None if not math.isfinite(self.sqn) else self.sqn,
            "win_rate": self.win_rate,
            "payoff_ratio": None if not math.isfinite(self.payoff_ratio) else self.payoff_ratio,
            "sample_confidence": self.sample_confidence,
        }


def strategy_quality_report(trades: list[float]) -> StrategyQualityResult:
    values = validate_series(trades, name="trades", min_len=1)
    expectancy = mean(values)
    sigma = std(values, sample=True)
    sqn = math.inf if sigma == 0 and expectancy > 0 else 0.0 if sigma == 0 else math.sqrt(len(values)) * expectancy / sigma
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    payoff = math.inf if wins and not losses else 0.0 if not wins else mean(wins) / abs(mean(losses))
    confidence = "high" if len(values) >= 30 else "medium" if len(values) >= 10 else "low"
    return StrategyQualityResult(len(values), expectancy, sigma, sqn, len(wins) / len(values), payoff, confidence)


__all__ = ["StrategyQualityResult", "strategy_quality_report"]
