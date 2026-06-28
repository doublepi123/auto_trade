"""P337: Regime-Conditional Cointegration — hedge ratios across market regimes.

For pairs/spread trading, the hedge ratio may differ dramatically between
bull, bear, and sideways regimes. This module computes per-regime OLS hedge
ratios, residual half-lives, and a stability score that penalizes hedge-ratio
instability.

Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegimeCointegrationResult:
    """Regime-conditional cointegration diagnostics."""
    per_regime: dict[str, dict[str, object]]
    stability_score: float | None
    breakdown_regimes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "per_regime": self.per_regime,
            "stability_score": self.stability_score,
            "breakdown_regimes": self.breakdown_regimes,
        }


def _validate_inputs(y: list[float], x: list[float], regime_labels: list[str]) -> None:
    if not y or not x or not regime_labels:
        raise ValueError("y, x, and regime_labels must be non-empty")
    if len(y) != len(x) or len(y) != len(regime_labels):
        raise ValueError("y, x, and regime_labels must have equal length")
    for v in y:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise ValueError(f"y contains non-finite value: {v}")
    for v in x:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise ValueError(f"x contains non-finite value: {v}")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _ols_slope(y: list[float], x: list[float]) -> float:
    """Simple OLS slope: y ~ x (no intercept)."""
    n = len(y)
    if n < 2:
        return 0.0
    num = sum(y[i] * x[i] for i in range(n))
    den = sum(x[i] * x[i] for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def _autocorr_lag1(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    num = sum((values[t] - m) * (values[t + 1] - m) for t in range(n - 1))
    den = sum((values[t] - m) ** 2 for t in range(n))
    if den == 0:
        return 0.0
    return num / den


def _half_life(rho: float) -> float:
    """Mean-reversion half-life from lag-1 autocorrelation of residuals."""
    if rho >= 1.0 or rho <= -1.0:
        return float("inf")
    if rho <= 0:
        return 1.0  # Very fast reversion
    return -math.log(2.0) / math.log(rho)


def regime_cointegration_report(
    y: list[float],
    x: list[float],
    regime_labels: list[str],
    *,
    min_regime_samples: int = 10,
) -> RegimeCointegrationResult:
    """Compute per-regime OLS hedge ratios and residual mean-reversion diagnostics.

    Args:
        y: Dependent variable series (e.g., spread constituent A).
        x: Independent variable series (e.g., spread constituent B).
        regime_labels: Per-observation regime label (e.g., "bull", "bear").
        min_regime_samples: Minimum observations for a regime to be considered sufficient.

    Returns:
        RegimeCointegrationResult with per-regime hedge_ratio, half_life,
        residual_autocorr, n_samples, sufficient, plus stability_score and
        breakdown_regimes.
    """
    _validate_inputs(y, x, regime_labels)

    # Group indices by regime
    regime_indices: dict[str, list[int]] = {}
    for idx, label in enumerate(regime_labels):
        label_str = str(label)
        if label_str not in regime_indices:
            regime_indices[label_str] = []
        regime_indices[label_str].append(idx)

    per_regime: dict[str, dict[str, object]] = {}
    hedge_ratios: list[float] = []
    breakdown_regimes: list[str] = []

    for regime, indices in sorted(regime_indices.items()):
        n_samples = len(indices)
        y_r = [y[i] for i in indices]
        x_r = [x[i] for i in indices]

        sufficient = n_samples >= min_regime_samples

        if sufficient:
            hedge_ratio = _ols_slope(y_r, x_r)
            # Compute residuals: y - hedge_ratio * x
            residuals = [y_r[i] - hedge_ratio * x_r[i] for i in range(n_samples)]
            residual_autocorr = _autocorr_lag1(residuals)
            half_life = _half_life(residual_autocorr)
            hedge_ratios.append(hedge_ratio)
        else:
            hedge_ratio = 0.0
            residual_autocorr = 0.0
            half_life = float("inf")
            breakdown_regimes.append(regime)

        per_regime[regime] = {
            "hedge_ratio": hedge_ratio,
            "half_life": half_life,
            "residual_autocorr": residual_autocorr,
            "n_samples": n_samples,
            "sufficient": sufficient,
        }

    # Stability score: 1 / CV of hedge_ratios (higher = more stable)
    if len(hedge_ratios) >= 2:
        hr_mean = _mean(hedge_ratios)
        hr_std = math.sqrt(sum((hr - hr_mean) ** 2 for hr in hedge_ratios) / len(hedge_ratios))
        if hr_std > 0 and hr_mean != 0:
            cv = hr_std / abs(hr_mean)
            stability_score = 1.0 / cv
        else:
            stability_score = float("inf")
    else:
        stability_score = None

    return RegimeCointegrationResult(
        per_regime=per_regime,
        stability_score=stability_score,
        breakdown_regimes=breakdown_regimes,
    )
