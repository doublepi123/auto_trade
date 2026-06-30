"""P369: Volatility Signature — realised variance by aggregation frequency.

Analyses how realised variance changes as returns are aggregated into lower
frequencies. Useful for identifying the optimal sampling frequency where
microstructure noise becomes negligible.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["VolatilitySignatureResult", "volatility_signature_report"]


@dataclass(frozen=True)
class VolatilitySignatureResult:
    """Frozen aggregate result of :func:`volatility_signature_report`.

    * ``signature`` — list of {frequency, realized_variance, n_obs} per freq.
    * ``noise_variance_estimate`` — difference between highest- and lowest-frequency RV.
    * ``optimal_frequency`` — the frequency at which RV begins to stabilise.
    """

    signature: list[dict[str, Any]]
    noise_variance_estimate: float
    optimal_frequency: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "noise_variance_estimate": self.noise_variance_estimate,
            "optimal_frequency": self.optimal_frequency,
        }


def _validate_returns(returns: list[float]) -> list[float]:
    """Validate return series and return cleaned copy."""
    if not isinstance(returns, list):
        raise ValueError("returns must be a list")
    if len(returns) < 3:
        raise ValueError("returns must contain at least 3 values")
    for i, r in enumerate(returns):
        if isinstance(r, bool) or not isinstance(r, (int, float)):
            raise ValueError(f"returns[{i}] must be a number")
        if not math.isfinite(float(r)):
            raise ValueError(f"returns[{i}] must be a finite number")
    return [float(r) for r in returns]


def _aggregate_returns(returns: list[float], freq: int) -> list[float]:
    """Aggregate returns into ``freq``-period overlapping or non-overlapping blocks.

    Returns a list of aggregated returns (sum over each block of size freq).
    Only complete blocks are used.
    """
    aggregated: list[float] = []
    for i in range(0, len(returns) - freq + 1, freq):
        block_sum = sum(returns[i : i + freq])
        aggregated.append(block_sum)
    return aggregated


def volatility_signature_report(
    returns: list[float],
    *,
    frequencies: list[int] | None = None,
) -> VolatilitySignatureResult:
    """Compute realised variance signature across aggregation frequencies.

    Parameters
    ----------
    returns : list[float]
        Time-series of asset returns.
    frequencies : list[int] | None
        Aggregation frequencies to evaluate. Defaults to [1, 2, 5, 10, 20].

    Returns
    -------
    VolatilitySignatureResult
    """
    cleaned = _validate_returns(returns)
    if frequencies is None:
        frequencies = [1, 2, 5, 10, 20]

    if not isinstance(frequencies, list) or not frequencies:
        raise ValueError("frequencies must be a non-empty list of positive integers")

    # Sort frequencies ascending
    sorted_freqs = sorted(set(frequencies))

    signature: list[dict[str, Any]] = []
    for freq in sorted_freqs:
        if not isinstance(freq, int) or isinstance(freq, bool) or freq < 1:
            raise ValueError(f"frequency {freq} must be a positive integer")
        if freq > len(cleaned):
            raise ValueError(
                f"frequency {freq} exceeds return series length {len(cleaned)}"
            )

        agg = _aggregate_returns(cleaned, freq)
        n_obs = len(agg)
        if n_obs < 2:
            raise ValueError(
                f"frequency {freq} yields fewer than 2 observations"
            )

        mean_agg = sum(agg) / n_obs
        realized_variance = sum((a - mean_agg) ** 2 for a in agg) / (n_obs - 1)

        signature.append({
            "frequency": freq,
            "realized_variance": realized_variance,
            "n_obs": n_obs,
        })

    # Noise variance estimate: highest-freq RV minus lowest-freq RV
    rv_highest = signature[0]["realized_variance"]
    rv_lowest = signature[-1]["realized_variance"]
    noise_variance_estimate = max(0.0, rv_highest - rv_lowest)

    # Optimal frequency: the first frequency where RV stabilises
    # (where the change from previous frequency is below a threshold)
    optimal_frequency = signature[0]["frequency"]
    threshold = 0.15  # 15% change threshold for stabilisation
    for i in range(1, len(signature)):
        prev_rv = signature[i - 1]["realized_variance"]
        curr_rv = signature[i]["realized_variance"]
        # Check if relative change is small for two consecutive steps
        if prev_rv > 1e-15:
            change = abs(curr_rv - prev_rv) / prev_rv
            if change < threshold and i + 1 < len(signature):
                next_rv = signature[i + 1]["realized_variance"]
                if prev_rv > 1e-15:
                    next_change = abs(next_rv - curr_rv) / curr_rv
                    if next_change < threshold:
                        optimal_frequency = signature[i]["frequency"]
                        break

    return VolatilitySignatureResult(
        signature=signature,
        noise_variance_estimate=noise_variance_estimate,
        optimal_frequency=optimal_frequency,
    )
