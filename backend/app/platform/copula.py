"""P235: Copula Tail-Dependence (empirical + Gumbel/Clayton moment fit).

Model the dependence *structure* between two return series independently of
their marginals — the way Embrechts-McNeil-Straumann (1999) recommended for
risk management, since linear correlation is a poor descriptor of joint extreme
events. A copula (Sklar 1959) separates the marginals from the dependence: by
Sklar's theorem any joint distribution ``F(x,y)`` factors as
``C(F_X(x), F_Y(y))`` for a unique copula ``C`` on ``[0,1]^2``. Working in the
copula space (rank-based pseudo-observations) makes the dependence estimate
scale-free and marginal-free.

* **empirical_copula** — map each ``(x_i, y_i)`` pair to its rank-based
  pseudo-observations ``(u_i, v_i) = (rank(x_i)/(n+1), rank(y_i)/(n+1))``. Ties
  get the average rank (mid-rank). This is the empirical copula sample on the
  unit square — the non-parametric estimate of ``C``.
* **kendall_tau** — Kendall's ``τ`` by the O(n²) concordant-minus-discordant
  count: ``τ = (P − Q) / (½n(n−1))`` where ``P`` is the number of concordant
  pairs and ``Q`` discordant. Pure Python, no scipy.
* **gumbel_fit** — Gumbel (logistic) copula parameter by method of moments from
  ``τ``: ``θ = 1/(1−τ)`` for ``τ ≥ 0``. Gumbel only models upper-tail dependence
  and requires non-negative association.
* **clayton_fit** — Clayton copula parameter by method of moments from ``τ``:
  ``θ = 2τ/(1−τ)`` for ``τ > 0``. Clayton only models lower-tail dependence
  with positive association.
* **upper_tail_dependence_gumbel** — closed-form ``λ_U = 2 − 2^{1/θ}`` for the
  Gumbel copula (θ ≥ 1). Tail dependence is always present for the Gumbel
  family (``λ_U = 0`` at θ = 1 the independence boundary, → 1 as θ → ∞).
* **lower_tail_dependence_clayton** — closed-form ``λ_L = 2^{−1/θ}`` (θ>0).
* **tail_dependence_coeffs** — full report: ``τ``, both copula parameters (when
  their sign constraints hold), and the tail-dependence coefficients.

Reference: Sklar (1959), Nelsen "An Introduction to Copulas" (2006),
Embrechts-McNeil-Straumann (1999) on copulas in risk management.
Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "CopulaResult",
    "empirical_copula",
    "kendall_tau",
    "gumbel_fit",
    "clayton_fit",
    "upper_tail_dependence_gumbel",
    "lower_tail_dependence_clayton",
    "tail_dependence_coeffs",
]


def _variance(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _ranks(values: Sequence[float]) -> list[float]:
    """Mid-ranks (average rank for ties), 1-based."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based, mid-rank for ties
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def empirical_copula(u: Sequence[float], v: Sequence[float]) -> list[tuple[float, float]]:
    """Return rank-based pseudo-observation pairs ``(u_i, v_i)``.

    Maps each pair to its rank-based pseudo-observations
    ``u_i = rank(x_i)/(n+1)``, ``v_i = rank(y_i)/(n+1)`` (the ``n+1``
    denominator avoids values exactly at 1, standard copula practice). Ties get
    the average (mid) rank.
    """
    n = len(u)
    if n != len(v):
        raise ValueError("u and v must have equal length")
    if n == 0:
        raise ValueError("inputs must be non-empty")
    rx = _ranks(u)
    ry = _ranks(v)
    denom = n + 1
    return [(rx[i] / denom, ry[i] / denom) for i in range(n)]


def kendall_tau(x: Sequence[float], y: Sequence[float]) -> float:
    """Kendall's ``τ`` via the O(n²) concordant-discordant count.

    ``τ = (P − Q) / (½n(n−1))`` where ``P`` is the number of concordant pairs
    (``x_i − x_j`` and ``y_i − y_j`` same sign) and ``Q`` discordant. Ties
    contribute 0 to ``P − Q`` and remain in the denominator (τ-a style here,
    since ties on a continuous return series are degenerate anyway).
    Pure Python — no scipy.
    """
    n = len(x)
    if n != len(y):
        raise ValueError("x and y must have equal length")
    if n < 2:
        raise ValueError("need >=2 samples for Kendall tau")
    concordant = 0
    discordant = 0
    for i in range(n - 1):
        xi = x[i]
        yi = y[i]
        for j in range(i + 1, n):
            dx = xi - x[j]
            dy = yi - y[j]
            prod = dx * dy
            if prod > 0:
                concordant += 1
            elif prod < 0:
                discordant += 1
    total = n * (n - 1) / 2.0
    if total == 0:
        return 0.0
    return (concordant - discordant) / total


def gumbel_fit(tau: float) -> float:
    """Gumbel copula parameter ``θ`` from Kendall's ``τ``.

    ``θ = 1 / (1 − τ)`` for ``τ ≥ 0``. The Gumbel copula only models
    upper-tail dependence and requires non-negative association, so a negative
    ``τ`` is a model misspecification → ``ValueError``.
    """
    if tau >= 1.0:
        raise ValueError("tau must be < 1 for Gumbel fit")
    if tau < 0.0:
        raise ValueError("Gumbel copula requires tau >= 0 (upper-tail dependence only)")
    return 1.0 / (1.0 - tau)


def clayton_fit(tau: float) -> float:
    """Clayton copula parameter ``θ`` from Kendall's ``τ``.

    ``θ = 2τ / (1 − τ)`` for ``τ > 0``. The Clayton copula only models
    lower-tail dependence with positive association, so ``τ ≤ 0`` raises
    ``ValueError`` (it cannot be fit — the Clayton family is undefined there
    for lower-tail work).
    """
    if tau >= 1.0:
        raise ValueError("tau must be < 1 for Clayton fit")
    if tau <= 0.0:
        raise ValueError("Clayton copula requires tau > 0 (lower-tail dependence, positive association)")
    return 2.0 * tau / (1.0 - tau)


def upper_tail_dependence_gumbel(theta: float) -> float:
    """Gumbel upper-tail-dependence coefficient.

    ``λ_U = 2 − 2^{1/θ}`` for the Gumbel copula (θ ≥ 1). Tail dependence is
    always present for the Gumbel family: ``λ_U = 0`` at θ = 1 (the
    independence copula boundary), increasing monotonically toward 1 as
    θ → ∞.
    """
    if theta < 1.0:
        raise ValueError("Gumbel theta must be >= 1")
    return 2.0 - 2.0 ** (1.0 / theta)


def lower_tail_dependence_clayton(theta: float) -> float:
    """Clayton lower-tail-dependence coefficient.

    ``λ_L = 2^{−1/θ}`` for the Clayton copula (θ > 0). Tail dependence is
    present for θ > 0 (``λ_L > 0``, → 0 as θ → 0⁺, → 1 as θ → ∞).
    """
    if theta <= 0.0:
        raise ValueError("Clayton theta must be > 0")
    return 2.0 ** (-1.0 / theta)


@dataclass(frozen=True)
class CopulaResult:
    kendall_tau: float
    gumbel_theta: float | None
    clayton_theta: float | None
    upper_tail_dependence: float | None
    lower_tail_dependence: float | None
    n: int

    def to_dict(self) -> dict:
        return {
            "kendall_tau": self.kendall_tau,
            "gumbel_theta": self.gumbel_theta,
            "clayton_theta": self.clayton_theta,
            "upper_tail_dependence": self.upper_tail_dependence,
            "lower_tail_dependence": self.lower_tail_dependence,
            "n": self.n,
        }


def tail_dependence_coeffs(x: Sequence[float], y: Sequence[float]) -> CopulaResult:
    """Full copula tail-dependence report for two series.

    Computes Kendall's ``τ``, fits Gumbel (if ``τ ≥ 0``) and Clayton (if
    ``τ > 0``) by method-of-moments from ``τ``, and derives the closed-form
    tail-dependence coefficients. Raises ``ValueError`` for fewer than 10 samples
    or for constant series (zero variance, since ``τ`` is undefined there).
    """
    n = len(x)
    if n != len(y):
        raise ValueError("x and y must have equal length")
    if n < 10:
        raise ValueError("need >=10 samples for tail-dependence estimation")
    if _variance(x) <= 0.0 or _variance(y) <= 0.0:
        raise ValueError("constant series (zero variance) — tau undefined")

    tau = kendall_tau(x, y)

    gumbel_theta: float | None = None
    clayton_theta: float | None = None
    upper_td: float | None = None
    lower_td: float | None = None

    # Gumbel: defined for 0 <= tau < 1 (theta = 1/(1-tau) blows up at tau=1).
    if 0.0 <= tau < 1.0:
        gumbel_theta = gumbel_fit(tau)
        upper_td = upper_tail_dependence_gumbel(gumbel_theta)
    # Clayton: defined for 0 < tau < 1 (theta = 2tau/(1-tau) blows up at tau=1).
    if 0.0 < tau < 1.0:
        clayton_theta = clayton_fit(tau)
        lower_td = lower_tail_dependence_clayton(clayton_theta)

    return CopulaResult(
        kendall_tau=tau,
        gumbel_theta=gumbel_theta,
        clayton_theta=clayton_theta,
        upper_tail_dependence=upper_td,
        lower_tail_dependence=lower_td,
        n=n,
    )