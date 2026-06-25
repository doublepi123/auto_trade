"""Tests for P231 parameter importance & sensitivity."""

from __future__ import annotations

import pytest

from app.platform.sensitivity import (
    first_order_sobol,
    parameter_importance,
    total_order_sobol,
)


def test_first_order_zero_when_constant_metric():
    records = [
        {"params": {"a": 1}, "metric": 1.0},
        {"params": {"a": 2}, "metric": 1.0},
        {"params": {"a": 3}, "metric": 1.0},
    ]
    assert first_order_sobol(records, "a") == 0.0


def test_first_order_dominant_axis():
    # metric depends ONLY on 'a'
    records = []
    for a in [1, 2, 3, 4]:
        for b in [1, 2, 3]:
            records.append({"params": {"a": a, "b": b}, "metric": float(a)})
    s_a = first_order_sobol(records, "a")
    s_b = first_order_sobol(records, "b")
    assert s_a > 0.9  # 'a' explains almost all variance
    assert s_b < 0.1


def test_first_order_too_few_records():
    assert first_order_sobol([{"params": {"a": 1}, "metric": 1.0}], "a") == 0.0


def test_total_order_dominant_axis():
    records = []
    for a in [1, 2, 3, 4]:
        for b in [1, 2, 3]:
            records.append({"params": {"a": a, "b": b}, "metric": float(a)})
    s_t_a = total_order_sobol(records, "a")
    assert s_t_a > 0.9  # 'a' drives almost all → high total-order


def test_parameter_importance_ranking():
    records = []
    for a in [1, 2, 3, 4]:
        for b in [1, 2, 3]:
            records.append({"params": {"a": a, "b": b}, "metric": float(a * 10 + b)})
    rep = parameter_importance(records)
    assert "a" in rep.axes and "b" in rep.axes
    # 'a' (range 10-40) dominates 'b' (range 1-3)
    assert rep.importance_ranking[0] == "a"
    assert rep.total_order["a"] > rep.total_order["b"]
    d = rep.to_dict()
    assert "importance_ranking" in d and "interaction" in d


def test_parameter_importance_empty():
    with pytest.raises(ValueError):
        parameter_importance([])


def test_interaction_detects_cross_term():
    # metric = a * b (pure interaction, no main effect)
    records = []
    for a in [1, 2, 3, 4]:
        for b in [1, 2, 3, 4]:
            records.append({"params": {"a": a, "b": b}, "metric": float(a * b)})
    rep = parameter_importance(records)
    # total-order exceeds first-order ⇒ interaction is present for both axes
    assert rep.total_order["a"] > rep.first_order["a"]
    assert rep.total_order["b"] > rep.first_order["b"]
    assert rep.interaction["a"] > 0.0
    assert rep.interaction["b"] > 0.0