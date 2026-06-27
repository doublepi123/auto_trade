"""P283: cross-sectional factor neutralization."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class FactorNeutralizationResult:
    method: str
    neutralized: dict[str, float]
    group_means_before: dict[str, float]
    group_means_after: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def neutralize_factor(factor: dict[str, float], *, method: str = "market_demean", groups: dict[str, str] | None = None, exposures: dict[str, dict[str, float]] | None = None) -> FactorNeutralizationResult:
    vals = _validate_factor(factor)
    group_means_before: dict[str, float] = {}
    if method == "market_demean":
        mu = mean(list(vals.values()))
        out = {k: v - mu for k, v in vals.items()}
    elif method in {"group_demean", "group_zscore"}:
        if groups is None or set(groups) != set(vals):
            raise ValueError("groups keys must match factor keys")
        out = {}
        for group in sorted(set(groups.values())):
            names = [name for name, g in groups.items() if g == group]
            series = [vals[name] for name in names]
            mu = mean(series)
            sigma = std(series)
            group_means_before[group] = mu
            for name in names:
                out[name] = (vals[name] - mu) / sigma if method == "group_zscore" and sigma else vals[name] - mu
    elif method == "residualize":
        if exposures is None or not isinstance(exposures, dict):
            raise ValueError("exposures must be a dict")
        if set(exposures) != set(vals):
            raise ValueError("exposures keys must match factor keys")
        if not all(isinstance(row, dict) for row in exposures.values()):
            raise ValueError("exposure rows must be dicts")
        exposure_names = sorted(next(iter(exposures.values())).keys())
        for row in exposures.values():
            if set(row) != set(exposure_names):
                raise ValueError("all exposure rows must have the same keys")
            for value in row.values():
                _finite(value, "exposure values")
        out = _residualize(vals, exposures, exposure_names)
    else:
        raise ValueError("unknown neutralization method")
    if groups is not None:
        after: dict[str, float] = {}
        for group in sorted(set(groups.values())):
            names = [name for name, g in groups.items() if g == group]
            after[group] = mean([out[name] for name in names])
            group_means_before.setdefault(group, mean([vals[name] for name in names]))
    else:
        after = {"market": mean(list(out.values()))}
        group_means_before = {"market": mean(list(vals.values()))}
    return FactorNeutralizationResult(method, out, group_means_before, after)


def _validate_factor(factor: dict[str, float]) -> dict[str, float]:
    if not isinstance(factor, dict) or len(factor) < 2:
        raise ValueError("factor must contain at least two assets")
    return {str(k): _finite(v, "factor values") for k, v in factor.items()}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite numbers")
    return number


def _residualize(vals: dict[str, float], exposures: dict[str, dict[str, float]], names: list[str]) -> dict[str, float]:
    columns = ["intercept", *names]
    xtx = [[0.0 for _ in columns] for _ in columns]
    xty = [0.0 for _ in columns]
    for asset, y in vals.items():
        row = [1.0, *[_finite(exposures[asset][name], "exposure values") for name in names]]
        for i, xi in enumerate(row):
            xty[i] += xi * y
            for j, xj in enumerate(row):
                xtx[i][j] += xi * xj
    beta = _solve_linear_system(xtx, xty)
    return {asset: y - sum(coef * x for coef, x in zip(beta, [1.0, *[_finite(exposures[asset][name], "exposure values") for name in names]])) for asset, y in vals.items()}


def _solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    aug = [row.copy() + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[col][col] += 1e-9
            pivot = col
        aug[col], aug[pivot] = aug[pivot], aug[col]
        denom = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= denom
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            for j in range(col, n + 1):
                aug[row][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


__all__ = ["FactorNeutralizationResult", "neutralize_factor"]
