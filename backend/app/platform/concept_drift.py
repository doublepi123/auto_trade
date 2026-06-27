"""P317: Concept drift detection via non-overlapping window mean-shift scoring.

Pure-Python drift detector: partitions the series into non-overlapping windows,
computes each window's mean, and flags windows where the absolute change from
the previous window's mean exceeds ``threshold * overall_std``. No numpy/scipy/
pandas dependency — every statistic is computed with elementary arithmetic.

Public surface
--------------

* **concept_drift_report(values, window, threshold)** → frozen
  :class:`ConceptDriftResult` with ``drift_points`` (list of indices belonging
  to windows where drift was detected) and per-bar ``drift_scores`` (float list
  of length n).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import std, validate_series

__all__ = ["ConceptDriftResult", "concept_drift_report"]


@dataclass(frozen=True)
class ConceptDriftResult:
    drift_points: list[int]
    drift_scores: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {"drift_points": list(self.drift_points), "drift_scores": list(self.drift_scores)}


def concept_drift_report(values: list[float], *, window: int = 20, threshold: float = 2.0) -> ConceptDriftResult:
    series = validate_series(values, name="values", min_len=1)
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError("window must be an int >= 2")
    if len(series) <= window:
        raise ValueError("series must contain more than window values")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a finite number")
    threshold_f = float(threshold)
    if not math.isfinite(threshold_f):
        raise ValueError("threshold must be a finite number")

    n = len(series)
    overall_std = std(series, sample=False)

    scores: list[float] = [0.0] * n
    drift_points: list[int] = []

    if overall_std == 0.0:
        return ConceptDriftResult(drift_points=[], drift_scores=scores)

    # Partition into non-overlapping windows and compute means.
    window_means: list[float] = []
    window_starts: list[int] = []
    for start in range(0, n, window):
        end = min(start + window, n)
        w_vals = series[start:end]
        w_mean = sum(w_vals) / len(w_vals)
        window_means.append(w_mean)
        window_starts.append(start)

    # Compare each window (after the first) to its predecessor.
    for wi in range(1, len(window_means)):
        diff = abs(window_means[wi] - window_means[wi - 1])
        score = diff / overall_std
        start = window_starts[wi]
        end = min(start + window, n)
        for idx in range(start, end):
            scores[idx] = score
        if score >= threshold_f:
            for idx in range(start, end):
                drift_points.append(idx)

    return ConceptDriftResult(drift_points=drift_points, drift_scores=scores)
