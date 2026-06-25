"""P246: Stochastic processes / SDE simulation and analytic moments.

Pure-Python, dependency-free simulation of the canonical continuous-time
processes used in derivatives and risk modelling, plus their closed-form
moment identities so tests can verify correctness without a reference:

* **Geometric Brownian Motion** (GBM): ``dS = μS dt + σS dW``; exact solution
  ``Sₜ = S₀ exp((μ − ½σ²)t + σWₜ)``; ``E[ln Sₜ] = ln S₀ + (μ−½σ²)t``,
  ``Var[ln Sₜ] = σ²t``.
* **Ornstein-Uhlenbeck** (OU): ``dx = κ(θ−x) dt + σ dW``; stationary
  ``Var = σ²/(2κ)``; ``E[xₜ] = θ + (x₀−θ)e^{−κt}``.
* **Cox-Ingersoll-Ross** (CIR): ``dr = κ(θ−r) dt + σ√r dW``; non-central χ²
  transition; stationary mean θ, stationary variance ``σ²θ/(2κ)``; positivity
  preserved when ``2κθ ≥ σ²`` (Feller condition). We simulate via the
  absorbing-zero Euler scheme and report the bias.
* **Merton Jump-Diffusion**: GBM plus compound-Poisson jumps
  ``Nₜ ~ Poisson(λt)``, jump sizes ``J ~ N(ln(1+m), v²)``; analytic
  ``E[ln Sₜ] = (μ − ½σ² − λm) t`` (with mean jump contribution folded in).

Simulation uses the Euler-Maruyama discretisation (exact for GBM) driven by an
injected ``random.Random(seed)`` — fully deterministic. No numpy / scipy.

Reference: Glasserman (2003) "Monte Carlo Methods in Financial Engineering";
Cox-Ingersoll-Ross (1985); Merton (1976); Ornstein-Uhlenbeck (1930).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

from app.platform._math_utils import norm_inv

__all__ = [
    "ProcessResult",
    "gbm_simulate",
    "ou_simulate",
    "cir_simulate",
    "merton_jd_simulate",
    "gbm_moments",
    "ou_moments",
    "cir_moments",
    "merton_jd_moments",
]


@dataclass(frozen=True)
class ProcessResult:
    process: str
    path: list[float]
    times: list[float]
    n_steps: int
    dt: float
    seed: int

    def to_dict(self) -> dict:
        return {
            "process": self.process,
            "path": self.path,
            "times": self.times,
            "n_steps": self.n_steps,
            "dt": self.dt,
            "seed": self.seed,
        }


def _normal(rng: random.Random) -> float:
    """Standard normal sample via the Box-Muller transform (deterministic)."""
    u1 = rng.random()
    u2 = rng.random()
    # Guard against log(0).
    if u1 < 1e-12:
        u1 = 1e-12
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _times(horizon: float, n_steps: int) -> list[float]:
    if horizon <= 0.0:
        raise ValueError("horizon must be positive")
    if n_steps <= 0:
        raise ValueError("n_steps must be positive")
    dt = horizon / n_steps
    return [i * dt for i in range(n_steps + 1)]


# ---------------------------------------------------------------------------
# GBM
# ---------------------------------------------------------------------------


def gbm_simulate(
    s0: float,
    mu: float,
    sigma: float,
    horizon: float,
    n_steps: int,
    seed: int = 0,
) -> ProcessResult:
    """Simulate GBM via the exact log-Euler scheme ``Sₜ = S₀ exp(...)``."""
    if s0 <= 0.0:
        raise ValueError("s0 must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    dt = horizon / n_steps
    times = _times(horizon, n_steps)
    rng = random.Random(seed)
    path = [s0]
    log_s = math.log(s0)
    drift = (mu - 0.5 * sigma * sigma) * dt
    diff = sigma * math.sqrt(dt)
    for _ in range(n_steps):
        log_s = log_s + drift + diff * _normal(rng)
        path.append(math.exp(log_s))
    return ProcessResult("gbm", path, times, n_steps, dt, seed)


def gbm_moments(s0: float, mu: float, sigma: float, t: float) -> dict[str, float]:
    """Analytic ``E[ln Sₜ]`` and ``Var[ln Sₜ]`` for GBM."""
    if t < 0.0:
        raise ValueError("t must be non-negative")
    return {
        "mean_log": math.log(s0) + (mu - 0.5 * sigma * sigma) * t,
        "var_log": sigma * sigma * t,
        "mean": s0 * math.exp(mu * t),
    }


# ---------------------------------------------------------------------------
# Ornstein-Uhlenbeck
# ---------------------------------------------------------------------------


def ou_simulate(
    x0: float,
    kappa: float,
    theta: float,
    sigma: float,
    horizon: float,
    n_steps: int,
    seed: int = 0,
) -> ProcessResult:
    """Simulate OU ``dx = κ(θ−x) dt + σ dW`` via Euler-Maruyama."""
    if kappa <= 0.0:
        raise ValueError("kappa must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    dt = horizon / n_steps
    times = _times(horizon, n_steps)
    rng = random.Random(seed)
    path = [x0]
    x = x0
    diff = sigma * math.sqrt(dt)
    for _ in range(n_steps):
        x = x + kappa * (theta - x) * dt + diff * _normal(rng)
        path.append(x)
    return ProcessResult("ou", path, times, n_steps, dt, seed)


def ou_moments(x0: float, kappa: float, theta: float, sigma: float, t: float) -> dict[str, float]:
    """Analytic OU mean / variance; stationary variance ``σ²/(2κ)``."""
    if t < 0.0:
        raise ValueError("t must be non-negative")
    e = math.exp(-kappa * t)
    mean = theta + (x0 - theta) * e
    var = sigma * sigma * (1.0 - math.exp(-2.0 * kappa * t)) / (2.0 * kappa)
    return {
        "mean": mean,
        "var": var,
        "stationary_var": sigma * sigma / (2.0 * kappa),
    }


# ---------------------------------------------------------------------------
# CIR
# ---------------------------------------------------------------------------


def cir_simulate(
    r0: float,
    kappa: float,
    theta: float,
    sigma: float,
    horizon: float,
    n_steps: int,
    seed: int = 0,
) -> ProcessResult:
    """Simulate CIR ``dr = κ(θ−r) dt + σ√r dW`` via absorbing-zero Euler."""
    if r0 < 0.0:
        raise ValueError("r0 must be non-negative")
    if kappa <= 0.0:
        raise ValueError("kappa must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    if theta < 0.0:
        raise ValueError("theta must be non-negative")
    dt = horizon / n_steps
    times = _times(horizon, n_steps)
    rng = random.Random(seed)
    path = [r0]
    r = r0
    sqrt_dt = math.sqrt(dt)
    for _ in range(n_steps):
        # full-truncation / absorbing zero: sqrt(max(r,0))
        vol = sigma * math.sqrt(max(r, 0.0)) * sqrt_dt
        r = r + kappa * (theta - r) * dt + vol * _normal(rng)
        # Reflect at zero (non-negativity) — common Euler fix.
        if r < 0.0:
            r = 0.0
        path.append(r)
    return ProcessResult("cir", path, times, n_steps, dt, seed)


def cir_moments(r0: float, kappa: float, theta: float, sigma: float, t: float) -> dict[str, float]:
    """Analytic CIR mean / stationary variance and Feller flag."""
    if t < 0.0:
        raise ValueError("t must be non-negative")
    e = math.exp(-kappa * t)
    mean = theta + (r0 - theta) * e
    stationary_var = sigma * sigma * theta / (2.0 * kappa)
    feller = 2.0 * kappa * theta >= sigma * sigma
    return {
        "mean": mean,
        "stationary_var": stationary_var,
        "feller_satisfied": 1.0 if feller else 0.0,
    }


# ---------------------------------------------------------------------------
# Merton jump diffusion
# ---------------------------------------------------------------------------


def merton_jd_simulate(
    s0: float,
    mu: float,
    sigma: float,
    jump_lambda: float,
    jump_mean: float,
    jump_std: float,
    horizon: float,
    n_steps: int,
    seed: int = 0,
) -> ProcessResult:
    """Simulate Merton jump-diffusion: GBM plus compound-Poisson log jumps.

    Jump sizes ``J ~ N(jump_mean, jump_std²)`` arrive at rate ``jump_lambda``
    per unit time. The drift is adjusted by ``−λ·(e^{m+½v²}−1)`` so the
    discounted asset is a martingale when ``μ`` is the risk-neutral rate.
    """
    if s0 <= 0.0:
        raise ValueError("s0 must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    if jump_lambda < 0.0:
        raise ValueError("jump_lambda must be non-negative")
    if jump_std < 0.0:
        raise ValueError("jump_std must be non-negative")
    dt = horizon / n_steps
    times = _times(horizon, n_steps)
    rng = random.Random(seed)
    # Drift correction so E[S_t] = S0 exp(mu t).
    m = jump_mean
    v2 = jump_std * jump_std
    correction = jump_lambda * (math.exp(m + 0.5 * v2) - 1.0)
    drift = (mu - correction - 0.5 * sigma * sigma) * dt
    diff = sigma * math.sqrt(dt)
    log_s = math.log(s0)
    path = [s0]
    for _ in range(n_steps):
        log_s = log_s + drift + diff * _normal(rng)
        # Compound-Poisson jumps in this step.
        if jump_lambda > 0.0:
            n_jumps = _poisson(rng, jump_lambda * dt)
            for _j in range(n_jumps):
                log_s += m + jump_std * _normal(rng)
        path.append(math.exp(log_s))
    return ProcessResult("merton_jd", path, times, n_steps, dt, seed)


def _poisson(rng: random.Random, lam: float) -> int:
    """Sample from Poisson(lam) via Knuth's algorithm (deterministic)."""
    if lam <= 0.0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def merton_jd_moments(
    s0: float,
    mu: float,
    sigma: float,
    jump_lambda: float,
    jump_mean: float,
    jump_std: float,
    t: float,
) -> dict[str, float]:
    """Analytic Merton-JD ``E[Sₜ]`` and ``Var[ln Sₜ]``.

    ``E[Sₜ] = S₀ e^{μt}`` (by the drift correction used in simulation).
    ``Var[ln Sₜ] = (σ² + λ(m²+v²)) t``.
    """
    if t < 0.0:
        raise ValueError("t must be non-negative")
    v2 = jump_std * jump_std
    var_log = (sigma * sigma + jump_lambda * (jump_mean * jump_mean + v2)) * t
    return {
        "mean": s0 * math.exp(mu * t),
        "var_log": var_log,
        "expected_jumps": jump_lambda * t,
    }