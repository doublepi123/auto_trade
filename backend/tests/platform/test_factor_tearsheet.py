from __future__ import annotations

import pytest

from app.platform.factor_tearsheet import factor_tearsheet_report


def test_factor_tearsheet_reports_ic_and_quality():
    records = [
        {"date": "d1", "symbol": "A", "factor": 1.0, "forward_return": 0.02},
        {"date": "d1", "symbol": "B", "factor": -1.0, "forward_return": -0.01},
        {"date": "d2", "symbol": "A", "factor": 0.5, "forward_return": 0.01},
        {"date": "d2", "symbol": "B", "factor": -0.5, "forward_return": -0.02},
    ]
    body = factor_tearsheet_report(records, n_quantiles=2).to_dict()
    assert body["summary"]["mean_rank_ic"] == pytest.approx(1.0)
    assert body["summary"]["quality_score"] > 0


def test_factor_tearsheet_rejects_empty_records():
    with pytest.raises(ValueError):
        factor_tearsheet_report([])
    with pytest.raises(ValueError):
        factor_tearsheet_report([{"date": "d1"}])
