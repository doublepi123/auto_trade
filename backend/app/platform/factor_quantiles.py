"""P271: alphalens-style factor quantile return diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, validate_pair


@dataclass(frozen=True)
class FactorQuantileResult:
    quantiles: list[dict[str, float | int]]
    top_bottom_spread: float
    monotonicity_score: float

    def to_dict(self) -> dict[str, Any]:
        return {"quantiles": self.quantiles, "top_bottom_spread": self.top_bottom_spread, "monotonicity_score": self.monotonicity_score}


def factor_quantile_report(factor: list[float], forward_returns: list[float], *, n_quantiles: int = 5) -> FactorQuantileResult:
    f, r = validate_pair(factor, forward_returns, x_name="factor", y_name="forward_returns")
    if isinstance(n_quantiles, bool) or not isinstance(n_quantiles, int) or n_quantiles < 2 or n_quantiles > len(f):
        raise ValueError("n_quantiles must be between 2 and input length")
    ordered = sorted(zip(f, r), key=lambda item: (item[0], item[1]))
    buckets: list[dict[str, float | int]] = []
    means: list[float] = []
    for i in range(n_quantiles):
        start = i * len(ordered) // n_quantiles
        end = (i + 1) * len(ordered) // n_quantiles
        vals = [ret for _, ret in ordered[start:end]]
        avg = mean(vals)
        means.append(avg)
        buckets.append({"quantile": i + 1, "count": len(vals), "mean_return": avg})
    comparisons = max(1, len(means) - 1)
    monotonic = sum(1 for a, b in zip(means, means[1:]) if b >= a) / comparisons
    return FactorQuantileResult(quantiles=buckets, top_bottom_spread=means[-1] - means[0], monotonicity_score=monotonic)


__all__ = ["FactorQuantileResult", "factor_quantile_report"]
