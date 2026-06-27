"""P303: dynamic factor exposure diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class DynamicFactorExposureResult:
    betas: dict[str, list[float]]
    drift_flags: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {"betas": self.betas, "drift_flags": self.drift_flags}


def dynamic_factor_exposure_report(strategy_returns: list[float], factor_panel: dict[str, list[float]], *, window: int = 20) -> DynamicFactorExposureResult:
    strat = validate_series(strategy_returns, name="strategy_returns", min_len=2)
    if isinstance(window, bool) or not isinstance(window, int) or window < 2 or window > len(strat):
        raise ValueError("window must be an int in [2, len(strategy_returns)]")
    if not isinstance(factor_panel, dict) or not factor_panel:
        raise ValueError("factor_panel must be non-empty")
    panel = {str(name): validate_series(series, name=str(name), min_len=len(strat)) for name, series in factor_panel.items()}
    if any(len(series) != len(strat) for series in panel.values()):
        raise ValueError("factor panel series must match strategy_returns length")
    if len(panel) > 50:
        raise ValueError("factor_panel must contain at most 50 factors")
    betas: dict[str, list[float]] = {}
    drift: dict[str, bool] = {}
    for name, factor in panel.items():
        series = [_rolling_beta(strat[max(0, i - window + 1): i + 1], factor[max(0, i - window + 1): i + 1]) for i in range(len(strat))]
        betas[name] = series
        first_half = series[: len(series) // 2]
        second_half = series[len(series) // 2:]
        fm = mean(first_half) if first_half else 0.0
        sm = mean(second_half) if second_half else 0.0
        fs = std(first_half, sample=True) if len(first_half) > 1 else 0.0
        drift[name] = fs > 0 and abs(sm - fm) > 2 * fs
    return DynamicFactorExposureResult(betas, drift)


def _rolling_beta(y: list[float], x: list[float]) -> float:
    if len(x) < 2:
        return 0.0
    my = mean(y)
    mx = mean(x)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    return num / den if den != 0 else 0.0


__all__ = ["DynamicFactorExposureResult", "dynamic_factor_exposure_report"]
