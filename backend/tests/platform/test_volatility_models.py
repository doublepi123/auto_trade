"""Tests for P225 volatility forecasting models."""

from __future__ import annotations

import math

import pytest

from app.platform.volatility_models import (
    ewma_volatility,
    garch11_volatility,
    parkinson_volatility,
    volatility_report,
)


def test_ewma_length_and_seed():
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    v = ewma_volatility(rs, lam=0.94)
    assert len(v) == 5
    assert v[0] == 0.01 ** 2  # seeded with first squared return


def test_ewma_decay_blends():
    rs = [0.0, 0.10, 0.0, 0.0]
    v = ewma_volatility(rs, lam=0.5)
    # v[1] = 0.5*0 + 0.5*0 = 0 ; v[2] = 0.5*0 + 0.5*0.10^2 = 0.005
    assert abs(v[2] - 0.005) < 1e-9


def test_ewma_invalid_lambda():
    with pytest.raises(ValueError):
        ewma_volatility([0.01], lam=1.5)


def test_garch_stationarity_constraint():
    with pytest.raises(ValueError):
        garch11_volatility([0.01, -0.01], alpha=0.6, beta=0.5)


def test_garch_invalid_alpha():
    with pytest.raises(ValueError):
        garch11_volatility([0.01, -0.01], alpha=-0.1, beta=0.5)


def test_garch_too_short():
    with pytest.raises(ValueError):
        garch11_volatility([0.01])


def test_garch_mean_reverts_to_long_run():
    # constant-zero returns → variance decays toward long-run (sample) variance ~0
    rs = [0.0] * 50
    v = garch11_volatility(rs, alpha=0.1, beta=0.85)
    assert abs(v[-1]) < 1e-6


def test_garch_reacts_to_shock():
    rs = [0.0] * 20 + [0.20] + [0.0] * 20
    v = garch11_volatility(rs, alpha=0.1, beta=0.85)
    # variance spikes right after the shock (index 21)
    assert v[21] > v[20]


def test_parkinson_basic():
    # H=110, L=100 → ln(1.1)≈0.0953, var = (1/(4ln2))*0.0953^2
    h = [110.0]
    l = [100.0]
    v = parkinson_volatility(h, l)
    expected = (1.0 / (4.0 * math.log(2.0))) * (math.log(1.1) ** 2)
    assert abs(v[0] - expected) < 1e-9


def test_parkinson_invalid_inputs():
    with pytest.raises(ValueError):
        parkinson_volatility([110.0], [100.0, 90.0])
    with pytest.raises(ValueError):
        parkinson_volatility([], [])


def test_parkinson_window_smoothing():
    h = [110.0, 105.0, 120.0]
    l = [100.0, 95.0, 110.0]
    v = parkinson_volatility(h, l, window=2)
    assert len(v) == 3


def test_volatility_report_with_highs_lows():
    rs = [0.01, -0.02, 0.03, -0.01, 0.02, 0.0, -0.01, 0.015]
    h = [101.0, 99.0, 103.0, 100.0, 102.0, 101.0, 100.5, 101.5]
    l = [99.0, 97.0, 100.0, 98.0, 100.0, 99.5, 99.0, 100.5]
    rep = volatility_report(rs, highs=h, lows=l)
    d = rep.to_dict()
    assert "ewma" in d and "garch" in d and "parkinson" in d
    assert rep.latest_parkinson is not None
    assert rep.long_run_variance >= 0


def test_volatility_report_no_highs_lows():
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    rep = volatility_report(rs)
    assert rep.parkinson is None
    assert rep.latest_parkinson is None