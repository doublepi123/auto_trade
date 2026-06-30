"""P388: Tail diversification analysis.

For a panel of asset returns and a set of portfolio weights, compare the
portfolio's tail loss (worst 5 % periods) to an equally-weighted benchmark.
The tail diversification benefit quantifies how much downside the chosen
portfolio avoids relative to the equal-weight alternative.

Pure Python — no numpy / scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson, validate_series

__all__ = [
    "TailDiversificationResult",
    "tail_diversification_report",
]


@dataclass(frozen=True)
class TailDiversificationResult:
    """Frozen carrier for the tail diversification report.

    Attributes
    ----------
    portfolio_tail_var: Average portfolio loss in the tail window.
    benchmark_tail_var: Average equal-weight benchmark loss in the tail window.
    tail_diversification_benefit: benchmark_tail_loss - portfolio_tail_loss (> 0 is good).
    tail_correlation: Average pairwise correlation in tail periods.
    normal_correlation: Average pairwise correlation over the full sample.
    correlation_breakdown: tail_correlation - normal_correlation (positive when correlations rise in stress).
    """

    portfolio_tail_var: float
    benchmark_tail_var: float
    tail_diversification_benefit: float
    tail_correlation: float
    normal_correlation: float
    correlation_breakdown: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_tail_var": self.portfolio_tail_var,
            "benchmark_tail_var": self.benchmark_tail_var,
            "tail_diversification_benefit": self.tail_diversification_benefit,
            "tail_correlation": self.tail_correlation,
            "normal_correlation": self.normal_correlation,
            "correlation_breakdown": self.correlation_breakdown,
        }


def _portfolio_returns(
    returns_panel: dict[str, list[float]],
    weights: dict[str, float],
) -> list[float]:
    """Compute weighted portfolio return series."""
    n = len(next(iter(returns_panel.values())))
    result: list[float] = []
    for t in range(n):
        port_ret = 0.0
        for name, w in weights.items():
            port_ret += w * returns_panel[name][t]
        result.append(port_ret)
    return result


def _average_pairwise_correlation(panel: dict[str, list[float]]) -> float:
    """Compute the average of all pairwise Pearson correlations."""
    names = list(panel.keys())
    if len(names) < 2:
        return 0.0
    # Check that series are long enough for correlation
    for name in names:
        if len(panel[name]) < 2:
            return 0.0
    corrs: list[float] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            corrs.append(pearson(panel[names[i]], panel[names[j]]))
    return sum(corrs) / len(corrs) if corrs else 0.0


def tail_diversification_report(
    returns_panel: dict[str, list[float]],
    weights: dict[str, float],
    *,
    threshold: float = 0.05,
) -> TailDiversificationResult:
    """Analyse tail diversification of a portfolio vs equal-weight benchmark.

    Parameters
    ----------
    returns_panel: {asset_name: [return_series]} — all same length, ≤50 assets.
    weights: {asset_name: weight} — must sum approximately to 1.0.
    threshold: Tail quantile (default 0.05 = worst 5 %).

    Returns
    -------
    TailDiversificationResult with tail loss metrics and correlation breakdown.

    Raises
    ------
    ValueError: If inputs are invalid.
    """
    if not isinstance(returns_panel, dict) or len(returns_panel) < 2:
        raise ValueError("returns_panel must contain at least two assets")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")

    validated_panel: dict[str, list[float]] = {}
    for name, series in returns_panel.items():
        name_str = str(name)
        validated_panel[name_str] = validate_series(
            series, name=f"returns_panel['{name_str}']", min_len=10
        )

    lengths = {len(v) for v in validated_panel.values()}
    if len(lengths) != 1:
        raise ValueError("all return series must have equal length")
    n = list(lengths)[0]

    if not isinstance(weights, dict) or not weights:
        raise ValueError("weights must be a non-empty dict")
    if set(weights.keys()) != set(validated_panel.keys()):
        raise ValueError("weights keys must match returns_panel keys")

    validated_weights: dict[str, float] = {}
    for name, w in weights.items():
        wf = float(w)
        if not math.isfinite(wf):
            raise ValueError(f"weight for '{name}' must be finite")
        validated_weights[str(name)] = wf

    weight_sum = sum(validated_weights.values())
    if abs(weight_sum - 1.0) > 0.01:
        raise ValueError("weights must sum approximately to 1.0")
    # Normalize
    if weight_sum != 0:
        validated_weights = {k: v / weight_sum for k, v in validated_weights.items()}

    if not 0 < threshold < 1:
        raise ValueError("threshold must be in (0, 1)")

    # Compute portfolio and benchmark returns
    port_returns = _portfolio_returns(validated_panel, validated_weights)

    na = len(validated_panel)
    equal_weights = {name: 1.0 / na for name in validated_panel}
    bench_returns = _portfolio_returns(validated_panel, equal_weights)

    # Determine tail window: we use the portfolio's worst periods to define the tail.
    # Create (return, index) pairs and sort by return ascending.
    indexed = [(port_returns[i], i) for i in range(n)]
    indexed.sort(key=lambda x: x[0])
    tail_count = max(1, int(threshold * n))
    tail_indices = {idx for _, idx in indexed[:tail_count]}

    # Portfolio tail loss
    port_tail_losses = [port_returns[i] for i in tail_indices]
    portfolio_tail_var = -(sum(port_tail_losses) / len(port_tail_losses))

    # Benchmark tail loss (same tail windows as portfolio)
    bench_tail_losses = [bench_returns[i] for i in tail_indices]
    benchmark_tail_var = -(sum(bench_tail_losses) / len(bench_tail_losses))

    tail_diversification_benefit = benchmark_tail_var - portfolio_tail_var

    # Tail correlation: average pairwise correlation using only tail-period data
    tail_panel: dict[str, list[float]] = {}
    for name in validated_panel:
        tail_panel[name] = [validated_panel[name][i] for i in tail_indices]
    tail_correlation = _average_pairwise_correlation(tail_panel)

    # Normal correlation: full sample average pairwise correlation
    normal_correlation = _average_pairwise_correlation(validated_panel)

    correlation_breakdown = tail_correlation - normal_correlation

    return TailDiversificationResult(
        portfolio_tail_var=portfolio_tail_var,
        benchmark_tail_var=benchmark_tail_var,
        tail_diversification_benefit=tail_diversification_benefit,
        tail_correlation=tail_correlation,
        normal_correlation=normal_correlation,
        correlation_breakdown=correlation_breakdown,
    )
