"""P375: Implied risk-free rate from put-call parity.

Pure-Python implementation that derives the risk-free rate implied by a set
of European call and put option prices at the same strikes via put-call
parity: C - P = S - K * e^{-rT}.

Reference: Hull, J. C. (2022). "Options, Futures, and Other Derivatives".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrikeResult:
    """Per-strike implied risk-free rate result."""

    strike: float
    implied_r: float
    deviation: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strike": self.strike,
            "implied_r": self.implied_r,
            "deviation": self.deviation,
        }


@dataclass(frozen=True)
class ImpliedRiskFreeRateResult:
    """Frozen carrier for implied risk-free rate results."""

    per_strike: list[StrikeResult]
    median_implied_r: float
    mean_implied_r: float
    consensus_r: float
    outliers: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_strike": [sr.to_dict() for sr in self.per_strike],
            "median_implied_r": self.median_implied_r,
            "mean_implied_r": self.mean_implied_r,
            "consensus_r": self.consensus_r,
            "outliers": self.outliers,
        }


def _validate_pcp_inputs(
    call_prices: dict[float, float],
    put_prices: dict[float, float],
    spot: float,
    expiry: float,
) -> tuple[list[tuple[float, float, float]], float, float]:
    """Validate all inputs and return aligned (strike, call, put) triples."""
    if not isinstance(call_prices, dict) or not call_prices:
        raise ValueError("call_prices must be a non-empty dict of {strike: price}")
    if not isinstance(put_prices, dict) or not put_prices:
        raise ValueError("put_prices must be a non-empty dict of {strike: price}")

    if isinstance(spot, bool) or not isinstance(spot, (int, float)):
        raise ValueError("spot must be a finite number")
    spot_f = float(spot)
    if not math.isfinite(spot_f) or spot_f <= 0:
        raise ValueError("spot must be a finite positive number")

    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise ValueError("expiry must be a finite number")
    expiry_f = float(expiry)
    if not math.isfinite(expiry_f) or expiry_f <= 0:
        raise ValueError("expiry must be a finite positive number")

    # Find common strikes
    call_strikes: set[float] = set()
    for k, v in call_prices.items():
        if isinstance(k, bool) or not isinstance(k, (int, float)):
            raise ValueError("call_prices keys must be numbers")
        kf = float(k)
        if not math.isfinite(kf) or kf <= 0:
            raise ValueError("call_prices keys must be finite positive numbers")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("call_prices values must be finite numbers")
        vf = float(v)
        if not math.isfinite(vf) or vf < 0:
            raise ValueError("call_prices values must be finite non-negative numbers")
        call_strikes.add(kf)

    put_strikes: set[float] = set()
    for k, v in put_prices.items():
        if isinstance(k, bool) or not isinstance(k, (int, float)):
            raise ValueError("put_prices keys must be numbers")
        kf = float(k)
        if not math.isfinite(kf) or kf <= 0:
            raise ValueError("put_prices keys must be finite positive numbers")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("put_prices values must be finite numbers")
        vf = float(v)
        if not math.isfinite(vf) or vf < 0:
            raise ValueError("put_prices values must be finite non-negative numbers")
        put_strikes.add(kf)

    common_strikes = sorted(call_strikes & put_strikes)
    if not common_strikes:
        raise ValueError("call_prices and put_prices must share at least one common strike")

    # Build triples
    triples: list[tuple[float, float, float]] = []
    for k in common_strikes:
        triples.append((k, call_prices[k], put_prices[k]))

    return triples, spot_f, expiry_f


def implied_risk_free_rate_report(
    call_prices: dict[float, float],
    put_prices: dict[float, float],
    *,
    spot: float,
    expiry: float,
) -> ImpliedRiskFreeRateResult:
    """Compute implied risk-free rate from put-call parity.

    Parameters
    ----------
    call_prices:
        Dict mapping strike to call option price.
    put_prices:
        Dict mapping strike to put option price.
    spot:
        Current spot price of the underlying asset.
    expiry:
        Time to expiry in years (T).

    Returns
    -------
    ImpliedRiskFreeRateResult with per-strike implied rates and consensus.
    """
    triples, spot_f, expiry_f = _validate_pcp_inputs(
        call_prices, put_prices, spot, expiry
    )

    epsilon = 1e-12
    implied_rates: list[float] = []
    per_strike: list[StrikeResult] = []

    for strike, call, put in triples:
        # PCP: C - P = S - K * e^{-rT}
        # => K * e^{-rT} = S - C + P
        # => e^{-rT} = (S - C + P) / K
        rhs = spot_f - call + put
        if rhs <= 0 or strike <= 0:
            continue

        ratio = rhs / strike
        if ratio <= 0:
            continue

        # r = -ln(ratio) / T
        implied_r = -math.log(max(ratio, epsilon)) / expiry_f
        if not math.isfinite(implied_r):
            continue
        implied_rates.append(implied_r)

    if not implied_rates:
        raise ValueError(
            "No valid strike produced a finite implied rate; "
            "check that put-call parity is not violated"
        )

    # Sort for median
    sorted_r = sorted(implied_rates)
    n_rates = len(sorted_r)
    if n_rates % 2 == 1:
        median_r = sorted_r[n_rates // 2]
    else:
        median_r = (sorted_r[n_rates // 2 - 1] + sorted_r[n_rates // 2]) / 2.0

    mean_r = sum(implied_rates) / n_rates

    # Outliers: |r - median| > 0.05
    outliers: list[float] = []
    for r in implied_rates:
        if abs(r - median_r) > 0.05:
            outliers.append(r)

    # Consensus rate: mean after removing outliers
    if len(outliers) < len(implied_rates):
        inlier_rates = [r for r in implied_rates if abs(r - median_r) <= 0.05]
        consensus_r = sum(inlier_rates) / len(inlier_rates) if inlier_rates else median_r
    else:
        consensus_r = median_r

    # Build per_strike with deviation from median
    for strike, _, _ in triples:
        rhs = spot_f - call_prices.get(strike, 0.0) + put_prices.get(strike, 0.0)
        if rhs <= 0 or strike <= 0:
            continue
        ratio = rhs / strike
        if ratio <= 0:
            continue
        r = -math.log(max(ratio, epsilon)) / expiry_f
        if not math.isfinite(r):
            continue
        deviation = r - median_r
        per_strike.append(StrikeResult(strike=strike, implied_r=r, deviation=deviation))

    return ImpliedRiskFreeRateResult(
        per_strike=per_strike,
        median_implied_r=median_r,
        mean_implied_r=mean_r,
        consensus_r=consensus_r,
        outliers=sorted(outliers),
    )
