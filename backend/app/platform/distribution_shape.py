"""P327: Distribution shape — rolling skew, kurtosis, tail index.

Rolling-window characterization of the return distribution's higher moments:
skewness, excess kurtosis, and a Hill-estimator tail index. Identifies
clusters where kurtosis exceeds a threshold, flagging fat-tail regimes.

Reference: Hill (1975) "A Simple General Approach to Inference About the
Tail of a Distribution"; Kim & White (2004) "On More Robust Estimation of
Skewness and Kurtosis". Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DistributionShapeBar:
    start_idx: int
    end_idx: int
    skew: float
    kurtosis: float
    tail_index: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_idx": self.start_idx,
            "end_idx": self.end_idx,
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "tail_index": self.tail_index,
        }


@dataclass(frozen=True)
class DistributionShapeResult:
    n_observations: int
    window: int
    bars: list[DistributionShapeBar]
    fat_tail_clusters: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_observations": self.n_observations,
            "window": self.window,
            "bars": [b.to_dict() for b in self.bars],
            "fat_tail_clusters": self.fat_tail_clusters,
            "summary": self.summary,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std_pop(xs: list[float]) -> float:
    """Population standard deviation."""
    n = len(xs)
    if n < 1:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / n)


def _skewness(xs: list[float]) -> float:
    """Sample skewness (Fisher-Pearson) for a window."""
    n = len(xs)
    if n < 3:
        return 0.0
    m = _mean(xs)
    s = _std_pop(xs)
    if s <= 0:
        return 0.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    return m3 / (s ** 3)


def _excess_kurtosis(xs: list[float]) -> float:
    """Sample excess kurtosis (Fisher's g₂) for a window."""
    n = len(xs)
    if n < 4:
        return 0.0
    m = _mean(xs)
    s = _std_pop(xs)
    if s <= 0:
        return 0.0
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m4 / (s ** 4) - 3.0


def _hill_estimator(xs: list[float], k: int | None = None) -> float:
    """Hill's tail-index estimator on absolute values."""
    x = sorted(abs(r) for r in xs if r != 0)
    n = len(x)
    if n < 4:
        return 0.0
    if k is None:
        k = max(2, int(math.sqrt(n)))
    k = min(k, n - 1)
    threshold = x[n - k - 1]
    if threshold <= 0:
        return 0.0
    log_sum = 0.0
    for i in range(n - k, n):
        if x[i] <= 0:
            continue
        log_sum += math.log(x[i] / threshold)
    if log_sum <= 0:
        return 0.0
    return k / log_sum


def distribution_shape_report(
    returns: list[float],
    *,
    window: int = 20,
) -> DistributionShapeResult:
    """Rolling-window skew, kurtosis, tail-index report.

    Args:
        returns: Period returns list.
        window: Rolling window size (must be >= 4 for kurtosis).

    Returns:
        DistributionShapeResult with per-bar stats, fat-tail clusters, summary.

    Raises:
        ValueError: Empty returns, window < 1, or non-finite values.
    """
    if not returns:
        raise ValueError("returns must be non-empty")
    if window < 1:
        raise ValueError("window must be >= 1")

    returns_f = []
    for v in returns:
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError("returns must contain only finite numbers")
        returns_f.append(fv)

    n = len(returns_f)
    bars: list[DistributionShapeBar] = []
    fat_tail_clusters: list[dict[str, Any]] = []

    # Kurtosis threshold for "fat tail" (Fisher g₂ > 1.0 = leptokurtic)
    kurtosis_threshold = 1.0

    # Rolling window computation
    for start in range(0, n - window + 1):
        end = start + window
        window_data = returns_f[start:end]

        skew = _skewness(window_data)
        kurt = _excess_kurtosis(window_data)
        tail = _hill_estimator(window_data)

        bars.append(DistributionShapeBar(
            start_idx=start,
            end_idx=end,
            skew=skew,
            kurtosis=kurt,
            tail_index=tail,
        ))

    # Find fat-tail clusters: contiguous intervals where kurtosis > threshold
    cluster_start: int | None = None
    for i, bar in enumerate(bars):
        if bar.kurtosis > kurtosis_threshold:
            if cluster_start is None:
                cluster_start = i
        else:
            if cluster_start is not None:
                fat_tail_clusters.append({
                    "bar_start": cluster_start,
                    "bar_end": i,
                    "idx_start": bars[cluster_start].start_idx,
                    "idx_end": bars[i - 1].end_idx,
                    "max_kurtosis": max(bars[j].kurtosis for j in range(cluster_start, i)),
                    "n_bars": i - cluster_start,
                })
                cluster_start = None

    if cluster_start is not None:
        fat_tail_clusters.append({
            "bar_start": cluster_start,
            "bar_end": len(bars),
            "idx_start": bars[cluster_start].start_idx,
            "idx_end": bars[-1].end_idx,
            "max_kurtosis": max(bars[j].kurtosis for j in range(cluster_start, len(bars))),
            "n_bars": len(bars) - cluster_start,
        })

    # Summary
    all_skews = [b.skew for b in bars]
    all_kurts = [b.kurtosis for b in bars]
    all_tails = [b.tail_index for b in bars]

    summary = {
        "n_bars": len(bars),
        "mean_skew": _mean(all_skews),
        "mean_kurtosis": _mean(all_kurts),
        "mean_tail_index": _mean(all_tails),
        "max_kurtosis": max(all_kurts) if all_kurts else 0.0,
        "n_fat_tail_clusters": len(fat_tail_clusters),
    }

    return DistributionShapeResult(
        n_observations=n,
        window=window,
        bars=bars,
        fat_tail_clusters=fat_tail_clusters,
        summary=summary,
    )


__all__ = [
    "DistributionShapeBar",
    "DistributionShapeResult",
    "distribution_shape_report",
]
