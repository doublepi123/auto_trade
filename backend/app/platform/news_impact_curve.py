"""P372: News Impact Curve — conditional variance response to shocks.

Fits a simplified symmetric and asymmetric news-impact model to return data,
characterising how conditional variance responds to positive and negative
return shocks (the "news impact curve"). Inspired by EGARCH / GJR-GARCH
asymmetry.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["NewsImpactCurveResult", "news_impact_curve_report"]


# Number of points to sample along the shock axis for the curve
_CURVE_POINTS = 21


@dataclass(frozen=True)
class NewsImpactCurveResult:
    """Frozen aggregate result of :func:`news_impact_curve_report`.

    * ``symmetric_curve`` — list of {shock, conditional_var} for symmetric model.
    * ``asymmetric_curve`` — list of {shock, conditional_var} for asymmetric model.
    * ``leverage_effect`` — gamma coefficient (asymmetry parameter).
    * ``asymmetry_ratio`` — ratio of conditional_var for negative vs positive
      shocks of equal magnitude.
    """

    symmetric_curve: list[dict[str, Any]]
    asymmetric_curve: list[dict[str, Any]]
    leverage_effect: float
    asymmetry_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symmetric_curve": self.symmetric_curve,
            "asymmetric_curve": self.asymmetric_curve,
            "leverage_effect": self.leverage_effect,
            "asymmetry_ratio": self.asymmetry_ratio,
        }


def _validate_returns(returns: list[float]) -> list[float]:
    """Validate return series and return cleaned copy."""
    if not isinstance(returns, list):
        raise ValueError("returns must be a list")
    if len(returns) < 5:
        raise ValueError("returns must contain at least 5 values")
    cleaned: list[float] = []
    for i, r in enumerate(returns):
        if isinstance(r, bool) or not isinstance(r, (int, float)):
            raise ValueError(f"returns[{i}] must be a number")
        val = float(r)
        if not math.isfinite(val):
            raise ValueError(f"returns[{i}] must be a finite number")
        cleaned.append(val)
    return cleaned


def _fit_symmetric_garch(returns: list[float], lags: int) -> tuple[float, float]:
    """Fit simplified symmetric ARCH(1)-like model.

    h_t = omega + alpha * eps_{t-1}^2

    Uses OLS on squared residuals with an intercept.

    Returns (omega, alpha).
    """
    n = len(returns)

    # Compute demeaned returns (shocks)
    # We use raw returns as innovation proxies
    # Squared returns as proxy for conditional variance
    r_sq = [r * r for r in returns]

    # OLS: r_sq[t] = omega + alpha * r_sq[t-1]
    y = r_sq[1:]  # dependent
    x = r_sq[:-1]  # lagged squared returns

    mean_y = sum(y) / len(y)
    mean_x = sum(x) / len(x)

    numerator = 0.0
    denominator = 0.0
    for xi, yi in zip(x, y):
        dx = xi - mean_x
        dy = yi - mean_y
        numerator += dx * dy
        denominator += dx * dx

    if denominator < 1e-15:
        alpha = 0.0
    else:
        alpha = numerator / denominator
        alpha = max(0.0, min(1.0, alpha))  # constrain to reasonable range

    omega = mean_y - alpha * mean_x
    omega = max(0.0, omega)  # variance must be non-negative

    return omega, alpha


def _fit_asymmetric_garch(returns: list[float], lags: int) -> tuple[float, float, float]:
    """Fit simplified asymmetric model: h_t = omega + alpha*eps^2 + gamma*eps^2*I(eps<0).

    Uses OLS with two regressors: eps_{t-1}^2 and eps_{t-1}^2 * I(eps_{t-1} < 0).

    Returns (omega, alpha, gamma).
    """
    r_sq = [r * r for r in returns]

    # Build regressors
    y = r_sq[1:]  # h_t proxy
    x1 = r_sq[:-1]  # eps_{t-1}^2
    returns_lag = returns[:-1]
    x2 = [x1[i] if returns_lag[i] < 0 else 0.0 for i in range(len(returns_lag))]
    # x2: eps^2 * I(eps<0)

    # Simple double OLS (not multivariate — we'll do sequential estimation)
    # Step 1: regress y on x1 to get alpha_initial
    n = len(y)
    mean_y = sum(y) / n

    # Alpha: OLS regression of squared returns on lagged squared returns
    mean_x1 = sum(x1) / n
    num_a = 0.0
    den_a = 0.0
    for yi, x1i in zip(y, x1):
        dx1 = x1i - mean_x1
        dy = yi - mean_y
        num_a += dx1 * dy
        den_a += dx1 * dx1

    alpha = num_a / den_a if den_a > 1e-15 else 0.01
    alpha = max(0.0, min(0.5, alpha))

    # Omega: OLS intercept (consistent with _fit_symmetric_garch)
    omega = mean_y - alpha * mean_x1
    omega = max(0.0, omega)

    # Compute residuals after symmetric model
    residuals = [yi - (omega + alpha * x1i) for yi, x1i in zip(y, x1)]

    # Gamma: regress residuals on x2
    mean_x2 = sum(x2) / n if n > 0 else 0
    num_g = 0.0
    den_g = 0.0
    for ri, x2i in zip(residuals, x2):
        dx2 = x2i - mean_x2
        num_g += dx2 * ri
        den_g += dx2 * dx2

    gamma = num_g / den_g if den_g > 1e-15 else 0.0
    # Constrain gamma: typically positive (negative shocks increase variance more)
    gamma = max(-0.3, min(0.3, gamma))

    return omega, alpha, gamma


def news_impact_curve_report(
    returns: list[float],
    *,
    lags: int = 1,
) -> NewsImpactCurveResult:
    """Compute news impact curve from return data.

    Fits symmetric (ARCH-style) and asymmetric (leverage-effect) models and
    plots the conditional variance response to shocks.

    Parameters
    ----------
    returns : list[float]
        Time-series of asset returns.
    lags : int
        Number of lags (currently only lags=1 is supported for the simple model).

    Returns
    -------
    NewsImpactCurveResult
    """
    cleaned = _validate_returns(returns)

    # Fit symmetric model
    omega_sym, alpha_sym = _fit_symmetric_garch(cleaned, lags)

    # Fit asymmetric model
    omega_asym, alpha_asym, gamma_asym = _fit_asymmetric_garch(cleaned, lags)

    # Compute std for shock range
    n = len(cleaned)
    mean_r = sum(cleaned) / n
    variance = sum((r - mean_r) ** 2 for r in cleaned) / (n - 1)
    std_dev = math.sqrt(max(0.0, variance))

    # Generate curve points from -3σ to +3σ
    symmetric_curve: list[dict[str, Any]] = []
    asymmetric_curve: list[dict[str, Any]] = []

    for i in range(_CURVE_POINTS):
        shock = -3.0 * std_dev + (6.0 * std_dev) * i / (_CURVE_POINTS - 1)

        # Symmetric: h = omega + alpha * eps^2
        h_sym = omega_sym + alpha_sym * shock * shock
        h_sym = max(0.0, h_sym)

        symmetric_curve.append({
            "shock": shock,
            "conditional_var": h_sym,
        })

        # Asymmetric: h = omega + alpha*eps^2 + gamma*eps^2*I(eps<0)
        indicator_neg = 1.0 if shock < 0 else 0.0
        h_asym = omega_asym + alpha_asym * shock * shock + gamma_asym * shock * shock * indicator_neg
        h_asym = max(0.0, h_asym)

        asymmetric_curve.append({
            "shock": shock,
            "conditional_var": h_asym,
        })

    leverage_effect = gamma_asym

    # Asymmetry ratio: var(-1σ) / var(+1σ)
    h_neg = omega_asym + alpha_asym * std_dev * std_dev + gamma_asym * std_dev * std_dev
    h_pos = omega_asym + alpha_asym * std_dev * std_dev

    if h_pos > 1e-15:
        asymmetry_ratio = h_neg / h_pos
    else:
        asymmetry_ratio = 1.0

    return NewsImpactCurveResult(
        symmetric_curve=symmetric_curve,
        asymmetric_curve=asymmetric_curve,
        leverage_effect=leverage_effect,
        asymmetry_ratio=asymmetry_ratio,
    )
