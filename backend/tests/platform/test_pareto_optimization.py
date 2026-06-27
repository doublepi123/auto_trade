"""Tests for P309 pareto_optimization module."""

from __future__ import annotations

import pytest

from app.platform.pareto_optimization import pareto_optimize_report


def test_pareto_frontier_excludes_dominated_configs():
    """Construct 3 configs where one is dominated; frontier should have 2."""
    configs: list[dict[str, object]] = [
        {"name": "A", "sharpe": 2.0, "max_drawdown": -0.15, "calmar": 1.5},
        {"name": "B", "sharpe": 0.5, "max_drawdown": -0.20, "calmar": 0.5},  # dominated by both A and C
        {"name": "C", "sharpe": 1.0, "max_drawdown": -0.10, "calmar": 2.5},
    ]
    result = pareto_optimize_report(configs, objectives=["sharpe", "calmar"])
    d = result.to_dict()
    names = [c["name"] for c in d["frontier"]]
    assert "A" in names
    assert "C" in names
    assert "B" not in names
    assert d["frontier_size"] == 2


def test_pareto_frontier_single_config():
    configs: list[dict[str, object]] = [
        {"name": "X", "sharpe": 1.0, "max_drawdown": -0.10},
    ]
    result = pareto_optimize_report(configs, objectives=["sharpe"])
    d = result.to_dict()
    assert d["frontier_size"] == 1
    assert d["frontier"][0]["name"] == "X"


def test_pareto_rejects_empty_configs():
    with pytest.raises(ValueError):
        pareto_optimize_report([], objectives=["sharpe"])


def test_pareto_rejects_empty_objectives():
    with pytest.raises(ValueError):
        pareto_optimize_report([{"name": "X", "sharpe": 1.0}], objectives=[])


def test_pareto_rejects_missing_objective():
    with pytest.raises(ValueError):
        pareto_optimize_report([{"name": "X", "sharpe": 1.0}], objectives=["calmar"])


def test_pareto_with_non_finite_values():
    with pytest.raises(ValueError):
        pareto_optimize_report([{"name": "X", "sharpe": float("nan")}], objectives=["sharpe"])


def test_pareto_frontier_all_non_dominated():
    configs: list[dict[str, object]] = [
        {"name": "A", "ret": 3.0, "risk": -2.0},
        {"name": "B", "ret": 1.0, "risk": -1.0},  # both are Pareto-optimal (trade-off)
        {"name": "C", "ret": 2.0, "risk": -1.5},
    ]
    result = pareto_optimize_report(configs, objectives=["ret", "risk"])
    assert result.to_dict()["frontier_size"] == 3


def test_pareto_rejects_non_dict_config():
    with pytest.raises(ValueError):
        pareto_optimize_report([[1.0, 2.0]], objectives=["ret"])
    with pytest.raises(ValueError):
        pareto_optimize_report([{"ret": 1.0}, "bad"], objectives=["ret"])  # type: ignore[list-item]


def test_pareto_rejects_too_many_configs():
    configs = [{"ret": float(i)} for i in range(51)]
    with pytest.raises(ValueError):
        pareto_optimize_report(configs, objectives=["ret"])
