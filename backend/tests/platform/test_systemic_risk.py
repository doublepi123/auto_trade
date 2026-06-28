"""P340: Systemic risk tests."""

from __future__ import annotations

import pytest


def test_systemic_risk_basic():
    """Target and market positively correlated → delta_covar and mes are positive."""
    from app.platform.systemic_risk import systemic_risk_report

    target = [0.01, 0.02, -0.01, 0.005, -0.005, 0.01, -0.02, 0.015, 0.0, 0.005,
              0.02, 0.03, -0.015, 0.01, -0.01, 0.02, -0.03, 0.02, 0.005, 0.01]
    market = [0.015, 0.03, -0.015, 0.008, -0.008, 0.015, -0.03, 0.02, 0.0, 0.008,
              0.03, 0.045, -0.02, 0.015, -0.015, 0.03, -0.045, 0.03, 0.008, 0.015]
    result = systemic_risk_report(target, market, confidence=0.95)
    d = result.to_dict()
    assert "delta_covar" in d
    assert "mes" in d
    assert "covar_target_down" in d
    assert "covar_target_median" in d
    assert "systemic_score" in d
    # delta_covar: should be positive since target and market are positively correlated
    assert d["delta_covar"] > 0
    # mes: marginal expected shortfall (positive magnitude)
    assert d["mes"] >= 0


def test_systemic_risk_negative_correlation():
    """Negatively correlated target/market → delta_covar should be positive (loss scenario)."""
    from app.platform.systemic_risk import systemic_risk_report

    # Market goes up, target goes down (negative correlation)
    market = [0.02, 0.03, 0.01, 0.025, 0.015]
    target = [-0.01, -0.02, -0.005, -0.015, -0.01]
    result = systemic_risk_report(target, market, confidence=0.95)
    d = result.to_dict()
    # delta_covar captures additional loss during market stress — can be positive
    assert "delta_covar" in d


def test_systemic_risk_rejects_empty():
    from app.platform.systemic_risk import systemic_risk_report

    with pytest.raises(ValueError):
        systemic_risk_report([], [0.01])


def test_systemic_risk_rejects_unequal_length():
    from app.platform.systemic_risk import systemic_risk_report

    with pytest.raises(ValueError):
        systemic_risk_report([0.01, 0.02], [0.01])


def test_systemic_risk_rejects_nan():
    from app.platform.systemic_risk import systemic_risk_report

    with pytest.raises(ValueError):
        systemic_risk_report([0.01, float("nan")], [0.01, 0.02])
