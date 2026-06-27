from __future__ import annotations

import pytest

from app.platform.regime_factor_returns import regime_factor_returns_report


def test_regime_factor_returns_splits_by_regime():
    factor = {"A": 1.0, "B": -1.0, "C": 2.0, "D": -2.0}
    returns = {"A": 0.02, "B": -0.02, "C": 0.04, "D": -0.04}
    regimes = ["bull", "bear", "bull", "bear"]
    body = regime_factor_returns_report(factor, returns, regimes).to_dict()
    assert set(body["regimes"].keys()) == {"bull", "bear"}
    assert body["regimes"]["bull"]["mean_return"] > 0
    assert body["regimes"]["bear"]["mean_return"] < 0


def test_regime_factor_returns_rejects_length_mismatch():
    with pytest.raises(ValueError):
        regime_factor_returns_report({"A": 1.0}, {"A": 0.1}, ["bull", "bear"])


def test_regime_factor_returns_rejects_too_many_assets():
    factor = {f"A{i}": 1.0 for i in range(51)}
    returns = {f"A{i}": 0.1 for i in range(51)}
    regimes = ["bull"] * 51
    with pytest.raises(ValueError):
        regime_factor_returns_report(factor, returns, regimes)
