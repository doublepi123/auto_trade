"""P326: regime attribution tests."""

from __future__ import annotations

import pytest

from app.platform.regime_attribution import (
    RegimeAttributionResult,
    regime_attribution_report,
)


def test_regime_attribution_basic():
    """Bull and bear regimes produce alpha with expected sign."""
    returns = [0.02, 0.03, 0.01, -0.02, -0.03, -0.01, 0.02, -0.02]
    regimes = ["bull", "bull", "bull", "bear", "bear", "bear", "bull", "bear"]
    result = regime_attribution_report(returns, regimes)
    d = result.to_dict()
    assert "regimes" in d
    regimes_out = d["regimes"]
    assert len(regimes_out) == 2
    bull = next(r for r in regimes_out if r["regime"] == "bull")
    bear = next(r for r in regimes_out if r["regime"] == "bear")
    assert bull["alpha"] > bear["alpha"]


def test_regime_attribution_with_benchmark():
    """With benchmark, alpha = mean_return - benchmark_mean."""
    returns = [0.02, 0.03, 0.01, -0.02, -0.03, -0.01]
    regimes = ["bull", "bull", "bull", "bear", "bear", "bear"]
    benchmark = [0.01, 0.01, 0.01, -0.01, -0.01, -0.01]
    result = regime_attribution_report(returns, regimes, benchmark=benchmark)
    d = result.to_dict()
    bull = next(r for r in d["regimes"] if r["regime"] == "bull")
    bear = next(r for r in d["regimes"] if r["regime"] == "bear")
    # bull mean = 0.02, benchmark bull mean = 0.01 → alpha ≈ 0.01
    assert bull["alpha"] == pytest.approx(0.01, abs=1e-10)
    # bear mean = -0.02, benchmark bear mean = -0.01 → alpha ≈ -0.01
    assert bear["alpha"] == pytest.approx(-0.01, abs=1e-10)


def test_regime_attribution_contribution():
    """Contribution = regime_mean × regime_proportion."""
    returns = [0.02, 0.03, 0.01, -0.02, -0.03, -0.01, 0.02, -0.02]
    regimes = ["bull", "bull", "bull", "bear", "bear", "bear", "bull", "bear"]
    result = regime_attribution_report(returns, regimes)
    d = result.to_dict()
    bull = next(r for r in d["regimes"] if r["regime"] == "bull")
    # bull: 4/8 proportion, mean = (0.02+0.03+0.01+0.02)/4 = 0.02
    assert bull["contribution"] == pytest.approx(0.02 * 0.5, abs=1e-10)


def test_regime_attribution_beta():
    """Beta = regime returns vs benchmark returns (cov/var)."""
    returns = [0.02, 0.03, 0.01, -0.02, -0.03, -0.01]
    regimes = ["bull", "bull", "bull", "bear", "bear", "bear"]
    benchmark = [0.01, 0.015, 0.005, -0.01, -0.015, -0.005]
    result = regime_attribution_report(returns, regimes, benchmark=benchmark)
    d = result.to_dict()
    for r in d["regimes"]:
        assert "beta" in r
        assert isinstance(r["beta"], float)


def test_regime_attribution_single_regime():
    """Single regime → contribution = mean."""
    returns = [0.01, 0.02, 0.01]
    regimes = ["flat", "flat", "flat"]
    result = regime_attribution_report(returns, regimes)
    d = result.to_dict()
    assert len(d["regimes"]) == 1
    assert d["regimes"][0]["contribution"] == pytest.approx(
        sum(returns) / len(returns), abs=1e-10
    )


def test_regime_attribution_rejects_mismatched_lengths():
    """Length mismatch raises ValueError."""
    with pytest.raises(ValueError):
        regime_attribution_report([0.01, 0.02], ["bull"])
    with pytest.raises(ValueError):
        regime_attribution_report(
            [0.01, 0.02], ["bull", "bear"], benchmark=[0.01]
        )


def test_regime_attribution_to_dict_keys():
    """to_dict contains expected top-level keys."""
    result = regime_attribution_report(
        [0.01, 0.02], ["bull", "bull"]
    )
    d = result.to_dict()
    assert "regimes" in d
    assert "n_observations" in d
    assert "total_return" in d
