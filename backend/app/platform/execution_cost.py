"""P227: Almgren-Chriss Optimal Execution.

The classic execution-cost model: split a parent order of ``X`` shares over
``T`` time slices, balancing market impact (trades move price against you)
against timing risk (price drifts while you wait). The trader's problem is

    minimize  E[cost] + λ · Var[cost]

over a trading trajectory ``x_0, x_1, …, x_T`` with ``x_0 = X``, ``x_T = 0``.
For the linear-impact / arithmetic-random-walk model the solution is a
deterministic **linear liquidation trajectory** plus a closed-form cost
decomposition into **impact, timing risk, and efficient frontier**.

We provide:

* ``almgren_chriss_trajectory`` — the optimal (risk-averse) share schedule.
* ``execution_cost`` — expected cost + variance (risk) for a given trajectory.
* ``efficient_frontier`` — ``(expected_cost, risk)`` pairs sweeping the
  risk-aversion λ, the standard Almgren-Chriss efficient frontier of execution.

Reference: Almgren & Chriss (2000) "Optimal Execution of Portfolio Transactions".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "AlmgrenChrissResult",
    "almgren_chriss",
    "almgren_chriss_trajectory",
    "execution_cost",
    "efficient_frontier",
]


def execution_cost(
    x: Sequence[float],
    eta: float,
    sigma: float,
) -> tuple[float, float]:
    """Expected cost and variance (risk) of a trajectory ``x``.

    With arithmetic random walk + linear temporary impact ``η·v`` (``v`` the
    per-slice trading rate), the Almgren-Chriss cost model gives

        E[cost]    = η · Σ v_t² · Δt           (temporary impact dominates)
        Var[cost]  = σ² · Σ x_t² · Δt          (timing risk from held inventory)

    where ``x_t`` is the *remaining* inventory at the start of slice ``t`` and
    ``v_t = (x_{t−1} − x_t)/Δt`` is the slice's trading rate. ``Δt = 1`` here.
    """
    if len(x) < 2:
        raise ValueError("trajectory needs >=2 points")
    if eta < 0 or sigma < 0:
        raise ValueError("eta and sigma must be >=0")
    exp_cost = 0.0
    variance = 0.0
    for i in range(1, len(x)):
        v = x[i - 1] - x[i]  # trading rate per slice (Δt=1)
        exp_cost += eta * v * v
        variance += sigma * sigma * x[i - 1] * x[i - 1]
    return exp_cost, variance


def almgren_chriss_trajectory(
    total_shares: float,
    n_slices: int,
    eta: float = 0.1,
    sigma: float = 0.3,
    risk_aversion: float = 0.0,
) -> list[float]:
    """Optimal (risk-averse) liquidation trajectory.

    ``x_t = X · (T − t)/(T + κ)`` for the risk-averse case, where ``κ`` is the
    risk-aversion adjustment derived from λ; for ``λ = 0`` (risk-neutral) it
    degenerates to a straight line ``x_t = X·(T−t)/T`` (linear VWAP). We always
    include the start ``x_0 = X`` and end ``x_T = 0``.
    """
    if total_shares < 0:
        raise ValueError("total_shares must be >=0")
    if n_slices < 1:
        raise ValueError("n_slices must be >=1")
    if eta <= 0:
        raise ValueError("eta must be >0")
    T = n_slices
    if risk_aversion <= 0.0:
        # risk-neutral: linear liquidation
        return [total_shares * (T - t) / T for t in range(T + 1)]
    # κ = λ·σ²/η ; larger κ → faster early liquidation (more concave trajectory)
    kappa = risk_aversion * sigma * sigma / eta
    denom = T + kappa
    if denom <= 0:
        return [total_shares * (T - t) / T for t in range(T + 1)]
    # x_t = X * sinh(kappa*(T-t)/T) / sinh(kappa)   (continuous soln discretized)
    # simpler discrete AC form: x_t = X * (T - t) / (T + kappa_adj)
    # We use the sinh form (the textbook continuous-solution discretization).
    sinh_kappa = math.sinh(kappa) if kappa < 50 else math.exp(kappa) / 2
    if sinh_kappa == 0:
        return [total_shares * (T - t) / T for t in range(T + 1)]
    return [
        total_shares * math.sinh(kappa * (T - t) / T) / sinh_kappa
        for t in range(T + 1)
    ]


@dataclass(frozen=True)
class AlmgrenChrissResult:
    trajectory: list[float]
    expected_cost: float
    risk: float
    risk_aversion: float
    n_slices: int
    total_shares: float
    eta: float
    sigma: float

    def to_dict(self) -> dict:
        return {
            "trajectory": self.trajectory,
            "expected_cost": self.expected_cost,
            "risk": self.risk,
            "risk_aversion": self.risk_aversion,
            "n_slices": self.n_slices,
            "total_shares": self.total_shares,
            "eta": self.eta,
            "sigma": self.sigma,
        }


def almgren_chriss(
    total_shares: float,
    n_slices: int,
    eta: float = 0.1,
    sigma: float = 0.3,
    risk_aversion: float = 0.0,
) -> AlmgrenChrissResult:
    """Full Almgren-Chriss optimal execution: trajectory + cost + risk."""
    traj = almgren_chriss_trajectory(total_shares, n_slices, eta, sigma, risk_aversion)
    ec, var = execution_cost(traj, eta, sigma)
    return AlmgrenChrissResult(
        trajectory=traj,
        expected_cost=ec,
        risk=math.sqrt(max(var, 0.0)),
        risk_aversion=risk_aversion,
        n_slices=n_slices,
        total_shares=total_shares,
        eta=eta,
        sigma=sigma,
    )


def efficient_frontier(
    total_shares: float,
    n_slices: int,
    eta: float = 0.1,
    sigma: float = 0.3,
    risk_aversions: Sequence[float] | None = None,
) -> list[dict[str, float]]:
    """Sweep risk-aversion λ and return the (expected_cost, risk) frontier.

    Larger λ → faster liquidation → higher impact cost, lower timing risk.
    """
    if risk_aversions is None:
        risk_aversions = [0.0, 0.1, 0.5, 1.0, 5.0, 20.0, 100.0]
    out: list[dict[str, float]] = []
    for lam in risk_aversions:
        res = almgren_chriss(total_shares, n_slices, eta=eta, sigma=sigma, risk_aversion=lam)
        out.append({
            "risk_aversion": lam,
            "expected_cost": res.expected_cost,
            "risk": res.risk,
        })
    return out