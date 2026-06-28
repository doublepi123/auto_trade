"""P358: Active Return Attribution & Factor Decomposition.

Decompose the active return (portfolio returns minus benchmark returns) into:
- Active return statistics (mean, std, t-stat, information ratio).
- Optional factor-based decomposition: factor contribution and residual alpha.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "ActiveAttributionResult",
    "active_attribution_report",
]


def _validate_returns(values: Sequence[float], label: str) -> list[float]:
    """Validate and coerce a list of finite return values."""
    if not isinstance(values, list):
        values = list(values)  # type: ignore[arg-type]
    if not values:
        raise ValueError(f"{label} must be non-empty")
    result: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{label} entries must be finite numbers")
        number = float(v)
        if not math.isfinite(number):
            raise ValueError(f"{label} entries must be finite numbers")
        result.append(number)
    return result


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float], ddof: int = 1) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    var = sum((x - m) ** 2 for x in values) / (n - ddof)
    return math.sqrt(var) if var > 0 else 0.0


@dataclass(frozen=True)
class ActiveAttributionResult:
    """Frozen carrier for active return attribution.

    Attributes
    ----------
    active_return_stats: {mean, std, t_stat, information_ratio}.
    factor_contribution: Per-factor contribution to active return (or None).
    residual_alpha: Active return minus factor contribution per period (or None).
    summary: Human-readable summary string.
    """

    active_return_stats: dict[str, float]
    factor_contribution: dict[str, float] | None
    residual_alpha: list[float] | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_return_stats": self.active_return_stats,
            "factor_contribution": self.factor_contribution,
            "residual_alpha": self.residual_alpha,
            "summary": self.summary,
        }


def active_attribution_report(
    returns: list[float],
    benchmark: list[float],
    *,
    factor_exposures: dict[str, list[float]] | None = None,
    factor_returns: dict[str, list[float]] | None = None,
) -> ActiveAttributionResult:
    """Decompose active return into factor contribution and residual alpha.

    Parameters
    ----------
    returns: Portfolio return series.
    benchmark: Benchmark return series (same length as returns).
    factor_exposures: {factor_name: [exposure per period]} (optional).
    factor_returns: {factor_name: [factor_return per period]} (optional).

    Returns a frozen result with active return statistics and optional
    factor decomposition.

    Raises ValueError/TypeError on invalid input.
    """
    returns_validated = _validate_returns(returns, "returns")
    benchmark_validated = _validate_returns(benchmark, "benchmark")
    n = len(returns_validated)
    if len(benchmark_validated) != n:
        raise ValueError(
            f"returns length {n} != benchmark length {len(benchmark_validated)}"
        )

    # Active return series.
    active = [returns_validated[t] - benchmark_validated[t] for t in range(n)]

    # Statistics.
    active_mean = _mean(active)
    active_std = _std(active)
    t_stat = active_mean / (active_std / math.sqrt(n)) if active_std > 0 else 0.0

    # Information ratio: mean(active) / std(active), annualised by convention
    # (not annualised here since we don't know frequency).
    ir = active_mean / active_std if active_std > 0 else 0.0

    # Optional factor decomposition.
    factor_contribution: dict[str, float] | None = None
    residual_alpha: list[float] | None = None
    summary_parts: list[str] = []

    has_factors = factor_exposures is not None and factor_returns is not None
    if has_factors:
        if not isinstance(factor_exposures, dict) or not factor_exposures:
            raise ValueError("factor_exposures must be a non-empty dict")
        if not isinstance(factor_returns, dict) or not factor_returns:
            raise ValueError("factor_returns must be a non-empty dict")

        factor_contribution = {}
        residual_alpha = [0.0] * n
        # Start with active return; subtract each factor's contribution.
        for t in range(n):
            residual_alpha[t] = active[t]

        for factor_name in factor_exposures:
            if factor_name not in factor_returns:
                raise ValueError(
                    f"factor '{factor_name}' has exposures but no returns"
                )
            expo = factor_exposures[factor_name]
            fret = factor_returns[factor_name]
            if not isinstance(expo, list) or not isinstance(fret, list):
                raise ValueError(
                    f"factor '{factor_name}' exposures and returns must be lists"
                )
            expo_validated = _validate_returns(expo, f"factor_exposures['{factor_name}']")
            fret_validated = _validate_returns(fret, f"factor_returns['{factor_name}']")
            if len(expo_validated) != n:
                raise ValueError(
                    f"factor_exposures['{factor_name}'] length "
                    f"{len(expo_validated)} != {n}"
                )
            if len(fret_validated) != n:
                raise ValueError(
                    f"factor_returns['{factor_name}'] length "
                    f"{len(fret_validated)} != {n}"
                )

            # Factor contribution per period.
            contrib = [expo_validated[t] * fret_validated[t] for t in range(n)]
            factor_contribution[factor_name] = _mean(contrib)

            # Subtract from residual.
            for t in range(n):
                residual_alpha[t] -= contrib[t]

        summary_parts.append(
            f"Factors: {', '.join(f'{k}={v:.6f}' for k, v in factor_contribution.items())}"
        )

    summary_parts.append(
        f"Active: mean={active_mean:.6f}, t={t_stat:.2f}, IR={ir:.4f}"
    )
    summary = "; ".join(summary_parts)

    return ActiveAttributionResult(
        active_return_stats={
            "mean": active_mean,
            "std": active_std,
            "t_stat": t_stat,
            "information_ratio": ir,
        },
        factor_contribution=factor_contribution,
        residual_alpha=residual_alpha,
        summary=summary,
    )
