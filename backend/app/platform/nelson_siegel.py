"""P255: Nelson-Siegel-Svensson (NSS) yield-curve fitting and interpolation.

The Nelson-Siegel (1987) / Svensson (1994) parametric yield curve models the
continuously-compounded zero rate at maturity τ as

    NS:  r(τ) = β₀ + (β₁ + β₂)·(1 − e^{−τ/τ₁})/(τ/τ₁) − β₂·e^{−τ/τ₁}
    NSS: r(τ) = β₀ + β₁·(1 − e^{−τ/τ₁})/(τ/τ₁) + β₂·[(1 − e^{−τ/τ₁})/(τ/τ₁) − e^{−τ/τ₁}]
                + β₃·[(1 − e^{−τ/τ₂})/(τ/τ₂) − e^{−τ/τ₂}]

with long-rate level ``β₀ > 0``, short-rate component ``β₁`` (``β₀+β₁`` = the
instantaneous rate), and curvature components ``β₂, β₃`` modulated by decay
time-constants ``τ₁, τ₂``. The instantaneous forward rate is ``f(τ) = β₀ +
β₁ e^{−τ/τ₁} + β₂ e^{−τ/τ₁}(τ/τ₁) + β₃ e^{−τ/τ₂}(τ/τ₂)``.

* **nelson_siegel_rate(tau, beta0, beta1, beta2, tau1)** — NS zero rate.
* **nelson_siegel_svensson_rate(...)** — NSS zero rate.
* **fit_nss(maturities, yields)** — non-linear least squares fit of the 6 NSS
  parameters via Levenberg-Marquardt Gauss-Newton with bounded ``τ`` and
  ``β`` projection. Returns :class:`NssFit`.
* **discount_factor(rate, tau)** — ``e^{−r(τ)·τ}``.

Pure Python, no scipy/numpy. Reference: Nelson & Siegel (1987); Svensson
(1994); Diebold-Li (2006). Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "NssFit",
    "nelson_siegel_rate",
    "nelson_siegel_svensson_rate",
    "discount_factor",
    "fit_nss",
]


def _load_factor(tau: float, decay: float) -> float:
    """The slope load ``(1 − e^{−τ/decay})/(τ/decay)`` (→ 1 as τ→0, → 0 as τ→∞)."""
    if decay <= 0.0:
        raise ValueError("decay (tau) must be positive")
    if tau <= 0.0:
        return 1.0
    x = tau / decay
    return (1.0 - math.exp(-x)) / x


def nelson_siegel_rate(tau: float, beta0: float, beta1: float, beta2: float, tau1: float) -> float:
    """Nelson-Siegel zero rate at maturity ``tau``."""
    if tau1 <= 0.0:
        raise ValueError("tau1 must be positive")
    if tau < 0.0:
        raise ValueError("tau must be non-negative")
    if tau == 0.0:
        return beta0 + beta1
    load1 = _load_factor(tau, tau1)
    return beta0 + beta1 * load1 + beta2 * (load1 - math.exp(-tau / tau1))


def nelson_siegel_svensson_rate(
    tau: float,
    beta0: float,
    beta1: float,
    beta2: float,
    beta3: float,
    tau1: float,
    tau2: float,
) -> float:
    """Nelson-Siegel-Svensson zero rate at maturity ``tau``."""
    if tau1 <= 0.0 or tau2 <= 0.0:
        raise ValueError("tau1, tau2 must be positive")
    if tau < 0.0:
        raise ValueError("tau must be non-negative")
    if tau == 0.0:
        return beta0 + beta1
    load1 = _load_factor(tau, tau1)
    load2 = _load_factor(tau, tau2)
    return (
        beta0
        + beta1 * load1
        + beta2 * (load1 - math.exp(-tau / tau1))
        + beta3 * (load2 - math.exp(-tau / tau2))
    )


def discount_factor(rate: float, tau: float) -> float:
    """Continuously-compounded discount factor ``e^{−r·τ}``."""
    if tau < 0.0:
        raise ValueError("tau must be non-negative")
    return math.exp(-rate * tau)


def _nss_residuals(maturities: Sequence[float], yields: Sequence[float], params: list[float]) -> list[float]:
    b0, b1, b2, b3, t1, t2 = params
    return [
        nelson_siegel_svensson_rate(tau, b0, b1, b2, b3, t1, t2) - y
        for tau, y in zip(maturities, yields)
    ]


def _nss_jacobian(tau: float, params: list[float]) -> list[float]:
    """Jacobian row ∂r/∂(β₀,β₁,β₂,β₃,τ₁,τ₂) at a single tau (β part only; τ partials set 0)."""
    b0, b1, b2, b3, t1, t2 = params
    if tau == 0.0:
        return [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    load1 = _load_factor(tau, t1)
    load2 = _load_factor(tau, t2)
    e1 = math.exp(-tau / t1)
    e2 = math.exp(-tau / t2)
    return [1.0, load1, load1 - e1, load2 - e2, 0.0, 0.0]


def _solve6(mat: list[list[float]], rhs: list[float]) -> list[float] | None:
    n = 6
    a = [row[:] + [rhs[i]] for i, row in enumerate(mat)]
    for col in range(n):
        pivot = col
        best = abs(a[col][col])
        for r in range(col + 1, n):
            if abs(a[r][col]) > best:
                best = abs(a[r][col])
                pivot = r
        if best < 1e-18:
            return None
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        piv = a[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col] / piv
            if factor != 0.0:
                for c in range(n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def _conditional_beta(maturities: Sequence[float], yields: Sequence[float], t1: float, t2: float) -> tuple[float, float, float, float]:
    """Conditional least-squares for (β₀,β₁,β₂,β₃) given the decay constants.

    The NSS rate is linear in β given τ₁, τ₂, so we solve the 4×4 normal
    equations of the design matrix [1, load₁, load₁−e1, load₂−e2].
    """
    rows: list[list[float]] = []
    for tau in maturities:
        if tau == 0.0:
            rows.append([1.0, 1.0, 0.0, 0.0])
        else:
            l1 = _load_factor(tau, t1)
            l2 = _load_factor(tau, t2)
            rows.append([1.0, l1, l1 - math.exp(-tau / t1), l2 - math.exp(-tau / t2)])
    # Normal equations AᵀA β = Aᵀy on the 4 columns.
    ata = [[0.0] * 4 for _ in range(4)]
    aty = [0.0] * 4
    for r, y in zip(rows, yields):
        for i in range(4):
            aty[i] += r[i] * y
            for j in range(4):
                ata[i][j] += r[i] * r[j]
    # Solve via Gaussian elimination.
    aug = [ata[i][:] + [aty[i]] for i in range(4)]
    for col in range(4):
        pivot = col
        best = abs(aug[col][col])
        for rr in range(col + 1, 4):
            if abs(aug[rr][col]) > best:
                best = abs(aug[rr][col])
                pivot = rr
        if best < 1e-18:
            continue
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        piv = aug[col][col]
        for rr in range(4):
            if rr == col:
                continue
            factor = aug[rr][col] / piv
            if factor != 0.0:
                for c in range(5):
                    aug[rr][c] -= factor * aug[col][c]
    beta = [aug[i][4] / aug[i][i] if abs(aug[i][i]) > 1e-18 else 0.0 for i in range(4)]
    return beta[0], beta[1], beta[2], beta[3]


@dataclass(frozen=True)
class NssFit:
    beta0: float
    beta1: float
    beta2: float
    beta3: float
    tau1: float
    tau2: float
    rms: float
    n_points: int

    def to_dict(self) -> dict:
        return {
            "beta0": self.beta0,
            "beta1": self.beta1,
            "beta2": self.beta2,
            "beta3": self.beta3,
            "tau1": self.tau1,
            "tau2": self.tau2,
            "rms": self.rms,
            "n_points": self.n_points,
        }


def _project(b0: float, b1: float, b2: float, b3: float, t1: float, t2: float) -> tuple[float, float, float, float, float, float]:
    """Project into the admissible set: τ₁<τ₂, both positive; β unconstrained."""
    t1 = max(t1, 1e-3)
    t2 = max(t2, t1 + 1e-2)  # keep τ₂ strictly greater than τ₁
    return b0, b1, b2, b3, t1, t2


def fit_nss(
    maturities: Sequence[float],
    yields: Sequence[float],
    *,
    init: tuple[float, float, float, float, float, float] | None = None,
    max_iter: int = 300,
    tol: float = 1e-12,
) -> NssFit:
    """Fit NSS parameters to (maturity, yield) observations via Gauss-Newton/LM.

    Raises ``ValueError`` on length mismatch / empty / < 2 points / non-positive
    maturities.
    """
    n = len(maturities)
    if n != len(yields):
        raise ValueError("maturities and yields must have equal length")
    if n < 2:
        raise ValueError("need at least 2 points to fit")
    if any(t < 0.0 for t in maturities):
        raise ValueError("maturities must be non-negative")
    taus = [float(t) for t in maturities]
    ys = [float(y) for y in yields]

    if init is None:
        # Grid-search the decay constants (β are conditionally linear given τ),
        # then refine β by least squares. This avoids the zero τ-derivative in the
        # analytical Jacobian and reliably recovers clean curves.
        best = None
        best_sse = float("inf")
        t1_grid = [0.3, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 7.0]
        for t1_cand in t1_grid:
            t2_grid = [c for c in t1_grid if c > t1_cand] + [10.0, 15.0, 20.0]
            for t2_cand in t2_grid:
                b0_c, b1_c, b2_c, b3_c = _conditional_beta(taus, ys, t1_cand, t2_cand)
                sse_c = sum(rr * rr for rr in _nss_residuals(
                    taus, ys, [b0_c, b1_c, b2_c, b3_c, t1_cand, t2_cand]))
                if sse_c < best_sse:
                    best_sse = sse_c
                    best = (b0_c, b1_c, b2_c, b3_c, t1_cand, t2_cand)
        b0, b1, b2, b3, t1, t2 = best if best is not None else (sum(ys) / n, 0.0, 0.0, 0.0, 2.0, 5.0)
    else:
        b0, b1, b2, b3, t1, t2 = init
    b0, b1, b2, b3, t1, t2 = _project(b0, b1, b2, b3, t1, t2)
    params = [b0, b1, b2, b3, t1, t2]
    lam = 1e-3

    def sse(p: list[float]) -> float:
        r = _nss_residuals(taus, ys, p)
        return sum(x * x for x in r)

    cur = sse(params)
    for _ in range(max_iter):
        r = _nss_residuals(taus, ys, params)
        jtj = [[0.0] * 6 for _ in range(6)]
        jtr = [0.0] * 6
        for tau, ri in zip(taus, r):
            jac = _nss_jacobian(tau, params)
            for i in range(6):
                jtr[i] += jac[i] * ri
                for j in range(6):
                    jtj[i][j] += jac[i] * jac[j]
        for i in range(6):
            jtj[i][i] += lam * (jtj[i][i] + 1e-12)
        delta = _solve6(jtj, [-x for x in jtr])
        if delta is None:
            lam *= 10.0
            if lam > 1e10:
                break
            continue
        new_params = list(_project(
            params[0] + delta[0], params[1] + delta[1], params[2] + delta[2],
            params[3] + delta[3], params[4] + delta[4], params[5] + delta[5],
        ))
        new_sse = sse(new_params)
        if new_sse < cur:
            params = new_params
            if abs(cur - new_sse) < tol * max(cur, 1e-12):
                cur = new_sse
                break
            cur = new_sse
            lam = max(lam * 0.5, 1e-12)
        else:
            lam *= 2.0
            if lam > 1e10:
                break
    rms = math.sqrt(cur / n)
    b0, b1, b2, b3, t1, t2 = params
    return NssFit(
        beta0=b0, beta1=b1, beta2=b2, beta3=b3, tau1=t1, tau2=t2,
        rms=rms, n_points=n,
    )