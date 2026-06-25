"""P232: Extreme Value Theory — Peaks-Over-Threshold & Tail Index.

Tail-risk estimation beyond the empirical quantile, the way the FRB/Basel
FRTB and McNeil-Frey stress-testing pipelines do it. Given a loss series we
fit the **Generalized Pareto Distribution (GPD)** to the exceedances over a
high threshold; the GPD shape parameter ``ξ`` (the tail index) governs how
heavy the tail is, and from it we extrapolate VaR/CVaR at confidence levels
beyond the sample size.

* **peaks_over_threshold** — pick the ``u``-threshold exceedances (losses
  beyond ``u``); returns the exceedances and the count.
* **gpd_fit** — the Davison-Smith (1990) method-of-moments estimator for the
  GPD shape ``ξ`` and scale ``σ``. With sample mean ``m`` and variance ``s²``
  of the exceedances, ``ξ = ½(m²/s² − 1)`` and ``σ = ½m(m²/s² + 1)``. Closed
  form, deterministic, no optimizer.
* **evt_var / evt_cvar** — GPD-based tail VaR/CVaR at arbitrary ``α``:
  ``VaR_α = u + (σ/ξ)[((1−α)/N_u·n)^{−ξ} − 1]``.

Reference: McNeil & Frey (2000), Embrechts et al. (1997) "Modelling
Extremal Events", Davison & Smith (1990). Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "GpdfitResult",
    "EvtResult",
    "peaks_over_threshold",
    "gpd_fit",
    "evt_var",
    "evt_cvar",
    "evt_report",
]


def peaks_over_threshold(losses: Sequence[float], threshold: float) -> list[float]:
    """Return exceedances (loss − threshold) for losses exceeding ``threshold``."""
    if not losses:
        raise ValueError("losses must be non-empty")
    return [l - threshold for l in losses if l > threshold]


def gpd_fit(exceedances: Sequence[float]) -> "GpdfitResult":
    """Method-of-moments GPD fit.

    For a GPD with shape ``ξ`` and scale ``σ``, the mean is ``σ/(1−ξ)``
    (for ``ξ<1``) and the variance is ``σ²/((1−ξ)²(1−2ξ))`` (for ``ξ<½``),
    giving ``mean²/var = 1 − 2ξ``. Solving,

        ξ = ½(1 − mean²/var),   σ = ½·mean·(1 + mean²/var).

    This is the standard MoM estimator (Davison & Smith 1990 / Hosking
    & Wallis 1987). Requires ≥2 exceedances with nonzero variance; otherwise
    ``ξ=0`` (exponential, light tail) and ``σ=mean``.
    """
    n = len(exceedances)
    if n < 2:
        raise ValueError("need >=2 exceedances")
    m = sum(exceedances) / n
    var = sum((x - m) ** 2 for x in exceedances) / (n - 1)
    if var <= 0:
        return GpdfitResult(xi=0.0, sigma=m, n_exceedances=n, mean=m, variance=0.0)
    ratio = (m * m) / var  # = 1 − 2ξ  for a true GPD
    xi = 0.5 * (1.0 - ratio)
    sigma = 0.5 * m * (1.0 + ratio)
    if sigma <= 0:
        sigma = abs(m) if m != 0 else 1e-9
        xi = 0.0
    return GpdfitResult(xi=xi, sigma=sigma, n_exceedances=n, mean=m, variance=var)


@dataclass(frozen=True)
class GpdfitResult:
    xi: float  # shape (tail index): 0 exponential, >0 heavy (Pareto), <0 bounded
    sigma: float  # scale
    n_exceedances: int
    mean: float
    variance: float

    def to_dict(self) -> dict:
        return {
            "xi": self.xi,
            "sigma": self.sigma,
            "n_exceedances": self.n_exceedances,
            "mean": self.mean,
            "variance": self.variance,
        }


def evt_var(
    losses: Sequence[float],
    threshold: float,
    alpha: float,
) -> float:
    """GPD-based VaR at level ``alpha`` (e.g. 0.999).

    ``VaR_α = u + (σ/ξ)[((1−α)·n/N_u)^{−ξ} − 1]``. Falls back to the empirical
    threshold if the fit is degenerate (ξ≈0 ⇒ exponential extrapolation).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    exc = peaks_over_threshold(losses, threshold)
    n = len(losses)
    nu = len(exc)
    if nu < 2:
        # not enough exceedances; return empirical max
        return max(losses) if losses else threshold
    fit = gpd_fit(exc)
    tail_prob = (1.0 - alpha) * n / nu
    if fit.xi == 0.0:
        # exponential tail: VaR = u + σ * ln(1/tail_prob)
        return threshold + fit.sigma * math.log(1.0 / tail_prob) if tail_prob > 0 else max(losses)
    if fit.xi < 0:
        # short-tailed (bounded): tail_prob^{−ξ} approaches 0 as tail_prob→0
        # but we cap the extrapolation at the (unknown) right endpoint; use formula anyway
        pass
    if tail_prob <= 0:
        return max(losses)
    inner = tail_prob ** (-fit.xi) - 1.0
    return threshold + (fit.sigma / fit.xi) * inner


def evt_cvar(
    losses: Sequence[float],
    threshold: float,
    alpha: float,
) -> float:
    """GPD-based CVaR (Expected Shortfall) at level ``alpha``.

    ``CVaR_α = (VaR_α + σ − ξ·u) / (1 − ξ)`` for ``ξ < 1``.
    """
    var = evt_var(losses, threshold, alpha)
    exc = peaks_over_threshold(losses, threshold)
    nu = len(exc)
    if nu < 2:
        return var
    fit = gpd_fit(exc)
    if fit.xi >= 1.0:
        # infinite mean → return VaR (cannot integrate)
        return var
    if fit.xi == 0.0:
        return var + fit.sigma
    return (var + fit.sigma - fit.xi * threshold) / (1.0 - fit.xi)


@dataclass(frozen=True)
class EvtResult:
    threshold: float
    n_exceedances: int
    gpd: GpdfitResult
    var: dict[str, float]
    cvar: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "n_exceedances": self.n_exceedances,
            "gpd": self.gpd.to_dict(),
            "var": self.var,
            "cvar": self.cvar,
        }


def evt_report(
    losses: Sequence[float],
    threshold: float,
    confidence_levels: Sequence[float] = (0.99, 0.999, 0.9999),
) -> EvtResult:
    """Full EVT tail-risk report at multiple confidence levels."""
    if not losses:
        raise ValueError("losses must be non-empty")
    exc = peaks_over_threshold(losses, threshold)
    fit = gpd_fit(exc) if len(exc) >= 2 else GpdfitResult(xi=0.0, sigma=0.0, n_exceedances=len(exc), mean=0.0, variance=0.0)
    var = {f"{c}": evt_var(losses, threshold, c) for c in confidence_levels}
    cvar = {f"{c}": evt_cvar(losses, threshold, c) for c in confidence_levels}
    return EvtResult(
        threshold=threshold,
        n_exceedances=len(exc),
        gpd=fit,
        var=var,
        cvar=cvar,
    )