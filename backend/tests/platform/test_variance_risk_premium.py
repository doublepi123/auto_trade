from __future__ import annotations

import pytest

from app.platform.variance_risk_premium import variance_risk_premium_report


def test_variance_risk_premium_reports_positive_premium():
    body = variance_risk_premium_report([0.01, -0.02, 0.015], [0.25, 0.24, 0.26], periods_per_year=252).to_dict()
    assert body["latest"]["implied_variance"] > body["latest"]["realized_variance"]
    assert body["latest"]["vrp"] > 0
    assert body["summary"]["mean_vrp"] > 0


def test_variance_risk_premium_rejects_mismatched_inputs():
    with pytest.raises(ValueError):
        variance_risk_premium_report([0.01], [0.2, 0.3])
    with pytest.raises(ValueError):
        variance_risk_premium_report([0.01, 0.02], [0.0, 0.2])
    with pytest.raises(ValueError):
        variance_risk_premium_report([0.01, 0.02], [0.2, 0.2], periods_per_year="252")  # type: ignore[arg-type]
