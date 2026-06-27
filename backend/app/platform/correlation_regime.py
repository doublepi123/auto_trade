"""P294: cross-asset correlation regime diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson, validate_series


@dataclass(frozen=True)
class CorrelationRegimeResult:
    matrix: dict[str, dict[str, float]]
    average_correlation: float
    largest_eigenvalue: float
    concentration_ratio: float
    regime: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def correlation_regime_report(returns_panel: dict[str, list[float]]) -> CorrelationRegimeResult:
    panel = _validate_panel(returns_panel)
    names = list(panel)
    matrix: dict[str, dict[str, float]] = {name: {} for name in names}
    pairs: list[float] = []
    for left in names:
        for right in names:
            corr = 1.0 if left == right else pearson(panel[left], panel[right])
            matrix[left][right] = corr
            if left < right:
                pairs.append(corr)
    avg_corr = sum(pairs) / len(pairs) if pairs else 0.0
    largest = _largest_eigenvalue([[matrix[left][right] for right in names] for left in names])
    concentration = largest / len(names)
    regime = "stress" if avg_corr > 0.75 or concentration > 0.75 else "concentrated" if avg_corr > 0.4 else "diversified" if avg_corr < 0.1 else "normal"
    return CorrelationRegimeResult(matrix, avg_corr, largest, concentration, regime)


def _validate_panel(panel: dict[str, list[float]]) -> dict[str, list[float]]:
    if not isinstance(panel, dict) or len(panel) < 2:
        raise ValueError("returns_panel must contain at least two assets")
    if len(panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    out = {str(name): validate_series(series, name=str(name), min_len=2) for name, series in panel.items()}
    lengths = {len(series) for series in out.values()}
    if len(lengths) != 1:
        raise ValueError("return series must have equal length")
    return out


def _largest_eigenvalue(matrix: list[list[float]]) -> float:
    n = len(matrix)
    vector = [float(i + 1) for i in range(n)]
    norm0 = sum(abs(value) for value in vector) or 1.0
    vector = [value / norm0 for value in vector]
    for _ in range(25):
        nxt = [sum(matrix[i][j] * vector[j] for j in range(n)) for i in range(n)]
        norm = sum(abs(value) for value in nxt) or 1.0
        vector = [value / norm for value in nxt]
    numerator = sum(vector[i] * sum(matrix[i][j] * vector[j] for j in range(n)) for i in range(n))
    denominator = sum(value * value for value in vector) or 1.0
    return numerator / denominator


__all__ = ["CorrelationRegimeResult", "correlation_regime_report"]
