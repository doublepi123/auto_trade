"""P365: Variance break detection via ICSS (Inclán-Tiao 1994) algorithm.

Pure-Python implementation of the Iterated Cumulative Sums of Squares (ICSS)
algorithm for detecting variance change points in a return series. Uses
recursive binary segmentation with an approximate critical value for the
D_k statistic.

Reference: Inclán & Tiao (1994) "Use of Cumulative Sums of Squares for
Retrospective Detection of Changes of Variance".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VarianceBreakResult:
    """Frozen carrier for ICSS variance-break detection results."""

    break_points: list[int]
    variance_ratios: list[float]
    icss_statistics: list[float]
    pre_post_stats: dict[str, dict[str, Any]]
    n_observations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "break_points": self.break_points,
            "variance_ratios": self.variance_ratios,
            "icss_statistics": self.icss_statistics,
            "pre_post_stats": self.pre_post_stats,
            "n_observations": self.n_observations,
        }


def _validate_returns(returns: list[float], min_segment: int) -> list[float]:
    """Validate and return the returns series."""
    if not isinstance(returns, list) or not returns:
        raise ValueError("returns must be a non-empty list of finite numbers")
    if len(returns) < 2 * min_segment:
        raise ValueError(f"returns must contain at least {2 * min_segment} values")
    validated: list[float] = []
    for v in returns:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("returns entries must be finite numbers")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("returns entries must be finite numbers")
        validated.append(f)
    return validated


def _cumulative_sums_of_squares(returns: list[float]) -> list[float]:
    """Compute the cumulative sum of squares of returns."""
    css = [0.0]
    for r in returns:
        css.append(css[-1] + r * r)
    return css


def _icss_d_statistic(css: list[float], start: int, end: int) -> list[float]:
    """Compute the D_k statistic series for segment [start, end).

    D_k = C_k / C_T - k / T where C_k is the cumulative sum of squares up to k
    within the segment, and T = end - start.

    Returns the list of D_k values for k in [1, T-1].
    """
    T = end - start
    if T < 2:
        return []
    C_T = css[end] - css[start]
    if C_T == 0.0:
        return []
    d_values: list[float] = []
    for k in range(1, T):
        C_k = css[start + k] - css[start]
        d_k = C_k / C_T - k / T
        d_values.append(d_k)
    return d_values


def _icss_critical_value(T: int) -> float:
    """Approximate 95% critical value for the ICSS D_k statistic.

    Uses the asymptotic critical value 1.358/sqrt(T) from the Kolmogorov-Smirnov
    distribution, which is a common approximation for the ICSS test.
    """
    if T < 2:
        return float("inf")
    return 1.358 / math.sqrt(T)


def _detect_single_break(
    returns: list[float], start: int, end: int, min_segment: int
) -> tuple[int | None, float]:
    """Find the single most significant variance break in [start, end).

    Returns (break_index, sup_d) or (None, 0.0) if no significant break found.
    break_index is relative to the full series.
    """
    T = end - start
    if T < 2 * min_segment:
        return None, 0.0

    css = _cumulative_sums_of_squares(returns)
    d_values = _icss_d_statistic(css, start, end)

    if not d_values:
        return None, 0.0

    # Find the maximum absolute D_k
    max_abs_d = 0.0
    max_k = -1
    for k_idx, d in enumerate(d_values):
        abs_d = abs(d)
        if abs_d > max_abs_d:
            max_abs_d = abs_d
            max_k = k_idx

    if max_k < 0:
        return None, 0.0

    # The actual index in the full series is start + max_k + 1
    break_idx = start + max_k + 1

    # Check against critical value
    critical = _icss_critical_value(T)
    if max_abs_d <= critical:
        return None, max_abs_d

    return break_idx, max_abs_d


def _variance_of_segment(returns: list[float], start: int, end: int) -> float:
    """Compute sample variance of returns[start:end]."""
    segment = returns[start:end]
    if len(segment) < 2:
        return 0.0
    mean = sum(segment) / len(segment)
    var = sum((r - mean) ** 2 for r in segment) / (len(segment) - 1)
    return var


def variance_break_report(
    returns: list[float], *, min_segment: int = 10
) -> VarianceBreakResult:
    """Detect variance change points using the ICSS binary segmentation algorithm.

    Parameters
    ----------
    returns:
        List of period returns. Must be non-empty, all values finite.
    min_segment:
        Minimum segment length for detection (default 10).

    Returns
    -------
    VarianceBreakResult with break_points, variance_ratios, icss_statistics,
    and pre_post_stats.
    """
    validated = _validate_returns(returns, min_segment)
    n = len(validated)

    break_points: list[int] = []
    variance_ratios: list[float] = []
    icss_statistics: list[float] = []

    # Recursive binary segmentation queue
    queue: list[tuple[int, int]] = [(0, n)]

    while queue:
        start, end = queue.pop(0)
        bp, stat = _detect_single_break(validated, start, end, min_segment)
        if bp is not None:
            break_points.append(bp)
            icss_statistics.append(stat)
            # Compute variance ratio: post-break variance / pre-break variance
            pre_var = _variance_of_segment(validated, start, bp)
            post_var = _variance_of_segment(validated, bp, end)
            if pre_var > 0:
                variance_ratios.append(post_var / pre_var)
            else:
                variance_ratios.append(float("inf") if post_var > 0 else 1.0)
            # Recurse on both sides
            queue.append((start, bp))
            queue.append((bp, end))

    # Build pre_post_stats for each segment
    # Collect all boundaries and sort
    boundaries = sorted(set([0] + break_points + [n]))
    pre_post_stats: dict[str, dict[str, Any]] = {}
    for i in range(len(boundaries) - 1):
        seg_start = boundaries[i]
        seg_end = boundaries[i + 1]
        seg_var = _variance_of_segment(validated, seg_start, seg_end)
        seg_key = f"segment_{i}"
        pre_post_stats[seg_key] = {
            "start": seg_start,
            "end": seg_end,
            "variance": seg_var,
        }

    # Sort break_points, variance_ratios, and icss_statistics together
    paired = list(zip(break_points, variance_ratios, icss_statistics))
    paired.sort()
    break_points = [p[0] for p in paired]
    variance_ratios = [p[1] for p in paired]
    icss_statistics = [p[2] for p in paired]

    return VarianceBreakResult(
        break_points=break_points,
        variance_ratios=variance_ratios,
        icss_statistics=icss_statistics,
        pre_post_stats=pre_post_stats,
        n_observations=n,
    )
