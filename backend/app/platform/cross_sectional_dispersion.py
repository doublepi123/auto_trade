"""P289: cross-sectional return dispersion diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class CrossSectionalDispersionResult:
    count: int
    dispersion: dict[str, float]
    opportunity_score: float

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "dispersion": self.dispersion, "opportunity_score": self.opportunity_score}


def cross_sectional_dispersion_report(returns: dict[str, float]) -> CrossSectionalDispersionResult:
    values = _validate_map(returns, "returns")
    ordered = sorted(values.values())
    q1 = _quantile(ordered, 0.25)
    q3 = _quantile(ordered, 0.75)
    mad = mean([abs(value - mean(ordered)) for value in ordered])
    top_bottom = ordered[-1] - ordered[0]
    dispersion = {
        "mean": mean(ordered),
        "std": std(ordered),
        "mad": mad,
        "min": ordered[0],
        "max": ordered[-1],
        "range": top_bottom,
        "iqr": q3 - q1,
        "gini": _gini([abs(value) for value in ordered]),
        "top_bottom_spread": top_bottom,
    }
    return CrossSectionalDispersionResult(len(ordered), dispersion, dispersion["std"] + dispersion["iqr"] + abs(top_bottom))


def _validate_map(values: dict[str, float], name: str) -> dict[str, float]:
    if not isinstance(values, dict) or len(values) < 2:
        raise ValueError(f"{name} must contain at least two assets")
    return {str(key): _finite(value, name) for key, value in values.items()}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} values must be finite numbers")
    return number


def _quantile(sorted_values: list[float], q: float) -> float:
    idx = (len(sorted_values) - 1) * q
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (idx - low)


def _gini(values: list[float]) -> float:
    total = sum(values)
    if total == 0:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    return sum((2 * i - n - 1) * value for i, value in enumerate(ordered, start=1)) / (n * total)


__all__ = ["CrossSectionalDispersionResult", "cross_sectional_dispersion_report"]
