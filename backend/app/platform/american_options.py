"""P253: American option pricing via the Cox-Ross-Rubinstein binomial tree.

The CRR (1979) binomial model discretises the risk-neutral geometric Brownian
motion over ``N`` steps; the option value is found by backward induction,
checking for early exercise at every node — the defining feature of American
options. Pure Python, no scipy/numpy.

Conventions
-----------
* ``spot`` S, ``strike`` K, ``time_to_expiry`` T (years), ``risk_free`` r,
  ``volatility`` σ, ``dividend_yield`` q (continuous), steps ``N``.
* Up/down factors ``u = exp(σ√dt)``, ``d = 1/u``, risk-neutral probability
  ``p = (exp((r−q)dt) − d) / (u − d)``.
* Terminal payoff intrinsic value; backward induction discounts by
  ``exp(−r dt)`` and takes ``max(cont, intrinsic)`` at each node for American
  exercise (European skips the intrinsic check).

Reference: Cox-Ross-Rubinstein (1979) "Option Pricing: A Simplified
Approach"; Hull ch. 21. Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ExerciseStyle",
    "BinomialResult",
    "binomial_price",
    "american_option_price",
    "european_option_price",
]

ExerciseStyle = str  # "american" | "european"
OptionType = str  # "call" | "put"


def _intrinsic(option_type: OptionType, s: float, k: float) -> float:
    if option_type == "call":
        return max(s - k, 0.0)
    if option_type == "put":
        return max(k - s, 0.0)
    raise ValueError("option_type must be 'call' or 'put'")


def binomial_price(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    *,
    steps: int = 200,
    dividend_yield: float = 0.0,
    exercise: ExerciseStyle = "american",
) -> BinomialResult:
    """Price an option on the CRR binomial tree.

    ``exercise`` may be ``"american"`` (early-exercise check at every node) or
    ``"european"`` (continuation value only). Raises ``ValueError`` on
    non-positive S/K/T/σ or non-positive steps.
    """
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    if time_to_expiry <= 0.0:
        raise ValueError("time_to_expiry must be positive")
    if volatility <= 0.0:
        raise ValueError("volatility must be positive")
    if steps < 1:
        raise ValueError("steps must be >= 1")
    if option_type not in ("call", "put"):
        raise ValueError("option_type must be 'call' or 'put'")
    if exercise not in ("american", "european"):
        raise ValueError("exercise must be 'american' or 'european'")

    dt = time_to_expiry / steps
    u = math.exp(volatility * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-risk_free * dt)
    growth = math.exp((risk_free - dividend_yield) * dt)
    p = (growth - d) / (u - d)
    if not 0.0 < p < 1.0:
        # Arbitrage-bound violation (r−q out of [d−1, u−1]); clamp & warn via result.
        p = max(0.0, min(1.0, p))

    # Terminal asset prices S·u^i·d^(N-i) for i=0..N.
    prices = [spot * (u ** i) * (d ** (steps - i)) for i in range(steps + 1)]
    # Terminal option values = intrinsic.
    values = [_intrinsic(option_type, prices[i], strike) for i in range(steps + 1)]

    # Backward induction.
    early_exercise_nodes = 0
    for step in range(steps - 1, -1, -1):
        for i in range(step + 1):
            cont = disc * (p * values[i + 1] + (1.0 - p) * values[i])
            if exercise == "american":
                s_node = spot * (u ** i) * (d ** (step - i))
                intr = _intrinsic(option_type, s_node, strike)
                if intr > cont:
                    values[i] = intr
                    early_exercise_nodes += 1
                else:
                    values[i] = cont
            else:
                values[i] = cont

    return BinomialResult(
        option_type=option_type,
        exercise=exercise,
        price=values[0],
        spot=spot,
        strike=strike,
        time_to_expiry=time_to_expiry,
        risk_free=risk_free,
        volatility=volatility,
        dividend_yield=dividend_yield,
        steps=steps,
        risk_neutral_prob=p,
        up_factor=u,
        down_factor=d,
        early_exercise_nodes=early_exercise_nodes,
    )


@dataclass(frozen=True)
class BinomialResult:
    option_type: str
    exercise: str
    price: float
    spot: float
    strike: float
    time_to_expiry: float
    risk_free: float
    volatility: float
    dividend_yield: float
    steps: int
    risk_neutral_prob: float
    up_factor: float
    down_factor: float
    early_exercise_nodes: int

    def to_dict(self) -> dict:
        return {
            "option_type": self.option_type,
            "exercise": self.exercise,
            "price": self.price,
            "spot": self.spot,
            "strike": self.strike,
            "time_to_expiry": self.time_to_expiry,
            "risk_free": self.risk_free,
            "volatility": self.volatility,
            "dividend_yield": self.dividend_yield,
            "steps": self.steps,
            "risk_neutral_prob": self.risk_neutral_prob,
            "up_factor": self.up_factor,
            "down_factor": self.down_factor,
            "early_exercise_nodes": self.early_exercise_nodes,
        }


def american_option_price(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    *,
    steps: int = 200,
    dividend_yield: float = 0.0,
) -> float:
    """Convenience wrapper returning the American option price."""
    return binomial_price(
        option_type, spot, strike, time_to_expiry, risk_free, volatility,
        steps=steps, dividend_yield=dividend_yield, exercise="american",
    ).price


def european_option_price(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    *,
    steps: int = 200,
    dividend_yield: float = 0.0,
) -> float:
    """Convenience wrapper returning the European (binomial) option price."""
    return binomial_price(
        option_type, spot, strike, time_to_expiry, risk_free, volatility,
        steps=steps, dividend_yield=dividend_yield, exercise="european",
    ).price