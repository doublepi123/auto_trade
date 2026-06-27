"""P261: Change-point detection via binary segmentation.

Pure-Python change-point detector for a uniformly-sampled scalar series. No
numpy/scipy/pandas dependency — every statistic is computed with elementary
arithmetic and prefix sums so split scoring is ``O(n)`` per candidate segment.

Public surface
--------------

* **mean_shift_score(series, index)** — Cohen's-d-style effect size measuring
  the shift in mean across a split at ``index`` (``|m_left − m_right|`` divided
  by the pooled standard deviation of the whole segment).
* **variance_shift_score(series, index)** — the analogous effect size for the
  shift in standard deviation across the split.
* **detect_change_points(series, min_size, max_points, threshold)** — frozen
  :class:`ChangePointResult` aggregating the change points found by recursive
  binary segmentation, a normalised ``confidence`` in ``[0, 1]``, and the
  contiguous ``segments`` implied by the detected change points.

Conventions
-----------

* ``index`` is the boundary position: ``series[:index]`` is the left segment
  and ``series[index:]`` is the right segment.
* The detector is deterministic: there is no randomness and no fitting.
* Following the P260 audit precedent the public surface raises ``ValueError``
  uniformly for invalid arguments so the platform endpoint can map every
  invalid-input case to HTTP 422 without distinguishing ``TypeError`` from
  ``ValueError``.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "ChangePoint",
    "ChangePointResult",
    "detect_change_points",
    "mean_shift_score",
    "variance_shift_score",
]


_MIN_MIN_SIZE = 2
"""Smallest legal ``min_size``; a segment of length < 2 has no variance."""

_EPS = 1e-12
"""Numerical floor guarding against division by a zero standard deviation."""


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _validate_series(series: Sequence[float]) -> list[float]:
    """Coerce ``series`` to ``list[float]`` after validating each entry.

    Raises ``ValueError`` for any invalid input: a non-iterable scalar, an
    empty series, or a non-finite / non-numeric entry. ``bool`` entries are
    rejected explicitly because ``bool`` is a subclass of ``int`` in Python
    and would otherwise be silently coerced to ``1.0`` / ``0.0``.
    """
    if isinstance(series, list):
        materialised = series
    else:
        try:
            materialised = list(series)
        except TypeError as exc:  # pragma: no cover - defensive
            raise ValueError("series must be a sequence of finite numbers") from exc
    if len(materialised) == 0:
        raise ValueError("series must be non-empty")
    coerced: list[float] = []
    for value in materialised:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("series entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("series entries must be finite numbers")
        coerced.append(number)
    return coerced


def _require_int(value: Any, name: str) -> int:
    """Validate an ``int`` parameter, rejecting ``bool`` and non-integer types.

    Raises ``ValueError`` (matching the rest of the public surface) so callers
    map every invalid-argument case to a single exception type.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int")
    return value


# ---------------------------------------------------------------------------
# prefix sums
# ---------------------------------------------------------------------------


def _prefix_sums(series: list[float]) -> tuple[list[float], list[float]]:
    """Return ``(prefix_sum, prefix_sum_of_squares)`` each of length ``n+1``.

    ``prefix_sum[k]`` = sum of ``series[0:k]`` (so ``prefix_sum[0] == 0``).
    Enables ``O(1)`` mean/variance queries over any sub-range.
    """
    n = len(series)
    s = [0.0] * (n + 1)
    sq = [0.0] * (n + 1)
    for i, v in enumerate(series):
        s[i + 1] = s[i] + v
        sq[i + 1] = sq[i] + v * v
    return s, sq


def _segment_stats(prefix_sum: list[float], prefix_sq: list[float], start: int, end: int) -> tuple[float, float]:
    """Return ``(mean, variance)`` of ``series[start:end]`` in ``O(1)``.

    ``variance`` is the biased (population) estimate; returns ``0.0`` for any
    segment of length < 2 (a single sample carries no dispersion).
    """
    length = end - start
    if length <= 0:
        return 0.0, 0.0
    total = prefix_sum[end] - prefix_sum[start]
    total_sq = prefix_sq[end] - prefix_sq[start]
    mean = total / length
    if length < 2:
        return mean, 0.0
    # E[X^2] - (E[X])^2 ; clamp tiny negatives from rounding to zero.
    variance = total_sq / length - mean * mean
    if variance < 0.0:
        variance = 0.0
    return mean, variance


# ---------------------------------------------------------------------------
# split scoring
# ---------------------------------------------------------------------------


def _validate_split_index(index: Any, n: int, min_size: int) -> int:
    """Validate ``index`` as a split point keeping ``min_size`` on each side."""
    index = _require_int(index, "index")
    if index < min_size or index > n - min_size:
        raise ValueError(
            f"index must satisfy {min_size} <= index <= {n - min_size}"
        )
    return index


def _overall_std(prefix_sum: list[float], prefix_sq: list[float], start: int, end: int) -> float:
    """Population standard deviation of ``series[start:end]``."""
    _, var = _segment_stats(prefix_sum, prefix_sq, start, end)
    return math.sqrt(var)


def mean_shift_score(series: Sequence[float], index: int) -> float:
    """Effect size of the mean shift across the split at ``index``.

    The score is a Cohen's-d-style statistic:

        score = |mean_left − mean_right| / (overall_std + eps)

    where ``overall_std`` is the population standard deviation of the **whole**
    segment (left ∪ right). The score is non-negative, zero when the segment
    means coincide, and grows without bound as the means diverge.

    Raises ``ValueError`` if ``series`` is empty, contains a non-finite /
    non-numeric / boolean entry, or ``index`` does not leave at least
    ``_MIN_MIN_SIZE`` samples on each side.
    """
    data = _validate_series(series)
    n = len(data)
    idx = _validate_split_index(index, n, _MIN_MIN_SIZE)
    prefix_sum, prefix_sq = _prefix_sums(data)
    mean_left, _ = _segment_stats(prefix_sum, prefix_sq, 0, idx)
    mean_right, _ = _segment_stats(prefix_sum, prefix_sq, idx, n)
    overall_std = _overall_std(prefix_sum, prefix_sq, 0, n)
    return abs(mean_left - mean_right) / (overall_std + _EPS)


def variance_shift_score(series: Sequence[float], index: int) -> float:
    """Effect size of the variance shift across the split at ``index``.

    The score is the standard-deviation analogue of
    :func:`mean_shift_score`:

        score = |std_left − std_right| / (overall_std + eps)

    Non-negative, zero when both sides share the same dispersion. Raises the
    same ``ValueError`` family as :func:`mean_shift_score`.
    """
    data = _validate_series(series)
    n = len(data)
    idx = _validate_split_index(index, n, _MIN_MIN_SIZE)
    prefix_sum, prefix_sq = _prefix_sums(data)
    _, var_left = _segment_stats(prefix_sum, prefix_sq, 0, idx)
    _, var_right = _segment_stats(prefix_sum, prefix_sq, idx, n)
    std_left = math.sqrt(var_left)
    std_right = math.sqrt(var_right)
    overall_std = _overall_std(prefix_sum, prefix_sq, 0, n)
    return abs(std_left - std_right) / (overall_std + _EPS)


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangePoint:
    """A single detected change point.

    * ``index`` — boundary position in the original series.
    * ``mean_shift_score`` / ``variance_shift_score`` — the per-channel effect
      sizes at this split (see :func:`mean_shift_score` /
      :func:`variance_shift_score`).
    * ``score`` — the combined score (Euclidean norm of the two effect sizes).
    """

    index: int
    mean_shift_score: float
    variance_shift_score: float
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mean_shift_score": self.mean_shift_score,
            "variance_shift_score": self.variance_shift_score,
            "score": self.score,
        }


@dataclass(frozen=True)
class ChangePointResult:
    """Aggregate result of :func:`detect_change_points`."""

    change_points: list[ChangePoint]
    best_index: int | None
    confidence: float
    mean_score: float
    variance_score: float
    segments: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_points": [cp.to_dict() for cp in self.change_points],
            "best_index": self.best_index,
            "confidence": self.confidence,
            "mean_score": self.mean_score,
            "variance_score": self.variance_score,
            "segments": [dict(seg) for seg in self.segments],
        }


# ---------------------------------------------------------------------------
# binary segmentation
# ---------------------------------------------------------------------------


def _combined_score(mean_score: float, variance_score: float) -> float:
    """Euclidean norm of the two effect sizes — a single combined split score."""
    return math.sqrt(mean_score * mean_score + variance_score * variance_score)


def _best_split(
    prefix_sum: list[float],
    prefix_sq: list[float],
    start: int,
    end: int,
    min_size: int,
) -> tuple[int | None, float, float, float]:
    """Find the best split in ``series[start:end]``.

    Returns ``(index, mean_score, variance_score, combined_score)`` or
    ``(None, 0.0, 0.0, 0.0)`` if no valid split exists (segment shorter than
    ``2 * min_size``).

    Only splits with a **strictly positive** combined score are considered
    valid. A flat segment (or any split whose mean- and variance-shift are
    both zero) yields ``combined == 0.0`` and is rejected — returning
    ``None`` here — so the default ``threshold=0.0`` cannot surface zero-score
    splits as spurious change points.
    """
    length = end - start
    if length < 2 * min_size:
        return None, 0.0, 0.0, 0.0
    overall_std = _overall_std(prefix_sum, prefix_sq, start, end)
    best_idx: int | None = None
    best_combined = 0.0  # reject non-positive splits (see docstring)
    best_mean = 0.0
    best_var = 0.0
    for idx in range(start + min_size, end - min_size + 1):
        mean_left, var_left = _segment_stats(prefix_sum, prefix_sq, start, idx)
        mean_right, var_right = _segment_stats(prefix_sum, prefix_sq, idx, end)
        m_score = abs(mean_left - mean_right) / (overall_std + _EPS)
        std_left = math.sqrt(var_left)
        std_right = math.sqrt(var_right)
        v_score = abs(std_left - std_right) / (overall_std + _EPS)
        combined = _combined_score(m_score, v_score)
        if combined > best_combined:
            best_combined = combined
            best_idx = idx
            best_mean = m_score
            best_var = v_score
    if best_idx is None:
        return None, 0.0, 0.0, 0.0
    return best_idx, best_mean, best_var, best_combined


def detect_change_points(
    series: Sequence[float],
    min_size: int = 5,
    max_points: int = 3,
    threshold: float = 0.0,
) -> ChangePointResult:
    """Detect change points via recursive binary segmentation.

    Parameters
    ----------
    series:
        Uniformly-sampled scalar series.
    min_size:
        Minimum number of samples required on each side of any candidate
        split (and the smallest segment the recursion will descend into).
        Must be ``>= 2``.
    max_points:
        Upper bound on the number of change points returned. Must be ``>= 1``.
    threshold:
        Minimum combined score for a split to be accepted as a change point.
        Must be ``>= 0.0``. Use ``0.0`` to keep every split the algorithm
        visits.

    Returns
    -------
    ChangePointResult
        Frozen aggregate result. ``best_index`` is the index of the strongest
        change point (or ``None`` if none was found). ``confidence`` is the
        best combined score mapped into ``[0, 1]`` via ``s / (1 + s)`` — a
        monotonically increasing normalisation that is small for weak
        changes and approaches 1 for strong ones. ``segments`` lists the
        contiguous index ranges implied by the (sorted) change points.

    Algorithm
    ----------
    Best-first binary segmentation. The segment whose best split has the
    highest combined score is examined first; when its split is accepted it
    is recorded and the best split of each of its two sub-segments becomes a
    new candidate. This continues until ``max_points`` change points are
    recorded or no candidate has a strictly positive score above
    ``threshold``. Best-first (rather than left-first DFS) ordering prevents
    weak or zero-score splits early in the series from exhausting the
    ``max_points`` budget before stronger splits later in the series are
    considered. Splits with a non-positive combined score (notably ``0.0``
    for a perfectly flat segment) are never accepted, so the default
    ``threshold=0.0`` surfaces only genuine changes.

    Raises
    ------
    ValueError
        On any invalid input (empty / non-finite / boolean series, or an
        invalid ``min_size`` / ``max_points`` / ``threshold`` parameter).
    """
    data = _validate_series(series)
    n = len(data)
    min_size_int = _require_int(min_size, "min_size")
    max_points_int = _require_int(max_points, "max_points")
    if min_size_int < _MIN_MIN_SIZE:
        raise ValueError(f"min_size must be >= {_MIN_MIN_SIZE}")
    if max_points_int < 1:
        raise ValueError("max_points must be >= 1")
    # ``threshold`` may legitimately be a float; validate it as a finite number.
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a number >= 0.0")
    threshold_float = float(threshold)
    if not math.isfinite(threshold_float) or threshold_float < 0.0:
        raise ValueError("threshold must be a number >= 0.0")
    if n < 2 * min_size_int:
        raise ValueError(f"series must contain at least {2 * min_size_int} values")

    prefix_sum, prefix_sq = _prefix_sums(data)

    # Best-first binary segmentation.
    #
    # A max-heap (emulated via negated scores) holds candidate splits ordered
    # by descending combined score. We seed it with the best split of the
    # whole series, then repeatedly pop the strongest remaining candidate:
    # accept it (if it clears ``threshold``), record it, and push the best
    # split of its left and right sub-segments back onto the heap. Looping
    # until ``max_points`` is reached or the heap is empty guarantees that
    # weak / zero-score splits early in the series cannot crowd out stronger
    # splits that appear later — the failure mode of left-first DFS.
    #
    # Heap entries: ``(-score, seq, start, end)``. The split details are
    # recomputed when a candidate interval is popped so descendants always use
    # the latest interval bounds while the heap remains compact and deterministic.
    # ``seq`` is a monotonically increasing tie-breaker so ``heapq`` never
    # falls back to comparing the (start, end) tuples — which would silently
    # reorder splits that share the same score.
    found: list[tuple[int, float, float, float]] = []
    initial = _best_split(prefix_sum, prefix_sq, 0, n, min_size_int)
    heap: list[tuple[float, int, int, int]] = []
    seq = 0
    if initial[0] is not None:
        # ``_best_split`` already rejects non-positive scores, but we keep a
        # defensive check so the heap invariant (positive scores only) holds
        # regardless of future edits to the helper.
        if initial[3] > 0.0:
            heapq.heappush(heap, (-initial[3], seq, 0, n))
            seq += 1
    while heap and len(found) < max_points_int:
        _, _, start, end = heapq.heappop(heap)
        idx, m_score, v_score, combined = _best_split(
            prefix_sum, prefix_sq, start, end, min_size_int
        )
        if idx is None or combined <= threshold_float:
            # ``combined <= 0`` is rejected implicitly because ``_best_split``
            # returns ``None`` for non-positive scores; the explicit check
            # above also covers user-supplied positive thresholds.
            continue
        found.append((idx, m_score, v_score, combined))
        # Enqueue the best split of each child segment so the next-strongest
        # candidate anywhere in the series is examined next.
        for child_start, child_end in ((start, idx), (idx, end)):
            child = _best_split(prefix_sum, prefix_sq, child_start, child_end, min_size_int)
            if child[0] is not None and child[3] > 0.0:
                heapq.heappush(heap, (-child[3], seq, child_start, child_end))
                seq += 1

    if not found:
        empty_segments = [{"start": 0, "end": n, "length": n, "mean": _segment_stats(prefix_sum, prefix_sq, 0, n)[0]}]
        return ChangePointResult(
            change_points=[],
            best_index=None,
            confidence=0.0,
            mean_score=0.0,
            variance_score=0.0,
            segments=empty_segments,
        )

    # Best-first ordering means ``found`` is already ranked by descending
    # score; ``max_points`` is enforced by the loop bound above. Re-sort by
    # index so the segments can be reconstructed contiguously.
    found.sort(key=lambda item: item[0])

    change_points = [
        ChangePoint(index=idx, mean_shift_score=m, variance_shift_score=v, score=c)
        for idx, m, v, c in found
    ]

    # Strongest change point (by combined score) drives ``best_index`` and the
    # aggregate scores.
    strongest = max(found, key=lambda item: item[3])
    best_index = strongest[0]
    best_combined = strongest[3]
    # Normalise to [0, 1] via s / (1 + s): 0 -> 0, inf -> 1.
    confidence = best_combined / (1.0 + best_combined) if best_combined > 0.0 else 0.0

    # Build contiguous segments from the sorted change-point indices.
    boundaries = [0] + [cp.index for cp in change_points] + [n]
    segments: list[dict[str, Any]] = []
    for a, b in zip(boundaries, boundaries[1:]):
        mean, _ = _segment_stats(prefix_sum, prefix_sq, a, b)
        segments.append({"start": a, "end": b, "length": b - a, "mean": mean})

    return ChangePointResult(
        change_points=change_points,
        best_index=best_index,
        confidence=confidence,
        mean_score=strongest[1],
        variance_score=strongest[2],
        segments=segments,
    )
