from __future__ import annotations

import pytest

from app.platform.factor_decay import factor_decay_report


def test_factor_decay_identifies_best_short_horizon():
    report = factor_decay_report(
        [1, 2, 3, 4, 5],
        {
            "1": [0.01, 0.02, 0.03, 0.04, 0.05],
            "2": [0.05, 0.04, 0.03, 0.02, 0.01],
            "3": [0.01, 0.01, 0.02, 0.02, 0.03],
        },
    )
    body = report.to_dict()
    assert body["best_horizon"] == "1"
    assert body["decay"]["1"]["ic"] > body["decay"]["2"]["ic"]
    assert body["half_life_horizon"] in {"2", "3", None}


def test_factor_decay_rejects_length_mismatch():
    with pytest.raises(ValueError):
        factor_decay_report([1, 2, 3], {"1": [1, 2]})


def test_factor_decay_half_life_is_after_best_horizon():
    # horizon "3" has a weak IC (magnitude well below half of horizon "2"'s
    # IC of 1.0), so the half-life search correctly identifies it.
    report = factor_decay_report(
        [1, 2, 3, 4, 5],
        {
            "1": [0.01, 0.01, 0.02, 0.02, 0.03],
            "2": [0.01, 0.02, 0.03, 0.04, 0.05],
            "3": [0.03, 0.01, 0.02, 0.04, 0.015],
        },
    )
    assert report.to_dict()["best_horizon"] == "2"
    assert report.to_dict()["half_life_horizon"] == "3"


def test_factor_decay_half_life_ignores_sign_flip():
    # An IC that flips sign but retains full magnitude has NOT decayed — the
    # half-life search must use magnitude, not signed comparison.
    report = factor_decay_report(
        [1, 2, 3, 4, 5],
        {
            "1": [0.01, 0.01, 0.02, 0.02, 0.03],
            "2": [0.01, 0.02, 0.03, 0.04, 0.05],
            "3": [0.05, 0.04, 0.03, 0.02, 0.01],
        },
    )
    assert report.to_dict()["best_horizon"] == "2"
    assert report.to_dict()["half_life_horizon"] is None


def test_factor_decay_rejects_mixed_unordered_horizon_labels():
    with pytest.raises(ValueError):
        factor_decay_report([1, 2, 3], {"1": [1, 2, 3], "1d": [1, 2, 3]})
    with pytest.raises(ValueError):
        factor_decay_report([1, 2, 3], {1: [1, 2, 3]})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        factor_decay_report([1, 2, 3], {"0": [1, 2, 3]})
    with pytest.raises(ValueError):
        factor_decay_report([1, 2, 3], {"01": [1, 2, 3]})
