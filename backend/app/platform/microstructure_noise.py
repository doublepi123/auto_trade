"""P382: Microstructure Noise — realized-variance signature analysis.

Uses the realised variance (RV) frequency signature to estimate the noise
variance, signal variance, signal-to-noise ratio (SNR), and the optimal
sampling frequency.

Reference: Bandi & Russell (2006, 2008); Aït-Sahalia, Mykland & Zhang (2005).
Pure Python, deterministic. Frozen dataclass result with to_dict().
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MicrostructureNoiseResult",
    "microstructure_noise_report",
]

_DEFAULT_FREQUENCIES = (1, 2, 5, 10, 20)
_MAX_SERIES = 5000


@dataclass(frozen=True)
class MicrostructureNoiseResult:
    noise_variance: float
    signal_variance: float
    snr: float
    optimal_sampling_freq: int
    noise_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "noise_variance": self.noise_variance,
            "signal_variance": self.signal_variance,
            "snr": self.snr,
            "optimal_sampling_freq": self.optimal_sampling_freq,
            "noise_ratio": self.noise_ratio,
        }


def _validate_prices(prices: list[float]) -> list[float]:
    if not isinstance(prices, list):
        raise ValueError("prices must be a non-empty list of finite numbers")
    if not prices:
        raise ValueError("prices must be a non-empty list of finite numbers")
    if len(prices) > _MAX_SERIES:
        raise ValueError(f"prices must contain at most {_MAX_SERIES} values")
    if len(prices) < 2:
        raise ValueError("prices must contain at least 2 values")
    out: list[float] = []
    for value in prices:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("prices entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("prices entries must be finite positive numbers")
        out.append(number)
    return out


def _log_returns(prices: list[float]) -> list[float]:
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def _realized_variance(returns: list[float], freq: int) -> float:
    """Compute RV as sum of squared block-aggregated returns (Bandi-Russell estimator).

    Returns are aggregated into non-overlapping blocks of size *freq*.
    Only complete blocks are used.
    """
    rv = 0.0
    for i in range(0, len(returns) - freq + 1, freq):
        block_return = sum(returns[i : i + freq])
        rv += block_return * block_return
    return rv


def microstructure_noise_report(
    prices: list[float],
    *,
    frequencies: list[int] | None = None,
) -> MicrostructureNoiseResult:
    """Estimate microstructure noise from price series using RV frequency signature.

    Computes realised variance at multiple sampling frequencies. The noise
    variance is estimated as RV at the highest frequency minus RV at the optimal
    (lowest-variance) frequency. The signal-to-noise ratio is computed from the
    estimated signal and noise variances.

    Args:
        prices: List of positive price observations.
        frequencies: Sampling frequencies (default: [1, 2, 5, 10, 20]).

    Returns:
        MicrostructureNoiseResult with noise/signal variance, SNR, optimal
        frequency, and noise ratio.

    Raises:
        ValueError: If prices or frequencies are invalid.
    """
    validated_prices = _validate_prices(prices)

    if frequencies is None:
        freqs = list(_DEFAULT_FREQUENCIES)
    else:
        if not isinstance(frequencies, list) or not frequencies:
            raise ValueError("frequencies must be a non-empty list of positive ints")
        freqs = []
        for f in frequencies:
            if isinstance(f, bool) or not isinstance(f, int):
                raise ValueError("frequencies entries must be positive ints")
            if f < 1:
                raise ValueError("frequencies entries must be positive ints")
            freqs.append(f)

    returns = _log_returns(validated_prices)

    if not returns:
        return MicrostructureNoiseResult(
            noise_variance=0.0,
            signal_variance=0.0,
            snr=0.0,
            optimal_sampling_freq=1,
            noise_ratio=0.0,
        )

    # Compute RV at each frequency
    rv_values: dict[int, float] = {}
    for f in freqs:
        rv_values[f] = _realized_variance(returns, f)

    # RV₁ is at the highest frequency (smallest int in freqs)
    freq_1 = min(freqs)
    rv_1 = rv_values[freq_1]

    # RV_optimal is the minimum across the tested frequencies
    # (excluding freq_1 if there are other options and it seems noisy)
    # Actually, the Bandi-Russell approach: noise_var ≈ RV₁ - RV_optimal
    # where RV_optimal is the RV at the frequency that minimises variance
    # We find the frequency that gives the minimum RV (as a proxy for
    # the one least contaminated by noise)
    optimal_freq = min(rv_values, key=lambda f: rv_values[f])
    rv_optimal = rv_values[optimal_freq]

    # Noise variance: excess variance at tick frequency
    noise_var = max(rv_1 - rv_optimal, 0.0)

    # Signal variance: approximated by RV at optimal frequency
    signal_var = rv_optimal

    # SNR
    total_var = signal_var + noise_var
    snr = signal_var / total_var if total_var > 0 else 0.0

    # Noise ratio
    noise_ratio = noise_var / total_var if total_var > 0 else 0.0

    return MicrostructureNoiseResult(
        noise_variance=noise_var,
        signal_variance=signal_var,
        snr=snr,
        optimal_sampling_freq=optimal_freq,
        noise_ratio=noise_ratio,
    )
