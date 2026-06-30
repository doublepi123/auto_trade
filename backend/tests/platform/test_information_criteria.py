"""Tests for P380 information criteria module."""

from __future__ import annotations

import math

import pytest

from app.platform.information_criteria import (
    InformationCriteriaResult,
    information_criteria_report,
)


def test_best_aic_is_good_model():
    """Construct 2 models: good (high LL) and bad (low LL). best_aic should be the good one."""
    models = [
        {"name": "good_model", "log_likelihood": -100.0, "n_params": 3},
        {"name": "bad_model", "log_likelihood": -500.0, "n_params": 5},
    ]
    result = information_criteria_report(models, n=200)
    assert isinstance(result, InformationCriteriaResult)
    assert result.best_aic_model == "good_model"


def test_best_bic_penalizes_complexity():
    """With many params and large n, BIC may prefer simpler model despite better LL."""
    # Complex has 1-unit better LL but 18 more params -> AIC favors it slightly
    # BIC with large n heavily penalizes extra params -> favors simple model
    models = [
        {"name": "simple", "log_likelihood": -200.0, "n_params": 2},
        {"name": "complex", "log_likelihood": -179.0, "n_params": 20},
    ]
    result = information_criteria_report(models, n=1000)
    # AIC: simple=404, complex=358+40=398 → complex wins (lower)
    assert result.best_aic_model == "complex"
    # BIC: simple=400+13.8=413.8, complex=358+138.2=496.2 → simple wins
    assert result.best_bic_model == "simple"


def test_per_model_structure():
    models = [
        {"name": "m1", "log_likelihood": -10.0, "n_params": 2},
        {"name": "m2", "log_likelihood": -12.0, "n_params": 1},
    ]
    result = information_criteria_report(models, n=100)
    for name in ("m1", "m2"):
        info = result.per_model[name]
        assert "aic" in info
        assert "bic" in info
        assert "hqic" in info
        assert math.isfinite(info["aic"])
        assert math.isfinite(info["bic"])
        assert math.isfinite(info["hqic"])


def test_to_dict_roundtrip():
    models = [
        {"name": "a", "log_likelihood": -5.0, "n_params": 2},
        {"name": "b", "log_likelihood": -8.0, "n_params": 3},
    ]
    result = information_criteria_report(models, n=50)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "per_model" in d
    assert "best_aic_model" in d
    assert "best_bic_model" in d


def test_aic_formula():
    """AIC = -2*LL + 2*k."""
    models = [{"name": "test", "log_likelihood": -10.0, "n_params": 5}]
    result = information_criteria_report(models, n=100)
    expected_aic = -2 * (-10.0) + 2 * 5  # = 20 + 10 = 30
    assert abs(result.per_model["test"]["aic"] - expected_aic) < 1e-9


def test_bic_formula():
    """BIC = -2*LL + k*ln(n)."""
    import math

    n = 200
    models = [{"name": "test", "log_likelihood": -15.0, "n_params": 3}]
    result = information_criteria_report(models, n=n)
    expected_bic = -2 * (-15.0) + 3 * math.log(n)
    assert abs(result.per_model["test"]["bic"] - expected_bic) < 1e-9


def test_hqic_formula():
    """HQIC = -2*LL + 2*k*ln(ln(n))."""
    import math

    n = 300
    models = [{"name": "test", "log_likelihood": -20.0, "n_params": 4}]
    result = information_criteria_report(models, n=n)
    expected_hqic = -2 * (-20.0) + 2 * 4 * math.log(math.log(n))
    assert abs(result.per_model["test"]["hqic"] - expected_hqic) < 1e-9


def test_validation_errors():
    """Test that invalid inputs raise ValueError."""
    import math

    with pytest.raises(ValueError):
        information_criteria_report([], n=100)
    with pytest.raises(ValueError):
        information_criteria_report([{"name": "x"}], n=100)
    with pytest.raises(ValueError):
        information_criteria_report(
            [{"name": "x", "log_likelihood": "bad", "n_params": 1}], n=100
        )
    with pytest.raises(ValueError):
        information_criteria_report(
            [{"name": "x", "log_likelihood": -1.0, "n_params": 1}], n=0
        )
