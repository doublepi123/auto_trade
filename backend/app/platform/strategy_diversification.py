"""P277: multi-strategy correlation and diversification diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, pearson, validate_series


@dataclass(frozen=True)
class StrategyDiversificationResult:
    correlation_matrix: dict[str, dict[str, float]]
    average_pairwise_correlation: float
    diversification_score: float
    redundant_pairs: list[list[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_matrix": self.correlation_matrix,
            "average_pairwise_correlation": self.average_pairwise_correlation,
            "diversification_score": self.diversification_score,
            "redundant_pairs": self.redundant_pairs,
        }


def strategy_diversification_report(strategies: dict[str, list[float]], *, redundancy_threshold: float = 0.9) -> StrategyDiversificationResult:
    if not isinstance(strategies, dict) or len(strategies) < 2:
        raise ValueError("strategies must contain at least two return series")
    if isinstance(redundancy_threshold, bool) or not isinstance(redundancy_threshold, (int, float)) or not 0 <= float(redundancy_threshold) <= 1:
        raise ValueError("redundancy_threshold must be in [0, 1]")
    checked = {name: validate_series(series, name=f"strategies[{name}]", min_len=2) for name, series in strategies.items()}
    lengths = {len(series) for series in checked.values()}
    if len(lengths) != 1:
        raise ValueError("strategy return series must have equal length")
    if any(len(set(series)) < 2 for series in checked.values()):
        raise ValueError("strategy return series must not be constant")
    names = list(checked)
    matrix: dict[str, dict[str, float]] = {name: {} for name in names}
    pair_corrs: list[float] = []
    abs_corrs: list[float] = []
    redundant: list[list[str]] = []
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            corr = 1.0 if i == j else pearson(checked[a], checked[b])
            matrix[a][b] = corr
            if i < j:
                pair_corrs.append(corr)
                abs_corrs.append(abs(corr))
                if corr >= float(redundancy_threshold):
                    redundant.append([a, b])
    return StrategyDiversificationResult(matrix, mean(pair_corrs), max(0.0, min(1.0, 1.0 - mean(abs_corrs))), redundant)


__all__ = ["StrategyDiversificationResult", "strategy_diversification_report"]
