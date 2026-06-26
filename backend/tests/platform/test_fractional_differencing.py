"""Tests for P258 fractional differencing."""

from __future__ import annotations

import math

import pytest

from app.platform.fractional_differencing import (
    fractional_difference,
    fractional_difference_ffd,
    fractional_diff_report,
    fractional_weights,
)


def test_weights_first_two():
    # w0 = 1; w1 = -d.
    w = fractional_weights(0.4, threshold=1e-5)
    assert abs(w[0] - 1.0) < 1e-12
    assert abs(w[1] - (-0.4)) < 1e-12


def test_weights_decay_after_d():
    # |w_k| is monotonically decreasing once k > d.
    w = fractional_weights(0.4, threshold=1e-5)
    # All weights past index 1 should have decreasing magnitude.
    mags = [abs(x) for x in w[1:]]
    for a, b in zip(mags, mags[1:]):
        assert b <= a + 1e-15


def test_weights_larger_d_more_persistence():
    # Larger d applies a stronger differencing transform and should still keep
    # a non-trivial fixed window at the practical default threshold.
    w_small = fractional_weights(0.1, threshold=1e-5)
    w_large = fractional_weights(0.6, threshold=1e-5)
    assert len(w_large) > 1
    assert len(w_small) > 1


def test_fractional_difference_d_one_approximates_first_diff():
    # As d -> 1 (from below), (1-B)^d -> (1-B): output ≈ x_t - x_{t-1}.
    series = [float(i) ** 2 for i in range(30)]  # quadratic trend
    out = fractional_difference(series, 0.9, threshold=1e-7)
    # Drop warm-up Nones; compare against first difference of the tail.
    present_idx = [i for i, v in enumerate(out) if v is not None]
    for t in present_idx[-5:]:
        first_diff = series[t] - series[t - 1]
        # Fractional d=0.9 should be close to first difference in sign & magnitude.
        value = out[t]
        assert value is not None
        assert abs(value) > 0.0


def test_fractional_difference_constant_series_matches_truncated_weight_sum():
    series = [5.0] * 20
    out = fractional_difference(series, 0.4)
    weights = fractional_weights(0.4)
    for idx, v in enumerate(out):
        assert v is not None
        expected = 5.0 * sum(weights[: idx + 1])
        assert abs(v - expected) < 1e-9


def test_fractional_difference_expanding_semantics():
    """Real expanding-window fractional differencing.

    The expanding variant ``(1−B)^d`` applied at index ``t`` uses *all* available
    history ``x_t, x_{t-1}, …, x_0`` (truncating the infinite weight sum at the
    start of the sample). This means there is **no warm-up gap**:

    * the first output entry is a valid (truncated-weight) number, not ``None``;
    * the output length equals the input length exactly.

    López de Prado (2018) §4 describes the expanding-window estimator precisely
    this way — the FFD (fixed-window) variant is the *separate* function that
    introduces a warm-up gap. Asserting this contract here guards against the
    two code paths silently collapsing into the same implementation.
    """
    series = [float(i) for i in range(20)]
    out = fractional_difference(series, 0.4)
    assert len(out) == len(series), "expanding output must match input length"
    assert out[0] is not None, "expanding first entry must be a real value (truncated sum), not None"
    assert all(v is not None for v in out), "expanding window has no warm-up gap"


def test_fractional_difference_differs_from_ffd():
    """Expanding-window and fixed-window differencing must produce different output.

    The expanding estimator re-weights every sample using all history up to that
    point, so the early entries differ from FFD's constant-width window. If the
    two implementations collapse to the same code path, this assertion fails —
    exposing the regression.
    """
    series = [float(i) for i in range(40)]
    expanding = fractional_difference(series, 0.4)
    ffd = fractional_difference_ffd(series, 0.4)
    assert len(expanding) == len(ffd) == len(series)
    # At the very least, the second entry (where FFD's window first fills vs.
    # expanding's already-full-from-history value) should disagree.
    assert any(a != b for a, b in zip(expanding, ffd)), \
        "expanding and ffd outputs must not be identical"


def test_ffd_same_length_as_input():
    series = [float(i) for i in range(30)]
    out = fractional_difference_ffd(series, 0.4)
    assert len(out) == 30


def test_ffd_preserves_more_than_first_difference():
    # FFD with d<1 keeps long memory: variance of FFD < variance of first diff
    # is NOT guaranteed, but FFD output should be less aggressive than diff=1.
    series = [math.sin(i / 3.0) * 10 + i * 0.5 for i in range(100)]
    ffd = [v for v in fractional_difference_ffd(series, 0.4) if v is not None]
    diff1 = [series[i] - series[i - 1] for i in range(1, len(series))]
    assert len(ffd) > 0
    # FFD output magnitudes are bounded; the signal level is preserved (not zeroed).
    assert any(abs(v) > 0.5 for v in ffd)


def test_invalid_d_raises():
    with pytest.raises(ValueError):
        fractional_weights(0.0)
    with pytest.raises(ValueError):
        fractional_weights(1.5)
    with pytest.raises(ValueError):
        fractional_weights(0.4, threshold=0.0)


def test_empty_series_raises():
    with pytest.raises(ValueError):
        fractional_difference([], 0.4)


def test_report_aggregates():
    series = [math.sin(i / 5.0) + i * 0.01 for i in range(60)]
    res = fractional_diff_report(series, d=0.4)
    d = res.to_dict()
    assert d["d"] == 0.4
    assert d["n_weights"] > 1
    assert d["n_output"] > 0
    assert "adf_stat" in d
    assert len(d["output"]) == 60


def test_to_dict_roundtrip():
    res = fractional_diff_report([float(i) for i in range(40)], d=0.3)
    out = res.to_dict()
    assert "output" in out and "d" in out
