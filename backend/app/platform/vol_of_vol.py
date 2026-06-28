"""P336: Volatility-of-Volatility Report — measure volatility persistence and regime.

Computes realized volatility sequences across multiple windows, then measures
the volatility of those volatilities (VoV). Higher VoV indicates less stable
market conditions. Also provides term-structure slope and autocorrelation.

Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VolOfVolResult:
    """Volatility-of-volatility report for a single return series."""
    per_window: dict[int, dict[str, float]]
    vov_term_structure_slope: float | None
    autocorr_lag1: float

    def to_dict(self) -> dict[str, object]:
        return {
            "per_window": {
                str(w): {
                    "vol_of_vol": entry["vol_of_vol"],
                    "mean_realized_vol": entry["mean_realized_vol"],
                    "vol_of_vol_annualized": entry["vol_of_vol_annualized"],
                }
                for w, entry in self.per_window.items()
            },
            "vov_term_structure_slope": self.vov_term_structure_slope,
            "autocorr_lag1": self.autocorr_lag1,
        }


def _validate_returns(returns: list[float]) -> None:
    if not returns:
        raise ValueError("returns must be a non-empty list")
    for v in returns:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            raise ValueError(f"returns contains non-finite value: {v}")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _autocorr_lag1(values: list[float]) -> float:
    """Compute lag-1 autocorrelation."""
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    num = sum((values[t] - m) * (values[t + 1] - m) for t in range(n - 1))
    den = sum((values[t] - m) ** 2 for t in range(n))
    if den == 0:
        return 0.0
    return num / den


def vol_of_vol_report(
    returns: list[float],
    *,
    windows: list[int] | None = None,
    periods_per_year: int = 252,
) -> VolOfVolResult:
    """Compute volatility-of-volatility across multiple rolling windows.

    For each window w, computes the realized volatility series RV_t = std(returns[t-w:t]),
    then VoV_w = std(RV). Higher VoV means more volatile volatility (regime instability).

    Args:
        returns: List of period returns.
        windows: List of window sizes. Default [10, 20, 60].
        periods_per_year: Annualization factor.

    Returns:
        VolOfVolResult with per_window diagnostics, term-structure slope, and autocorr_lag1.
    """
    _validate_returns(returns)

    if windows is None:
        windows = [10, 20, 60]
    if not windows:
        raise ValueError("windows must be a non-empty list")

    n = len(returns)
    per_window: dict[int, dict[str, float]] = {}
    longest_rv: list[float] | None = None
    longest_window = max(windows)

    for w in sorted(windows):
        if w < 2:
            raise ValueError(f"window {w} must be >= 2")
        if w > n:
            raise ValueError(f"window {w} exceeds returns length {n}")

        # Compute realized volatility series
        rv: list[float] = []
        for t in range(w, n + 1):
            window_returns = returns[t - w : t]
            rv.append(_std(window_returns))

        if not rv:
            # Should not happen if w <= n
            mean_rv = 0.0
            vov = 0.0
            vov_annualized = 0.0
        else:
            mean_rv = _mean(rv)
            vov = _std(rv)
            vov_annualized = vov * math.sqrt(periods_per_year)

        per_window[w] = {
            "vol_of_vol": vov,
            "mean_realized_vol": mean_rv,
            "vol_of_vol_annualized": vov_annualized,
        }

        if w == longest_window:
            longest_rv = rv

    # Term-structure slope: regress VoV on window size
    sorted_windows = sorted(windows)
    if len(sorted_windows) >= 2:
        w_mean = _mean([float(w) for w in sorted_windows])
        vov_mean = _mean([per_window[w]["vol_of_vol"] for w in sorted_windows])
        num = sum(
            (w - w_mean) * (per_window[w]["vol_of_vol"] - vov_mean)
            for w in sorted_windows
        )
        den = sum((w - w_mean) ** 2 for w in sorted_windows)
        vov_term_structure_slope = num / den if den != 0 else 0.0
    else:
        vov_term_structure_slope = None

    # Autocorr of longest-window RV
    autocorr_lag1_val = 0.0
    if longest_rv is not None and len(longest_rv) >= 2:
        autocorr_lag1_val = _autocorr_lag1(longest_rv)

    return VolOfVolResult(
        per_window=per_window,
        vov_term_structure_slope=vov_term_structure_slope,
        autocorr_lag1=autocorr_lag1_val,
    )
