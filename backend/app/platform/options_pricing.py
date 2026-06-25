"""P243: European option pricing and Greeks via Black-Scholes-Merton.

Closed-form pricing of European call/put options with continuous or discrete
dividend yield, plus the full first- and second-order Greek family. Pure
Python, no scipy/numpy. References QuantLib ``BlackScholesProcess`` and
py_vollib for the abstraction shape; the mathematics is the textbook
Black-Scholes-Merton (1973) with Merton's (1973) continuous-dividend extension.

Conventions
-----------
* ``spot`` S, ``strike`` K, ``time_to_expiry`` T (years, > 0), ``risk_free`` r
  (continuous, annualised), ``volatility`` σ (> 0), ``dividend_yield`` q
  (continuous, annualised, default 0).
* ``call``/``put`` via the ``OptionType`` literal ``"call" | "put"``.
* All Greeks are reported **per unit spot** (i.e. standard quoted form):
  delta, gamma, vega (per 1.0 vol), theta (per year, **not** per day),
  rho (per 1.0 rate), vanna (∂Δ/∂σ), volga / vomma (∂²V/∂σ²).

Forward-style with continuous dividend yield q:

    d1 = (ln(S/K) + (r − q + ½σ²)T) / (σ√T)
    d2 = d1 − σ√T
    call = S e^{−qT} Φ(d1) − K e^{−rT} Φ(d2)
    put  = K e^{−rT} Φ(−d2) − S e^{−qT} Φ(−d1)

Put-call parity: ``call − put = S e^{−qT} − K e^{−rT}``.

Reference: Black & Scholes (1973), Merton (1973). Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform._math_utils import norm_cdf, norm_pdf

__all__ = [
    "OptionType",
    "OptionsResult",
    "black_scholes",
    "greeks",
    "option_price",
]

OptionType = str  # "call" | "put"


def _d1_d2(spot: float, strike: float, t: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    if t <= 0.0:
        raise ValueError("time_to_expiry must be positive")
    if sigma <= 0.0:
        raise ValueError("volatility must be positive")
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def black_scholes(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> float:
    """European call/put price under Black-Scholes-Merton with continuous yield q."""
    d1, d2 = _d1_d2(spot, strike, time_to_expiry, risk_free, volatility, dividend_yield)
    disc_r = math.exp(-risk_free * time_to_expiry)
    disc_q = math.exp(-dividend_yield * time_to_expiry)
    if option_type == "call":
        return spot * disc_q * norm_cdf(d1) - strike * disc_r * norm_cdf(d2)
    if option_type == "put":
        return strike * disc_r * norm_cdf(-d2) - spot * disc_q * norm_cdf(-d1)
    raise ValueError("option_type must be 'call' or 'put'")


def greeks(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> dict[str, Any]:
    """Full Greek family for a European option.

    Returns delta, gamma, vega, theta, rho, vanna, volga. Theta is annualised
    (per year); multiply by 1/365 for per-calendar-day if desired.
    """
    d1, d2 = _d1_d2(spot, strike, time_to_expiry, risk_free, volatility, dividend_yield)
    t = time_to_expiry
    r = risk_free
    sigma = volatility
    q = dividend_yield
    sqrt_t = math.sqrt(t)
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    pdf_d1 = norm_pdf(d1)
    # gamma and vega are type-independent.
    gamma = disc_q * pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t  # per 1.0 change in vol
    # vanna = ∂Δ/∂σ = -disc_q * pdf_d1) * d2 / sigma
    vanna = -disc_q * pdf_d1 * d2 / sigma
    # volga / vomma = ∂²V/∂σ² = vega * d1 * d2 / sigma
    volga = vega * d1 * d2 / sigma
    if option_type == "call":
        delta = disc_q * norm_cdf(d1)
        rho = strike * t * disc_r * norm_cdf(d2)  # per 1.0 rate
        theta = (
            -spot * disc_q * pdf_d1 * sigma / (2.0 * sqrt_t)
            - r * strike * disc_r * norm_cdf(d2)
            + q * spot * disc_q * norm_cdf(d1)
        )
    elif option_type == "put":
        delta = -disc_q * norm_cdf(-d1)
        rho = -strike * t * disc_r * norm_cdf(-d2)
        theta = (
            -spot * disc_q * pdf_d1 * sigma / (2.0 * sqrt_t)
            + r * strike * disc_r * norm_cdf(-d2)
            - q * spot * disc_q * norm_cdf(-d1)
        )
    else:
        raise ValueError("option_type must be 'call' or 'put'")
    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
        "vanna": vanna,
        "volga": volga,
    }


@dataclass(frozen=True)
class OptionsResult:
    option_type: str
    spot: float
    strike: float
    time_to_expiry: float
    risk_free: float
    volatility: float
    dividend_yield: float
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    vanna: float
    volga: float

    def to_dict(self) -> dict:
        return {
            "option_type": self.option_type,
            "spot": self.spot,
            "strike": self.strike,
            "time_to_expiry": self.time_to_expiry,
            "risk_free": self.risk_free,
            "volatility": self.volatility,
            "dividend_yield": self.dividend_yield,
            "price": self.price,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "rho": self.rho,
            "vanna": self.vanna,
            "volga": self.volga,
        }


def option_price(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> OptionsResult:
    """Price + full Greeks aggregated into :class:`OptionsResult`."""
    g = greeks(option_type, spot, strike, time_to_expiry, risk_free, volatility, dividend_yield)
    price = black_scholes(option_type, spot, strike, time_to_expiry, risk_free, volatility, dividend_yield)
    return OptionsResult(
        option_type=option_type,
        spot=spot,
        strike=strike,
        time_to_expiry=time_to_expiry,
        risk_free=risk_free,
        volatility=volatility,
        dividend_yield=dividend_yield,
        price=price,
        delta=g["delta"],
        gamma=g["gamma"],
        vega=g["vega"],
        theta=g["theta"],
        rho=g["rho"],
        vanna=g["vanna"],
        volga=g["volga"],
    )