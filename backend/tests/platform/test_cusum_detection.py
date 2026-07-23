"""Tests for Page's tabular CUSUM sequential change detector."""

from __future__ import annotations

import pytest

from app.platform.cusum_detection import cusum


def _zero_mean_samples() -> list[float]:
    return [-1.0, 1.0] * 25


def test_stable_series_produces_no_signals():
    values = _zero_mean_samples() * 2

    report = cusum(values)

    assert report.signal_indices == []
    assert report.n_signals == 0


def test_upward_mean_shift_signals_shortly_after_shift():
    baseline = _zero_mean_samples()
    values = baseline + [value + 3.0 for value in baseline]

    report = cusum(values, direction="up")

    assert any(50 <= index < 75 for index in report.signal_indices)


@pytest.mark.parametrize("direction", ["down", "both"])
def test_downward_mean_shift_is_detected(direction: str):
    baseline = _zero_mean_samples()
    values = baseline + [value - 3.0 for value in baseline]

    report = cusum(values, direction=direction)

    assert any(50 <= index < 75 for index in report.signal_indices)


def test_signal_indices_stay_within_series_bounds():
    values = [0.0] * 5 + [3.0] * 5

    report = cusum(values, target=0.0, slack=0.0, threshold=1.0)

    assert report.signal_indices
    assert all(0 <= index < len(values) for index in report.signal_indices)


def test_population_defaults_and_report_serialization():
    report = cusum([-1.0, 1.0])

    assert report.target == pytest.approx(0.0)
    assert report.slack == pytest.approx(0.5)
    assert report.threshold == pytest.approx(5.0)
    assert report.to_dict() == {
        "direction": "both",
        "target": report.target,
        "slack": report.slack,
        "threshold": report.threshold,
        "cusum_pos": report.cusum_pos,
        "cusum_neg": report.cusum_neg,
        "signal_indices": report.signal_indices,
        "n_signals": report.n_signals,
    }


def test_constant_series_uses_positive_default_fallbacks():
    report = cusum([2.0] * 10)

    assert report.slack > 0.0
    assert report.threshold > 0.0
    assert report.signal_indices == []


def test_empty_values_raise_value_error():
    with pytest.raises(ValueError):
        cusum([])


def test_invalid_direction_raises_value_error():
    with pytest.raises(ValueError):
        cusum([0.0, 1.0], direction="sideways")


@pytest.mark.parametrize(
    ("slack", "threshold"),
    [(-0.1, 1.0), (0.1, -1.0)],
)
def test_negative_explicit_parameters_raise_value_error(
    slack: float,
    threshold: float,
):
    with pytest.raises(ValueError):
        cusum([0.0, 1.0], slack=slack, threshold=threshold)
