"""P272: information coefficient time-series diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class ICDiagnosticsResult:
    mean_ic: float
    std_ic: float
    positive_ratio: float
    t_like_score: float
    max_cumulative_drawdown: float
    stability: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean_ic": self.mean_ic,
            "std_ic": self.std_ic,
            "positive_ratio": self.positive_ratio,
            "t_like_score": self.t_like_score,
            "max_cumulative_drawdown": self.max_cumulative_drawdown,
            "stability": self.stability,
        }


def ic_diagnostics_report(ic_series: list[float]) -> ICDiagnosticsResult:
    values = validate_series(ic_series, name="ic_series", min_len=2)
    mu = mean(values)
    sigma = std(values, sample=True)
    t_like = 0.0 if sigma == 0 else mu / sigma * math.sqrt(len(values))
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    score = abs(t_like)
    stability = "strong" if score >= 2.0 else "moderate" if score >= 1.0 else "weak"
    return ICDiagnosticsResult(mu, sigma, sum(1 for value in values if value > 0) / len(values), t_like, max_dd, stability)


__all__ = ["ICDiagnosticsResult", "ic_diagnostics_report"]
