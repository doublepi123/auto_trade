"""P343: Options Greeks surface — Delta/Gamma/Vega/Theta across strikes and expiries.

Computes Black-Scholes Greeks for a list of European options and aggregates a
surface summary (ATM delta, total gamma, total vega). Pure Python, no scipy.
Reuses :mod:`app.platform.options_pricing` for the Black-Scholes Greeks engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.platform._math_utils import norm_cdf, norm_pdf

__all__ = ["GreeksSurfaceResult", "greeks_surface_report"]


def _black_scholes_greeks(
    spot: float,
    strike: float,
    expiry_years: float,
    risk_free: float,
    iv: float,
    option_type: str,
) -> dict[str, float]:
    """Compute delta, gamma, vega, theta for a single European option.

    Pure Python implementation using the standard Black-Scholes formulas
    with zero dividend yield (q=0).
    """
    if expiry_years <= 0.0:
        raise ValueError("expiry must be positive")
    if iv <= 0.0:
        raise ValueError("volatility must be positive")
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")

    sqrt_t = math.sqrt(expiry_years)
    d1 = (math.log(spot / strike) + (risk_free + 0.5 * iv * iv) * expiry_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t

    disc_r = math.exp(-risk_free * expiry_years)
    pdf_d1 = norm_pdf(d1)

    # gamma and vega are type-independent
    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t  # per 1.0 (100%) change in vol

    if option_type == "call":
        delta = norm_cdf(d1)
        theta = (
            -spot * pdf_d1 * iv / (2.0 * sqrt_t)
            - risk_free * strike * disc_r * norm_cdf(d2)
        )
    elif option_type == "put":
        delta = -norm_cdf(-d1)
        theta = (
            -spot * pdf_d1 * iv / (2.0 * sqrt_t)
            + risk_free * strike * disc_r * norm_cdf(-d2)
        )
    else:
        raise ValueError("option_type must be 'call' or 'put'")

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


@dataclass(frozen=True)
class GreeksSurfaceResult:
    greeks: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "greeks": self.greeks,
            "summary": self.summary,
        }


def greeks_surface_report(
    options: list[dict],
    *,
    spot: float,
    risk_free: float = 0.02,
) -> GreeksSurfaceResult:
    """Compute Black-Scholes Greeks for a list of options and surface summary.

    Args:
        options: List of dicts, each with ``strike`` (float), ``expiry_days`` (int),
            ``iv`` (float, implied volatility), and ``option_type`` ("call" or "put").
        spot: Underlying spot price (must be a finite positive number).
        risk_free: Continuous risk-free rate (annualised, default 0.02).

    Returns:
        GreeksSurfaceResult with per-option greeks and surface summary.

    Raises:
        ValueError: On invalid/missing inputs.
    """
    if not isinstance(options, list) or not options:
        raise ValueError("options must be a non-empty list")
    if not math.isfinite(spot) or spot <= 0.0:
        raise ValueError("spot must be a finite positive number")
    if not math.isfinite(risk_free):
        raise ValueError("risk_free must be a finite number")

    greeks_list: list[dict[str, Any]] = []
    total_gamma = 0.0
    total_vega = 0.0
    atm_delta = 0.0
    atm_distance = float("inf")

    for i, opt in enumerate(options):
        if not isinstance(opt, dict):
            raise ValueError(f"options[{i}] must be a dict")
        for key in ("strike", "expiry_days", "iv", "option_type"):
            if key not in opt:
                raise ValueError(f"options[{i}] missing required key '{key}'")

        strike = float(opt["strike"])
        expiry_days = int(opt["expiry_days"])
        iv = float(opt["iv"])
        option_type = str(opt["option_type"])

        if not math.isfinite(strike) or strike <= 0.0:
            raise ValueError(f"options[{i}].strike must be a finite positive number")
        if not isinstance(opt["expiry_days"], int) or expiry_days <= 0:
            raise ValueError(f"options[{i}].expiry_days must be a positive int")
        if not math.isfinite(iv) or iv <= 0.0:
            raise ValueError(f"options[{i}].iv must be a finite positive number")
        if option_type not in ("call", "put"):
            raise ValueError(f"options[{i}].option_type must be 'call' or 'put'")

        expiry_years = expiry_days / 365.0
        g = _black_scholes_greeks(spot, strike, expiry_years, risk_free, iv, option_type)

        entry: dict[str, Any] = {
            "strike": strike,
            "expiry": float(expiry_days),
            "type": option_type,
            "delta": g["delta"],
            "gamma": g["gamma"],
            "vega": g["vega"],
            "theta": g["theta"],
        }
        greeks_list.append(entry)

        total_gamma += abs(g["gamma"])
        total_vega += abs(g["vega"])

        # Track the closest-to-ATM option for atm_delta
        dist = abs(strike - spot)
        if dist < atm_distance:
            atm_distance = dist
            atm_delta = g["delta"]

    summary = {
        "atm_delta": atm_delta,
        "total_gamma": total_gamma,
        "total_vega": total_vega,
    }

    return GreeksSurfaceResult(greeks=greeks_list, summary=summary)
