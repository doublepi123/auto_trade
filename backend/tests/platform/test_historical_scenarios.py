"""Tests for P229 historical scenario generator."""

from __future__ import annotations

import pytest

from app.platform.historical_scenarios import (
    HistoricalEpisode,
    HistoricalScenarioLibrary,
    apply_scenario,
    default_episodes,
    historical_stress_report,
)


def test_default_episodes_nonempty():
    eps = default_episodes()
    assert len(eps) >= 3
    assert all(isinstance(e, HistoricalEpisode) for e in eps)


def test_apply_scenario_basic():
    positions = {"A.US": (100, 100.0), "B.US": (200, 50.0)}
    ep = HistoricalEpisode("test", {"A.US": -0.10, "B.US": 0.05})
    pnl = apply_scenario(positions, ep)
    assert abs(pnl["A.US"] - (-1000.0)) < 1e-9  # 100*100*-0.10
    assert abs(pnl["B.US"] - 500.0) < 1e-9  # 200*50*0.05


def test_apply_scenario_missing_symbol_zero():
    positions = {"X.US": (100, 100.0)}
    ep = HistoricalEpisode("test", {"A.US": -0.10})
    pnl = apply_scenario(positions, ep)
    assert pnl["X.US"] == 0.0


def test_historical_stress_report_worst():
    positions = {"A.US": (100, 100.0)}
    rep = historical_stress_report(positions)
    assert rep.worst_pnl < 0
    assert rep.best_pnl > rep.worst_pnl
    assert rep.worst_episode in [e.name for e in default_episodes()]


def test_historical_stress_report_to_dict_keys():
    positions = {"A.US": (100, 100.0)}
    d = historical_stress_report(positions).to_dict()
    assert "per_episode" in d and "worst_episode" in d
    assert "capital_adequate" in d and "percentile_95_loss" in d


def test_historical_stress_report_empty_positions():
    with pytest.raises(ValueError):
        historical_stress_report({})


def test_library_add_and_names():
    lib = HistoricalScenarioLibrary()
    assert lib.names() == []
    lib.add_episode(HistoricalEpisode("custom", {"A.US": -0.2}))
    assert lib.names() == ["custom"]


def test_historical_stress_report_with_custom_library():
    lib = HistoricalScenarioLibrary()
    lib.add_episode(HistoricalEpisode("mild", {"A.US": -0.01}))
    lib.add_episode(HistoricalEpisode("severe", {"A.US": -0.50}))
    positions = {"A.US": (100, 100.0)}
    rep = historical_stress_report(positions, library=lib)
    assert rep.worst_episode == "severe"
    assert abs(rep.worst_pnl - (-5000.0)) < 1e-9
    assert rep.best_episode == "mild"


def test_historical_stress_report_capital_adequate():
    lib = HistoricalScenarioLibrary()
    lib.add_episode(HistoricalEpisode("crash", {"A.US": -0.50}))
    positions = {"A.US": (100, 100.0)}  # notional 10000, loss -5000
    # with a 6000 buffer, worst + buffer = 1000 > 0 → adequate
    rep = historical_stress_report(positions, library=lib, capital_buffer=6000.0)
    assert rep.capital_adequate is True
    rep2 = historical_stress_report(positions, library=lib, capital_buffer=1000.0)
    assert rep2.capital_adequate is False  # -5000+1000 < 0


def test_empty_library_raises():
    lib = HistoricalScenarioLibrary()
    with pytest.raises(ValueError):
        historical_stress_report({"A.US": (100, 100.0)}, library=lib)


def test_episode_to_dict():
    ep = HistoricalEpisode("name", {"A.US": -0.1}, "desc")
    d = ep.to_dict()
    assert d["name"] == "name" and d["description"] == "desc" and d["returns"] == {"A.US": -0.1}