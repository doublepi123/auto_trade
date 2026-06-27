from __future__ import annotations

import pytest

from app.platform.factor_turnover import factor_turnover_report


def test_factor_turnover_reports_bucket_retention_and_autocorrelation():
    report = factor_turnover_report(
        [
            {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0},
            {"A": 4.2, "B": 2.8, "C": 2.1, "D": 1.2},
            {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0},
        ],
        bucket_fraction=0.5,
    )

    body = report.to_dict()
    assert body["n_snapshots"] == 3
    assert body["bucket_size"] == 2
    assert body["average_top_turnover"] == pytest.approx(0.5)
    assert body["average_bottom_turnover"] == pytest.approx(0.5)
    assert body["average_rank_autocorrelation"] < 0.5


def test_factor_turnover_rejects_missing_or_constant_snapshots():
    with pytest.raises(ValueError):
        factor_turnover_report([{"A": 1.0, "B": 2.0}, {"A": 1.0}])
    with pytest.raises(ValueError):
        factor_turnover_report([{"A": 1.0, "B": 1.0}, {"A": 1.0, "B": 1.0}])
    with pytest.raises(ValueError):
        factor_turnover_report([{"A": 1.0, "B": 2.0}, {"A": 2.0, "B": 1.0}], bucket_fraction=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        factor_turnover_report([{"A": 1.0, "B": 2.0}, {"A": 2.0, "B": 1.0}], bucket_fraction="0.5")  # type: ignore[arg-type]
