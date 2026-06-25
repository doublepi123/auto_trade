"""Shared math helpers: standard normal CDF / inverse CDF.

Pure Python, no scipy/numpy. ``norm_cdf`` uses ``math.erf`` (exact); ``norm_inv``
uses Acklam's high-accuracy rational approximation (~1e-9 across the central
range). Centralised here so option pricing, implied vol, Kalman, LOESS and
stochastic-process modules share one implementation instead of re-deriving.

Reference: Acklam (2004) "An algorithm for computing the inverse normal CDF";
Abramowitz & Stegun 26.2.29 (via erf). Pure Python, no scipy.
"""

from __future__ import annotations

import math

__all__ = ["norm_cdf", "norm_inv", "norm_pdf"]


def norm_pdf(x: float) -> float:
    """Standard normal density φ(x)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    """Standard normal CDF Φ(x) via the error function (exact, deterministic)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_inv(p: float) -> float:
    """Inverse standard normal CDF Φ⁻¹(p) — Acklam's algorithm.

    Returns ``-inf`` / ``+inf`` for ``p`` outside ``(0, 1)``.
    """
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = (-2.0 * math.log(p)) ** 0.5
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = (-2.0 * math.log(1.0 - p)) ** 0.5
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)