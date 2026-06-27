"""P278: bootstrap confidence and rolling-stability diagnostics."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class BacktestConfidenceResult:
    mean_return: float
    ci_low: float
    ci_high: float
    rolling_sharpe_std: float
    fragility_score: float

    def to_dict(self) -> dict[str, Any]:
        return {"mean_return": self.mean_return, "ci_low": self.ci_low, "ci_high": self.ci_high, "rolling_sharpe_std": self.rolling_sharpe_std, "fragility_score": self.fragility_score}


def _sharpe(values: list[float]) -> float:
    sigma = std(values, sample=True)
    return 0.0 if sigma == 0 else mean(values) / sigma


def backtest_confidence_report(returns: list[float], *, n_bootstrap: int = 1000, seed: int | None = None, window: int = 20) -> BacktestConfidenceResult:
    values = validate_series(returns, name="returns", min_len=2)
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, int) or n_bootstrap < 1 or n_bootstrap > 10000:
        raise ValueError("n_bootstrap must be an int in [1, 10000]")
    if isinstance(window, bool) or not isinstance(window, int) or window < 2 or window > len(values):
        raise ValueError("window must be in [2, len(returns)]")
    rng = random.Random(seed)
    boot = sorted(mean([values[rng.randrange(len(values))] for _ in values]) for _ in range(n_bootstrap))
    low = boot[int(0.025 * (len(boot) - 1))]
    high = boot[int(0.975 * (len(boot) - 1))]
    rolling = [_sharpe(values[i : i + window]) for i in range(0, len(values) - window + 1)]
    sigma = std(values)
    downside = abs(min(values))
    fragility = 0.0 if sigma == 0 else downside / (downside + sigma)
    return BacktestConfidenceResult(mean(values), low, high, std(rolling), fragility)


__all__ = ["BacktestConfidenceResult", "backtest_confidence_report"]
