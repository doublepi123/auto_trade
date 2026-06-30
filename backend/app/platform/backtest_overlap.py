"""P379: Backtest overlap analysis.

Computes the effective number of independent trials given rolling-window
backtest design with overlapping windows. Provides a PBO-style bias
correction factor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["BacktestOverlapResult", "backtest_overlap_report"]


@dataclass(frozen=True)
class BacktestOverlapResult:
    n_trials: int
    overlap_rate: float
    effective_sample_size: int
    bias_adjustment: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trials": self.n_trials,
            "overlap_rate": self.overlap_rate,
            "effective_sample_size": self.effective_sample_size,
            "bias_adjustment": self.bias_adjustment,
        }


def backtest_overlap_report(
    train_window: int,
    test_window: int,
    total_periods: int,
    *,
    step: int = 1,
) -> BacktestOverlapResult:
    """Compute overlap statistics for rolling-window backtest design.

    Args:
        train_window: Number of periods in each IS (train) window.
        test_window: Number of periods in each OOS (test) window.
        total_periods: Total number of available data periods.
        step: Step size between consecutive windows (default 1).

    Returns:
        BacktestOverlapResult with n_trials, overlap_rate,
        effective_sample_size, and bias_adjustment.

    Raises:
        ValueError: If parameters are invalid.
    """
    for name, val in (
        ("train_window", train_window),
        ("test_window", test_window),
        ("total_periods", total_periods),
        ("step", step),
    ):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"{name} must be a positive int")
        if val <= 0:
            raise ValueError(f"{name} must be positive")

    if train_window + test_window > total_periods:
        raise ValueError(
            f"train_window ({train_window}) + test_window ({test_window}) "
            f"must not exceed total_periods ({total_periods})"
        )

    # Number of effective independent trials
    # n_trials = (total - train - test) / step + 1
    n_trials = (total_periods - train_window - test_window) // step + 1

    # Overlap rate: proportion of overlapping data between consecutive trials
    # If step < test_window, adjacent test windows share data
    if n_trials > 1:
        overlap_fraction_per_trial = max(0.0, 1.0 - step / test_window)
    else:
        overlap_fraction_per_trial = 0.0

    # Overall overlap rate: 1 - effective_independent_ratio
    # conservative estimate based on the overlap fraction
    overlap_rate = overlap_fraction_per_trial

    # Effective sample size: discount n_trials by overlap
    # EES = n_trials / (1 + (n_trials - 1) * rho), rho is average correlation
    # Use the overlap fraction as proxy for correlation
    if n_trials > 1 and overlap_fraction_per_trial > 0:
        effective_sample_size = int(
            n_trials / (1.0 + (n_trials - 1) * overlap_fraction_per_trial)
        )
    else:
        effective_sample_size = n_trials

    effective_sample_size = max(1, effective_sample_size)

    # Bias adjustment: sqrt(effective_sample_size / n_trials)
    # This is the PBO-style correction factor for inflated t-stats
    if n_trials > 0:
        bias_adjustment = math.sqrt(effective_sample_size / n_trials)
    else:
        bias_adjustment = 1.0

    return BacktestOverlapResult(
        n_trials=int(n_trials),
        overlap_rate=float(overlap_rate),
        effective_sample_size=int(effective_sample_size),
        bias_adjustment=float(bias_adjustment),
    )
