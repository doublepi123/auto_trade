"""P302: bootstrap strategy significance diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class BootstrapStrategySignificanceResult:
    observed_sharpe: float
    bootstrap_mean: float
    bootstrap_std: float
    p_value: float
    ci_lower: float
    ci_upper: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def bootstrap_strategy_significance_report(returns: list[float], *, n_bootstrap: int = 1000, seed: int = 42) -> BootstrapStrategySignificanceResult:
    rets = validate_series(returns, name="returns", min_len=5)
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, int) or n_bootstrap < 10:
        raise ValueError("n_bootstrap must be an int >= 10")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative int")
    observed = _sharpe(rets)
    mu = mean(rets)
    demeaned = [r - mu for r in rets]
    rng = random.Random(seed)
    sharpes: list[float] = []
    n = len(rets)
    for _ in range(n_bootstrap):
        sample = [demeaned[rng.randrange(n)] for _ in range(n)]
        sharpes.append(_sharpe(sample))
    sharpes.sort()
    count_ge = sum(1 for s in sharpes if s >= observed)
    p_value = count_ge / len(sharpes)
    bmean = mean(sharpes)
    bstd = std(sharpes, sample=True) if len(sharpes) > 1 else 0.0
    lower = sharpes[int(0.025 * len(sharpes))]
    upper = sharpes[min(len(sharpes) - 1, int(0.975 * len(sharpes)))]
    return BootstrapStrategySignificanceResult(observed, bmean, bstd, p_value, lower, upper)


def _sharpe(values: list[float]) -> float:
    sigma = std(values, sample=True) if len(values) > 1 else 0.0
    return 0.0 if sigma == 0 else mean(values) / sigma


__all__ = ["BootstrapStrategySignificanceResult", "bootstrap_strategy_significance_report"]
