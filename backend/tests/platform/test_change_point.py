"""Tests for P261 change-point detection (binary segmentation).

Pure-Python change-point detector: mean-shift and variance-shift split scores,
combined via recursive binary segmentation into a ranked list of change points.
"""

from __future__ import annotations

import math

import pytest

from app.platform.change_point import (
    ChangePoint,
    ChangePointResult,
    detect_change_points,
    mean_shift_score,
    variance_shift_score,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _step_series() -> list[float]:
    """A clean two-regime mean-shift: 20 ones followed by 20 fives."""
    return [1.0] * 20 + [5.0] * 20


def _variance_shift_series() -> list[float]:
    """Low-variance segment followed by a high-variance segment (same mean).

    First 30 samples are ~0; last 30 samples swing +/-5 around 0. Means match
    so mean_shift_score is near-zero while variance_shift_score is large.
    """
    low = [0.0] * 30
    high = []
    for i in range(30):
        high.append(5.0 if i % 2 == 0 else -5.0)
    return low + high


def _flat_series(n: int = 40) -> list[float]:
    """A perfectly flat series — no change anywhere."""
    return [3.0] * n


# ---------------------------------------------------------------------------
# mean_shift_score
# ---------------------------------------------------------------------------


class TestMeanShiftScore:
    def test_step_series_peaks_at_true_split(self):
        series = _step_series()
        score_at_20 = mean_shift_score(series, 20)
        score_off = mean_shift_score(series, 10)
        assert score_at_20 > score_off
        assert score_at_20 > 0.0

    def test_score_non_negative(self):
        series = _step_series()
        for idx in range(2, len(series) - 2):
            assert mean_shift_score(series, idx) >= 0.0

    def test_invalid_index_below_min_size_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            mean_shift_score(series, 1)  # left side too small for min_size=5

    def test_invalid_index_above_len_minus_min_size_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            mean_shift_score(series, len(series) - 1)

    def test_non_int_index_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            mean_shift_score(series, 2.5)  # type: ignore[arg-type]

    def test_bool_index_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            mean_shift_score(series, True)  # type: ignore[arg-type]

    def test_short_series_raises(self):
        with pytest.raises(ValueError):
            mean_shift_score([1.0, 2.0], 1)

    def test_bool_entry_raises(self):
        with pytest.raises(ValueError):
            mean_shift_score([True, False, 1.0, 2.0, 1.0, 2.0], 3)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# variance_shift_score
# ---------------------------------------------------------------------------


class TestVarianceShiftScore:
    def test_variance_shift_peaks_at_true_split(self):
        series = _variance_shift_series()
        score_at_30 = variance_shift_score(series, 30)
        score_off = variance_shift_score(series, 15)
        assert score_at_30 > score_off
        assert score_at_30 > 0.0

    def test_score_non_negative(self):
        series = _variance_shift_series()
        for idx in range(5, len(series) - 5):
            assert variance_shift_score(series, idx) >= 0.0

    def test_flat_segment_variance_score_is_zero(self):
        # Both sides flat ⇒ variances 0 ⇒ variance shift score 0.
        series = _flat_series()
        assert variance_shift_score(series, 20) == pytest.approx(0.0, abs=1e-12)

    def test_invalid_index_raises(self):
        series = _variance_shift_series()
        n = len(series)
        # default min_size is 2 ⇒ index must satisfy 2 <= index <= n-2.
        with pytest.raises(ValueError):
            variance_shift_score(series, 1)
        with pytest.raises(ValueError):
            variance_shift_score(series, n - 1)

    def test_bool_index_raises(self):
        series = _variance_shift_series()
        with pytest.raises(ValueError):
            variance_shift_score(series, True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# detect_change_points
# ---------------------------------------------------------------------------


class TestDetectChangePoints:
    def test_step_series_detects_best_index_near_20(self):
        series = _step_series()
        result = detect_change_points(series)
        assert isinstance(result, ChangePointResult)
        assert result.best_index is not None
        assert abs(result.best_index - 20) <= 2
        assert result.change_points  # at least one
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence > 0.5  # strong change

    def test_change_points_sorted_by_index(self):
        # Two steps: 1→5 at 20, 5→9 at 40.
        series = [1.0] * 20 + [5.0] * 20 + [9.0] * 20
        result = detect_change_points(series, max_points=3)
        indices = [cp.index for cp in result.change_points]
        assert indices == sorted(indices)
        # Should detect both steps (allow some tolerance).
        assert len(result.change_points) >= 1

    def test_no_change_series_low_confidence(self):
        series = _flat_series()
        result = detect_change_points(series)
        # Flat series ⇒ no real change ⇒ either no change points or very low
        # best score / confidence.
        if result.change_points:
            assert result.confidence < 0.2
            assert all(cp.score <= 0.1 for cp in result.change_points)
        else:
            assert result.best_index is None or result.confidence < 0.2

    # ------------------------------------------------------------------
    # P261 audit: flat-series false positives and best-first ranking
    # ------------------------------------------------------------------

    def test_flat_series_yields_no_change_points_default_threshold(self):
        # P261 bug #1: a perfectly flat series has combined==0 everywhere;
        # the default ``threshold=0.0`` used to keep those zero-score splits
        # and report spurious change points. The fix requires a strictly
        # positive score, so a flat series must produce no change points.
        series = _flat_series()
        result = detect_change_points(series)
        assert result.change_points == []
        assert result.best_index is None
        # confidence must be exactly 0 (no positive score survived).
        assert result.confidence == 0.0
        # a single full-range segment is still returned.
        assert len(result.segments) == 1
        assert result.segments[0]["start"] == 0
        assert result.segments[0]["end"] == len(series)

    def test_default_threshold_rejects_zero_score_change_points(self):
        # P261 bug #1 (regression guard): default threshold must not accept a
        # split whose combined score is exactly 0. A flat series is the
        # canonical case; here we additionally assert the per-CP score
        # invariant directly via the API surface.
        series = _flat_series()
        result = detect_change_points(series)
        for cp in result.change_points:
            assert cp.score > 0.0

    def test_two_step_series_detects_both_real_change_points(self):
        # P261 bug #2: left-first DFS could exhaust ``max_points`` on weak /
        # zero splits before reaching the second real change point. With a
        # best-first recursion the two strong splits (at 20 and 40) must
        # both be reported for ``[1]*20 + [5]*20 + [9]*20``.
        series = [1.0] * 20 + [5.0] * 20 + [9.0] * 20
        result = detect_change_points(series, max_points=3)
        indices = [cp.index for cp in result.change_points]
        # Both real splits survive.
        assert len(result.change_points) == 2
        assert any(abs(idx - 20) <= 2 for idx in indices)
        assert any(abs(idx - 40) <= 2 for idx in indices)
        # Result stays sorted by index and every change point has a real score.
        assert indices == sorted(indices)
        for cp in result.change_points:
            assert cp.score > 0.0

    def test_best_first_does_not_lose_real_change_point_behind_weak_split(self):
        # P261 bug #2 (targeted guard): a strong change point later in the
        # series must not be crowded out by an earlier weaker split. Build a
        # 3-segment series where the FIRST split chosen by global best is the
        # strong one, then ensure both segments still surface.
        # Mild change at 15 (1 -> 1.3) then a big change at 30 (1.3 -> 9).
        series = [1.0] * 15 + [1.3] * 15 + [9.0] * 15
        result = detect_change_points(series, max_points=3)
        indices = [cp.index for cp in result.change_points]
        assert any(abs(idx - 15) <= 2 for idx in indices)
        assert any(abs(idx - 30) <= 2 for idx in indices)

    def test_variance_shift_detected(self):
        series = _variance_shift_series()
        result = detect_change_points(series)
        assert result.variance_score > 0.0
        assert result.change_points

    def test_segments_cover_full_range(self):
        series = _step_series()
        result = detect_change_points(series)
        assert result.segments  # non-empty
        first = result.segments[0]
        last = result.segments[-1]
        assert first["start"] == 0
        assert last["end"] == len(series)
        # Segments must be contiguous (no gaps, no overlaps).
        for a, b in zip(result.segments, result.segments[1:]):
            assert a["end"] == b["start"]

    def test_to_dict_keys(self):
        series = _step_series()
        body = detect_change_points(series).to_dict()
        assert set(body.keys()) >= {
            "change_points",
            "best_index",
            "confidence",
            "mean_score",
            "variance_score",
            "segments",
        }
        assert isinstance(body["change_points"], list)
        for cp in body["change_points"]:
            assert set(cp.keys()) == {
                "index",
                "mean_shift_score",
                "variance_shift_score",
                "score",
            }

    def test_max_points_caps_change_point_count(self):
        # Many steps; ensure we never exceed max_points.
        series = []
        for level in range(8):
            series.extend([float(level)] * 15)
        result = detect_change_points(series, max_points=3)
        assert len(result.change_points) <= 3

    def test_threshold_filters_weak_points(self):
        series = _flat_series()
        # With a high threshold nothing should survive.
        result = detect_change_points(series, threshold=1e9)
        assert result.change_points == []

    def test_short_series_raises(self):
        with pytest.raises(ValueError):
            detect_change_points([1.0, 2.0])

    def test_invalid_min_size_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, min_size=1)
        with pytest.raises(ValueError):
            detect_change_points(series, min_size=0)

    def test_min_size_exceeds_half_length_raises(self):
        # Length 40, min_size=25 ⇒ 2*min_size=50 > 40 ⇒ invalid.
        with pytest.raises(ValueError):
            detect_change_points(_step_series(), min_size=25)

    def test_invalid_max_points_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, max_points=0)

    def test_invalid_threshold_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, threshold=-0.1)

    def test_bool_min_size_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, min_size=True)  # type: ignore[arg-type]

    def test_bool_max_points_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, max_points=True)  # type: ignore[arg-type]

    def test_bool_threshold_raises(self):
        series = _step_series()
        with pytest.raises(ValueError):
            detect_change_points(series, threshold=True)  # type: ignore[arg-type]

    def test_bool_series_entry_raises(self):
        with pytest.raises(ValueError):
            detect_change_points([True, False, True, False, True, False, True, False, True, False])  # type: ignore[list-item]

    def test_min_size_enforced_on_splits(self):
        # min_size=10 ⇒ every detected change point must keep >=10 on each side.
        series = _step_series()  # length 40
        result = detect_change_points(series, min_size=10)
        for cp in result.change_points:
            assert cp.index >= 10
            assert cp.index <= len(series) - 10


# ---------------------------------------------------------------------------
# dataclass behaviour
# ---------------------------------------------------------------------------


class TestDataclassBehaviour:
    def test_change_point_is_frozen(self):
        cp = ChangePoint(index=10, mean_shift_score=0.5, variance_shift_score=0.1, score=0.6)
        with pytest.raises(Exception):
            cp.index = 11  # type: ignore[misc]
        with pytest.raises(Exception):
            cp.score = 0.9  # type: ignore[misc]

    def test_change_point_to_dict(self):
        cp = ChangePoint(index=10, mean_shift_score=0.5, variance_shift_score=0.1, score=0.6)
        body = cp.to_dict()
        assert body == {
            "index": 10,
            "mean_shift_score": 0.5,
            "variance_shift_score": 0.1,
            "score": 0.6,
        }

    def test_change_point_result_is_frozen(self):
        result = detect_change_points(_step_series())
        with pytest.raises(Exception):
            result.confidence = 0.99  # type: ignore[misc]

    def test_change_point_result_to_dict_reuses_change_point_to_dict(self):
        result = detect_change_points(_step_series())
        assert result.change_points
        body = result.to_dict()
        first = body["change_points"][0]
        assert first == result.change_points[0].to_dict()
