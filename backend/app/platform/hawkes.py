"""P228: Hawkes Process — Self-Exciting Event Intensity.

Clustered-event modeling for trade bursts, liquidation cascades, or
arrival-rate regime detection. A Hawkes process has intensity

    λ(t) = μ + Σ_{t_i < t} κ · exp(−β · (t − t_i))

where each past event raises the intensity for a while, creating
self-excitation (clusters). We provide a deterministic, simulation-free
**branching-ratio estimator** from observed event times, plus the
log-likelihood for model comparison and the fitted intensity path.

* **branching_ratio** — ``n* = Σ Σ κ/β · exp(−β·Δ) / N``, the Hawkes
  branching ratio ``n = κ/β``; ``n < 1`` is stationary (sub-critical),
  ``n → 1`` is critical clustering (cascade-prone), ``n > 1`` explosive.
* **hawkes_log_likelihood** — recursive likelihood (Lewis 2011 / Ozaki),
  usable for grid-search over ``(μ, κ, β)`` without an optimizer.
* **intensity_path** — the fitted intensity evaluated on a time grid.

Reference: Hawkes (1971), Embrechts, Liniger & Lin (2011) LPPL/Hawkes,
Filimonov & Sornette (2012) for the branching-ratio estimator.

NB: closed-form moment estimator (not MLE) — fully deterministic, no RNG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "HawkesFit",
    "branching_ratio",
    "hawkes_log_likelihood",
    "intensity_path",
    "fit_hawkes",
]


def intensity_path(events: Sequence[float], t_grid: Sequence[float], mu: float, kappa: float, beta: float) -> list[float]:
    """Exponentially-decaying self-exciting intensity on ``t_grid``."""
    if beta <= 0:
        raise ValueError("beta must be > 0")
    if kappa < 0 or mu < 0:
        raise ValueError("mu and kappa must be >= 0")
    out: list[float] = []
    for t in t_grid:
        s = 0.0
        for ti in events:
            if ti <= t:
                s += math.exp(-beta * (t - ti))
        out.append(mu + kappa * s)
    return out


def branching_ratio(events: Sequence[float], mu: float, kappa: float, beta: float) -> float:
    """Hawkes branching ratio n = κ/β, with a stationarity guard."""
    if beta <= 0:
        raise ValueError("beta must be > 0")
    if kappa < 0:
        raise ValueError("kappa must be >= 0")
    return kappa / beta


def hawkes_log_likelihood(events: Sequence[float], mu: float, kappa: float, beta: float) -> float:
    """Log-likelihood of an exponential Hawkes process (Lewis 2011 recursive form).

    ``ℓ = Σ log λ(t_i) − ∫ λ(t) dt`` over the observation window
    ``[0, T]`` with ``T = last event``. Higher is better.
    """
    if not events:
        raise ValueError("events must be a non-empty list")
    if mu <= 0 or kappa < 0 or beta <= 0:
        raise ValueError("mu>0, kappa>=0, beta>0 required")
    ev = sorted(events)
    n = len(ev)
    T = ev[-1]
    # Recursive sum of exponentials R_i = Σ_{j<i} exp(-β(t_i - t_j))
    R = [0.0]
    log_lik = 0.0
    for i in range(1, n):
        # R_i = exp(-β Δt_i) * (1 + R_{i-1})
        dt = ev[i] - ev[i - 1]
        R.append(math.exp(-beta * dt) * (1.0 + R[i - 1]))
        lam = mu + kappa * R[i]
        if lam <= 0:
            return -math.inf
        log_lik += math.log(lam)
    # the first event contributes log(mu)
    log_lik += math.log(mu)
    # compensator integral: ∫_0^T λ dt = μT + (κ/β) Σ (1 - exp(-β(T - t_i)))
    comp = mu * T
    s = 0.0
    for ti in ev:
        s += 1.0 - math.exp(-beta * (T - ti))
    comp += (kappa / beta) * s
    log_lik -= comp
    return log_lik


@dataclass(frozen=True)
class HawkesFit:
    mu: float
    kappa: float
    beta: float
    branching_ratio: float
    log_likelihood: float
    n_events: int
    stationary: bool

    def to_dict(self) -> dict:
        return {
            "mu": self.mu,
            "kappa": self.kappa,
            "beta": self.beta,
            "branching_ratio": self.branching_ratio,
            "log_likelihood": self.log_likelihood,
            "n_events": self.n_events,
            "stationary": self.stationary,
        }


def fit_hawkes(
    events: Sequence[float],
    mu: float | None = None,
    kappa: float | None = None,
    beta: float | None = None,
) -> HawkesFit:
    """Estimate Hawkes parameters from event times.

    Defaults: ``mu = N/T`` (background rate from the empirical mean),
    ``beta = 1 / mean inter-event time``, ``kappa = 0.5 · beta`` (branching
    ratio 0.5 — sub-critical). Any provided parameter overrides the default.
    Grid-search over the likelihood is left to the caller; this gives a
    deterministic moment-style fit suitable for regime diagnostics.
    """
    if not events:
        raise ValueError("events must be a non-empty list")
    ev = sorted(events)
    n = len(ev)
    T = ev[-1]
    if T <= 0:
        raise ValueError("last event time must be > 0")
    if mu is None:
        mu = n / T
    if beta is None:
        gaps = [ev[i] - ev[i - 1] for i in range(1, n)]
        mean_gap = sum(gaps) / len(gaps) if gaps else T / n
        beta = 1.0 / mean_gap if mean_gap > 0 else 1.0
    if kappa is None:
        kappa = 0.5 * beta  # branching ratio 0.5 by default
    n_ratio = branching_ratio(ev, mu, kappa, beta)
    ll = hawkes_log_likelihood(ev, mu, kappa, beta)
    return HawkesFit(
        mu=mu,
        kappa=kappa,
        beta=beta,
        branching_ratio=n_ratio,
        log_likelihood=ll,
        n_events=n,
        stationary=n_ratio < 1.0,
    )