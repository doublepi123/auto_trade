"""P242: Portfolio Diversification Diagnostics — effective N, diversification ratio.

Quantify how much of a portfolio's risk is *independent* versus merely
nominal. The two foundational quantities are Choueifaty-Coignard (2008)
**diversification ratio** and the Bouchaud-Potters **effective number of
bets**, plus the Herfindahl-Hirschman concentration index and a Lorenz-style
cumulative-share curve. All closed form, deterministic, pure Python.

* **diversification_ratio** — ``DR = (wᵀσ) / sqrt(wᵀΣw)`` ≥ 1 always; higher
  means more diversification. ``σ`` is the per-asset vol vector and ``Σ`` the
  covariance matrix. ``DR = 1`` ⇒ the portfolio is effectively a single asset
  (all risk concentrated); ``DR → n`` ⇒ uncorrelated equal-vol assets.
* **effective_n** — ``N_eff = DR² = (wᵀσ)² / (wᵀΣw)``: the number of
  independent bets the portfolio is equivalent to. Bouchaud-Potters /
  Choueifaty-Coignard. ``N_eff ∈ [1, n]``.
* **diversification_benefit** — ``1 − sqrt(wᵀΣw) / (wᵀσ)``: the fraction of
  weighted-average volatility eliminated by diversification (in ``[0, 1)``).
* **concentration_curve** — Lorenz-style cumulative-share curve of the
  sorted-ascending absolute weights (the Gini-style curve), endpoint 1.
* **concentration_index** — Herfindahl-Hirschman ``Σ wᵢ²`` on normalized
  weights, ``∈ [1/n, 1]``; lower = more diversified.
* **diversification_report** — :class:`DiversificationResult` aggregating all
  of the above plus ``n_assets``.

Reference: Choueifaty & Coignard (2008) "Toward Maximum Diversification",
Bouchaud & Potters "The Effective Number of Assets", Maillard-Roncalli.
Pure Python, no scipy.

Notes
-----
No new dependencies. Inputs are plain Python sequences / nested lists; the
covariance matrix is a square ``list[list[float]]`` symmetric and PSD
(only its diagonal and the quadratic form ``wᵀΣw`` are evaluated, so a
valid covariance is assumed — we do not symmetrize or project it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "DiversificationResult",
    "effective_n",
    "diversification_ratio",
    "diversification_benefit",
    "concentration_curve",
    "concentration_index",
    "diversification_report",
]


def _quadratic_form(weights: Sequence[float], cov: list[list[float]]) -> float:
    """Compute ``wᵀ Σ w`` for a square covariance matrix."""
    n = len(weights)
    total = 0.0
    for i in range(n):
        row = cov[i]
        wi = weights[i]
        for j in range(n):
            total += wi * row[j] * weights[j]
    return total


def _validate(weights: Sequence[float], sigmas: Sequence[float], cov: list[list[float]]) -> None:
    if not weights:
        raise ValueError("weights must be non-empty")
    if len(sigmas) != len(weights):
        raise ValueError("sigmas length must match weights length")
    n = len(weights)
    if len(cov) != n:
        raise ValueError("cov must have one row per weight")
    for row in cov:
        if len(row) != n:
            raise ValueError("cov must be square (row length == n_weights)")
    for s in sigmas:
        if s < 0:
            raise ValueError("sigmas must be non-negative")
    for w in weights:
        if not math.isfinite(w):
            raise ValueError("weights must be finite")


def diversification_ratio(
    weights: Sequence[float],
    sigmas: Sequence[float],
    cov: list[list[float]],
) -> float:
    """Choueifaty-Coignard diversification ratio.

    ``DR = (Σ_i w_i σ_i) / sqrt(wᵀ Σ w)``.

    Always ``≥ 1`` for a valid long-only covariance (Cauchy-Schwarz:
    ``(wᵀσ)² ≤ (wᵀΣw)(σᵀΣ⁻¹σ)`` and for a single-asset portfolio equality
    holds). Raises :class:`ValueError` on shape mismatch, empty input, or a
    non-positive portfolio volatility (``wᵀΣw ≤ 0``).
    """
    _validate(weights, sigmas, cov)
    weighted_vol = sum(w * s for w, s in zip(weights, sigmas))
    pv = _quadratic_form(weights, cov)
    if pv <= 0:
        raise ValueError("portfolio variance must be positive")
    return weighted_vol / math.sqrt(pv)


def effective_n(
    weights: Sequence[float],
    cov: list[list[float]],
) -> float:
    """Effective number of independent bets.

    ``N_eff = (Σ_i w_i σ_i)² / (wᵀΣw) = DR²``, where ``σ_i = sqrt(Σ_ii)`` is
    the per-asset volatility read off the covariance diagonal. This is the
    Bouchaud-Potters / Choueifaty-Coignard definition. ``N_eff ∈ [1, n]`` for
    a valid long-only portfolio. Raises :class:`ValueError` on empty weights,
    a non-square covariance, or non-positive portfolio variance.
    """
    if not weights:
        raise ValueError("weights must be non-empty")
    n = len(weights)
    if len(cov) != n:
        raise ValueError("cov must have one row per weight")
    for row in cov:
        if len(row) != n:
            raise ValueError("cov must be square (row length == n_weights)")
    sigmas = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(n)]
    for w in weights:
        if not math.isfinite(w):
            raise ValueError("weights must be finite")
    weighted_vol = sum(w * s for w, s in zip(weights, sigmas))
    pv = _quadratic_form(weights, cov)
    if pv <= 0:
        raise ValueError("portfolio variance must be positive")
    return (weighted_vol * weighted_vol) / pv


def diversification_benefit(
    weights: Sequence[float],
    sigmas: Sequence[float],
    cov: list[list[float]],
) -> float:
    """Fraction of weighted-average volatility eliminated by diversification.

    ``benefit = 1 − sqrt(wᵀΣw) / (Σ_i w_i σ_i)`` ∈ ``[0, 1)``.

    Zero when the portfolio is a single asset (no diversification); close
    to one when the assets are nearly uncorrelated and equal-vol. Raises
    :class:`ValueError` on shape mismatch, empty input, or non-positive
    weighted/portfolio volatility.
    """
    _validate(weights, sigmas, cov)
    weighted_vol = sum(w * s for w, s in zip(weights, sigmas))
    if weighted_vol <= 0:
        raise ValueError("weighted-average volatility must be positive")
    pv = _quadratic_form(weights, cov)
    if pv <= 0:
        raise ValueError("portfolio variance must be positive")
    return 1.0 - math.sqrt(pv) / weighted_vol


def concentration_curve(weights: Sequence[float]) -> list[float]:
    """Lorenz-style cumulative-share curve of sorted-ascending weights.

    Weights are sorted ascending and normalized by their absolute sum (so the
    curve is well-defined for short/long books with mixed signs). Returns the
    list of cumulative shares; the first element is the smallest weight's
    share and the final element is ``1.0``. For a perfectly equal-weight book
    the curve rises linearly to 1; for a concentrated book it stays near 0
    then jumps. Raises :class:`ValueError` on empty or all-zero weights.
    """
    if not weights:
        raise ValueError("weights must be non-empty")
    total = sum(abs(w) for w in weights)
    if total <= 0:
        raise ValueError("weights must have positive absolute sum")
    sorted_w = sorted(abs(w) / total for w in weights)
    cumulative: list[float] = []
    running = 0.0
    for w in sorted_w:
        running += w
        cumulative.append(running)
    # Pin the final to exactly 1.0 to absorb float drift.
    if cumulative:
        cumulative[-1] = 1.0
    return cumulative


def concentration_index(weights: Sequence[float]) -> float:
    """Herfindahl-Hirschman concentration index on normalized weights.

    ``HHI = Σ_i (|w_i| / Σ|w|)²`` ∈ ``[1/n, 1]``. Lower = more diversified
    (``1/n`` for equal weights, ``1`` for a single-asset book). Raises
    :class:`ValueError` on empty or all-zero weights.
    """
    if not weights:
        raise ValueError("weights must be non-empty")
    total = sum(abs(w) for w in weights)
    if total <= 0:
        raise ValueError("weights must have positive absolute sum")
    return sum((abs(w) / total) ** 2 for w in weights)


@dataclass(frozen=True)
class DiversificationResult:
    n_assets: int
    effective_n: float
    diversification_ratio: float
    diversification_benefit: float
    concentration_index: float

    def to_dict(self) -> dict:
        return {
            "n_assets": self.n_assets,
            "effective_n": self.effective_n,
            "diversification_ratio": self.diversification_ratio,
            "diversification_benefit": self.diversification_benefit,
            "concentration_index": self.concentration_index,
        }


def diversification_report(
    weights: Sequence[float],
    sigmas: Sequence[float],
    cov: list[list[float]],
) -> DiversificationResult:
    """Full diversification diagnostic report.

    Aggregates :func:`effective_n`, :func:`diversification_ratio`,
    :func:`diversification_benefit` and :func:`concentration_index` into a
    :class:`DiversificationResult`. Raises :class:`ValueError` on mismatched
    lengths, empty input, or non-positive portfolio / weighted volatility.
    """
    return DiversificationResult(
        n_assets=len(weights),
        effective_n=effective_n(weights, cov),
        diversification_ratio=diversification_ratio(weights, sigmas, cov),
        diversification_benefit=diversification_benefit(weights, sigmas, cov),
        concentration_index=concentration_index(weights),
    )