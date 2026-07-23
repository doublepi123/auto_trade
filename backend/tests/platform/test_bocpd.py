from __future__ import annotations

import math

import pytest

from app.platform.bocpd import bocpd


def _shifted_series() -> list[float]:
    pattern = [-0.30, 0.10, 0.25, -0.15, 0.05, -0.05]
    baseline = pattern * 10
    return baseline + [value + 5.0 for value in baseline]


def test_clear_mean_shift_produces_changepoint_near_shift():
    values = _shifted_series()

    report = bocpd(values, lam=100.0, threshold=0.009)

    assert any(55 <= index <= 75 for index in report.detected_changepoints)


def test_clear_mean_shift_resets_map_run_length_near_shift():
    values = _shifted_series()

    report = bocpd(values)

    assert any(report.map_run_lengths[index] <= 3 for index in range(55, 76))


def test_constant_series_produces_no_changepoints():
    report = bocpd([2.0] * 120)

    assert report.detected_changepoints == []


def test_report_probabilities_and_run_lengths_are_valid():
    report = bocpd(_shifted_series())

    assert len(report.changepoint_probs) == report.n_observations
    assert len(report.map_run_lengths) == report.n_observations
    assert all(0.0 <= probability <= 1.0 for probability in report.changepoint_probs)
    assert all(run_length >= 0 for run_length in report.map_run_lengths)
    assert report.changepoint_probs == pytest.approx([0.01] * report.n_observations)


def test_report_to_dict_contains_public_fields():
    report = bocpd([0.0, 0.1, -0.1])

    assert report.to_dict() == {
        "changepoint_probs": report.changepoint_probs,
        "map_run_lengths": report.map_run_lengths,
        "detected_changepoints": report.detected_changepoints,
        "n_observations": 3,
        "threshold": 0.5,
    }


def test_empty_values_raise_value_error():
    with pytest.raises(ValueError):
        bocpd([])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_raise_value_error(value: float):
    with pytest.raises(ValueError):
        bocpd([0.0, value])


def test_invalid_hyperparameters_raise_value_error():
    with pytest.raises(ValueError):
        bocpd([0.0], kappa0=0.0)
    with pytest.raises(ValueError):
        bocpd([0.0], alpha0=0.0)
    with pytest.raises(ValueError):
        bocpd([0.0], beta0=0.0)
    with pytest.raises(ValueError):
        bocpd([0.0], lam=1.0)


@pytest.mark.parametrize("threshold", [-0.01, 1.01, math.nan])
def test_invalid_threshold_raises_value_error(threshold: float):
    with pytest.raises(ValueError):
        bocpd([0.0], threshold=threshold)
