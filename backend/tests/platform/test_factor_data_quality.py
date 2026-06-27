from __future__ import annotations

import pytest

from app.platform.factor_data_quality import factor_data_quality_report


def test_factor_data_quality_flags_missing_constant_outlier_and_stale():
    report = factor_data_quality_report(
        {
            "good": [1.0, 2.0, 3.0, 4.0, 5.0],
            "constant": [1.0, 1.0, 1.0, 1.0, 1.0],
            "outlier": [1.0, 1.0, 1.0, 1.0, 100.0],
            "missing": [1.0, None, 2.0, None, 3.0],
        },
        stale_window=3,
        outlier_z=1.5,
    )
    body = report.to_dict()
    assert body["feature_count"] == 4
    assert body["features"]["constant"]["is_constant"] is True
    assert body["features"]["missing"]["missing_count"] == 2
    assert body["features"]["outlier"]["outlier_count"] >= 1
    assert body["issue_count"] >= 3


def test_factor_data_quality_rejects_empty_panel():
    with pytest.raises(ValueError):
        factor_data_quality_report({})


def test_factor_data_quality_rejects_invalid_parameters_and_missing_breaks_stale():
    with pytest.raises(ValueError):
        factor_data_quality_report({"x": [1.0, 2.0]}, stale_window=2.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        factor_data_quality_report({"x": [1.0, 2.0]}, outlier_z=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        factor_data_quality_report({"x": [1.0, 2.0]}, outlier_z=float("inf"))
    body = factor_data_quality_report({"x": [1.0, None, 1.0, None, 1.0]}, stale_window=2).to_dict()
    assert body["features"]["x"]["stale_run_count"] == 0
