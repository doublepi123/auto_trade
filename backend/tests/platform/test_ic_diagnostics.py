from __future__ import annotations

import pytest

from app.platform.ic_diagnostics import ic_diagnostics_report


def test_ic_diagnostics_reports_positive_ratio_and_drawdown():
    report = ic_diagnostics_report([0.05, 0.03, -0.02, 0.04, 0.01])
    body = report.to_dict()
    assert body["mean_ic"] > 0
    assert body["positive_ratio"] == pytest.approx(0.8)
    assert body["max_cumulative_drawdown"] <= 0
    assert body["stability"] in {"weak", "moderate", "strong"}


def test_ic_diagnostics_rejects_single_value():
    with pytest.raises(ValueError):
        ic_diagnostics_report([0.1])
