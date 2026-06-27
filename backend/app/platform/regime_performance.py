"""P276: performance sliced by market regime labels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class RegimePerformanceResult:
    regimes: dict[str, dict[str, float | int]]

    def to_dict(self) -> dict[str, Any]:
        return {"regimes": self.regimes}


def regime_performance_report(returns: list[float], regimes: list[str]) -> RegimePerformanceResult:
    values = validate_series(returns, name="returns", min_len=1)
    if not isinstance(regimes, list) or len(regimes) != len(values):
        raise ValueError("regimes must be a list with the same length as returns")
    grouped: dict[str, list[float]] = {}
    for value, regime in zip(values, regimes):
        if not isinstance(regime, str) or not regime:
            raise ValueError("regime labels must be non-empty strings")
        grouped.setdefault(regime, []).append(value)
    total_abs = sum(abs(sum(items)) for items in grouped.values()) or 1.0
    body: dict[str, dict[str, float | int]] = {}
    for regime, items in grouped.items():
        total = sum(items)
        body[regime] = {
            "count": len(items),
            "mean_return": mean(items),
            "volatility": std(items, sample=len(items) > 1),
            "win_rate": sum(1 for value in items if value > 0) / len(items),
            "total_return": total,
            "contribution_share": total / total_abs,
        }
    return RegimePerformanceResult(body)


__all__ = ["RegimePerformanceResult", "regime_performance_report"]
