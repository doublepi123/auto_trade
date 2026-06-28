"""Tests for P333 dynamic style analysis module."""

from __future__ import annotations

import pytest

from app.platform.dynamic_style import DynamicStyleResult, dynamic_style_analysis_report


def test_dynamic_style_recovers_factor_dominance():
    # returns are a linear combination of factor1 with weight 0.8 and factor2 with weight 0.2
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01] * 5  # 40 obs
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01] * 5
    returns = [0.8 * f1[i] + 0.2 * f2[i] for i in range(40)]
    factor_returns = {"F1": f1, "F2": f2}
    result = dynamic_style_analysis_report(returns, factor_returns, window=10, constraint="sum_eq_one")
    assert isinstance(result, DynamicStyleResult)
    assert len(result.per_window_weights) > 0
    # The first window's weights should approximately reflect the 0.8/0.2 split
    first = result.per_window_weights[0]
    assert abs(first["F1"] - 0.8) < 0.15
    assert abs(first["F2"] - 0.2) < 0.15


def test_dynamic_style_has_r_squared_series():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0] * 10  # 50 obs
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03] * 10
    returns = [0.5 * f1[i] + 0.5 * f2[i] for i in range(50)]
    result = dynamic_style_analysis_report(returns, {"F1": f1, "F2": f2}, window=10)
    assert len(result.r_squared_series) > 0
    assert all(0 <= r2 <= 1 for r2 in result.r_squared_series)


def test_dynamic_style_drift_score():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0] * 10
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03] * 10
    returns = [0.5 * f1[i] + 0.5 * f2[i] for i in range(50)]
    result = dynamic_style_analysis_report(returns, {"F1": f1, "F2": f2}, window=10)
    assert result.style_drift_score >= 0


def test_dynamic_style_constraint_none_uses_ols():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0] * 10
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03] * 10
    returns = [0.5 * f1[i] + 0.5 * f2[i] for i in range(50)]
    result = dynamic_style_analysis_report(returns, {"F1": f1, "F2": f2}, window=10, constraint="none")
    assert len(result.per_window_weights) > 0


def test_dynamic_style_to_dict():
    f1 = [0.01, 0.02, -0.01] * 10
    f2 = [0.0, -0.01, 0.02] * 10
    returns = [0.5 * f1[i] + 0.5 * f2[i] for i in range(30)]
    result = dynamic_style_analysis_report(returns, {"F1": f1, "F2": f2}, window=5)
    d = result.to_dict()
    assert "per_window_weights" in d
    assert "r_squared_series" in d
    assert "style_drift_score" in d
    assert "drift_detected" in d
    assert isinstance(d["per_window_weights"], list)


def test_dynamic_style_invalid_input_raises():
    try:
        dynamic_style_analysis_report([], {"F1": [0.01]}, window=10)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_dynamic_style_rejects_length_mismatch():
    from app.platform.dynamic_style import dynamic_style_analysis_report
    with pytest.raises(ValueError):
        dynamic_style_analysis_report([0.01, 0.02, 0.03], {"momentum": [0.01, 0.02]}, window=2)
