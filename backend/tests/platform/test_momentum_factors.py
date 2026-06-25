"""Tests for P238 momentum / reversal factor library."""

from __future__ import annotations

import math

import pytest

from app.platform.momentum_factors import (
    MomentumFactorResult,
    carhart_momentum,
    cross_sectional_momentum,
    momentum_factor,
    reversal_factor,
    time_series_momentum,
)


def _flat_panel(n_assets: int, n_prices: int) -> dict[str, list[float]]:
    return {f"A{i}": [100.0] * n_prices for i in range(n_assets)}


def test_time_series_momentum_basic_known_answer():
    # monotonically rising prices → every window has positive past return and
    # positive forward return → TSMOM = mean of forward returns.
    # prices: 1,2,3,4,5 (lookback=1, holding=1)
    # t in [1, 3]: t=1: sign(p1/p0-1)=+1, fwd=p2/p1-1=0.5; t=2: fwd=1/3; t=3: fwd=0.25
    prices = [1.0, 2.0, 3.0, 4.0, 5.0]
    val = time_series_momentum(prices, lookback=1, holding=1)
    expected = (0.5 + (1.0 / 3.0) + 0.25) / 3.0
    assert abs(val - expected) < 1e-12


def test_time_series_momentum_sign_follows_trend():
    # falling prices → positive past return is negative, sign=-1, fwd negative
    # → product positive (trend-following profits in falling markets too).
    prices = [5.0, 4.0, 3.0, 2.0, 1.0]
    val = time_series_momentum(prices, lookback=1, holding=1)
    # each fwd = -0.25, -0.333..., -0.5; sign=-1 → product = +0.25, +0.333..., +0.5
    expected = (0.25 + (1.0 / 3.0) + 0.5) / 3.0
    assert abs(val - expected) < 1e-12
    assert val > 0


def test_time_series_momentum_insufficient_prices():
    with pytest.raises(ValueError):
        time_series_momentum([1.0, 2.0], lookback=5, holding=1)


def test_time_series_momentum_empty():
    with pytest.raises(ValueError):
        time_series_momentum([], lookback=1)


def test_time_series_momentum_invalid_lookback():
    with pytest.raises(ValueError):
        time_series_momentum([1.0, 2.0, 3.0], lookback=0, holding=1)


def test_time_series_momentum_invalid_holding():
    with pytest.raises(ValueError):
        time_series_momentum([1.0, 2.0, 3.0, 4.0], lookback=1, holding=0)


def test_time_series_momentum_holding_skips_latest():
    # With holding=2 the last 2 bars cannot be used as a formation point.
    # prices: 1..7, lookback=1, holding=2
    # valid t in [1, n-holding-1] = [1, 4]: t=1,2,3,4
    prices = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    val = time_series_momentum(prices, lookback=1, holding=2)
    # valid t in [1, n-holding-1] = [1, 4]: t=1,2,3,4; fwd = p_{t+2}/p_t - 1
    # t=1: 4/2-1=1.0, t=2: 5/3-1, t=3: 6/4-1=0.5, t=4: 7/5-1=0.4
    expected = (1.0 + (2.0 / 3.0) + 0.5 + 0.4) / 4.0
    assert abs(val - expected) < 1e-12


def test_cross_sectional_momentum_basic():
    panel = {
        "A": [10.0, 11.0, 12.0],  # +20%
        "B": [10.0, 10.0, 10.0],  # 0%
        "C": [10.0, 9.0, 8.0],    # -20%
    }
    scores = cross_sectional_momentum(panel, lookback=2)
    assert abs(scores["A"] - 0.2) < 1e-12
    assert abs(scores["B"] - 0.0) < 1e-12
    assert abs(scores["C"] + 0.2) < 1e-12


def test_cross_sectional_momentum_empty():
    with pytest.raises(ValueError):
        cross_sectional_momentum({}, lookback=1)


def test_cross_sectional_momentum_insufficient_history():
    panel = {"A": [10.0]}
    with pytest.raises(ValueError):
        cross_sectional_momentum(panel, lookback=5)


def test_momentum_factor_known_answer():
    # Three assets, lookback=2, holding=1.
    # A: 10 -> 12 (+20% formation), B: 10 -> 10 (0%), C: 10 -> 8 (-20%)
    # Forward (holding=1): use last `holding+1` prices -> p[-2], p[-1].
    panel = {
        "A": [10.0, 11.0, 12.0, 13.0],  # past-lookback(2): 12/10-1=0.2 (winner)
        "B": [10.0, 10.0, 10.0, 10.0],  # 0.0
        "C": [10.0, 9.0, 8.0, 7.0],     # -0.2 (loser)
    }
    res = momentum_factor(panel, lookback=2, holding=1, n_long=1, n_short=1)
    # long A (winner), short C (loser)
    # A fwd = 13/12 - 1; C fwd = 7/8 - 1
    a_fwd = 13.0 / 12.0 - 1.0
    c_fwd = 7.0 / 8.0 - 1.0
    assert abs(res.long_leg_return - a_fwd) < 1e-12
    assert abs(res.short_leg_return - c_fwd) < 1e-12
    assert abs(res.ls_return - (a_fwd - c_fwd)) < 1e-12
    assert res.winners == ["A"]
    assert res.losers == ["C"]


def test_momentum_factor_too_few_assets():
    panel = {"A": [10.0, 11.0, 12.0, 13.0], "B": [10.0, 10.0, 10.0, 10.0]}
    with pytest.raises(ValueError):
        momentum_factor(panel, lookback=2, holding=1, n_long=2, n_short=2)


def test_momentum_factor_insufficient_history():
    panel = {
        "A": [10.0, 11.0, 12.0],  # only 3 prices, need lookback+holding+1=4
        "B": [10.0, 10.0, 10.0],
        "C": [10.0, 9.0, 8.0],
    }
    with pytest.raises(ValueError):
        momentum_factor(panel, lookback=2, holding=1, n_long=1, n_short=1)


def test_momentum_factor_flat_panel_zero_return():
    panel = _flat_panel(4, 20)
    res = momentum_factor(panel, lookback=2, holding=1, n_long=2, n_short=2)
    assert res.long_leg_return == 0.0
    assert res.short_leg_return == 0.0
    assert res.ls_return == 0.0


def test_reversal_factor_sign_flipped_vs_momentum():
    panel = {
        "A": [10.0, 11.0, 12.0, 13.0],
        "B": [10.0, 10.0, 10.0, 10.0],
        "C": [10.0, 9.0, 8.0, 7.0],
        "D": [10.0, 9.5, 9.0, 8.5],
    }
    mom = momentum_factor(panel, lookback=2, holding=1, n_long=1, n_short=1)
    rev = reversal_factor(panel, lookback=2, holding=1, n_long=1, n_short=1)
    # reversal longs past-loser, shorts past-winner → LS = -momentum LS
    assert abs(rev.ls_return + mom.ls_return) < 1e-12
    # winners / losers labels: same assets, opposite leg assignment
    assert rev.losers == mom.losers
    assert rev.winners == mom.winners


def test_reversal_factor_default_lookback_60():
    # ensure default long horizon works without crashing on enough history
    panel = {
        "A": [100.0 * (1.0 + 0.001 * i) for i in range(70)],
        "B": [100.0 * (1.0 - 0.001 * i) for i in range(70)],
    }
    rev = reversal_factor(panel, n_long=1, n_short=1)
    d = rev.to_dict()
    assert "long_leg_return" in d and "short_leg_return" in d and "ls_return" in d
    assert "winners" in d and "losers" in d


def test_carhart_momentum_equal_halves():
    panel = {
        "A": [10.0, 11.0, 12.0, 13.0],
        "B": [10.0, 10.5, 11.0, 11.5],
        "C": [10.0, 9.5, 9.0, 8.5],
        "D": [10.0, 9.0, 8.0, 7.0],
    }
    res = carhart_momentum(panel, lookback=2, holding=1)
    # 4 assets → 2 long / 2 short
    assert len(res.winners) == 2
    assert len(res.losers) == 2
    assert isinstance(res, MomentumFactorResult)
    # winners should be the higher-past-return assets
    scores = cross_sectional_momentum(panel, lookback=2)
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    assert set(res.winners) == {a for a, _ in ranked[-2:]}
    assert set(res.losers) == {a for a, _ in ranked[:2]}


def test_carhart_momentum_too_few_assets():
    panel = {"A": [10.0, 11.0, 12.0, 13.0]}
    with pytest.raises(ValueError):
        carhart_momentum(panel, lookback=2, holding=1)


def test_momentum_factor_result_to_dict_keys():
    panel = {
        "A": [10.0, 11.0, 12.0, 13.0],
        "B": [10.0, 10.0, 10.0, 10.0],
        "C": [10.0, 9.0, 8.0, 7.0],
    }
    res = momentum_factor(panel, lookback=2, holding=1, n_long=1, n_short=1)
    d = res.to_dict()
    assert set(d.keys()) == {
        "long_leg_return",
        "short_leg_return",
        "ls_return",
        "winners",
        "losers",
    }
    assert isinstance(d["winners"], list)
    assert isinstance(d["losers"], list)


def test_cross_sectional_momentum_zero_base():
    panel = {"A": [0.0, 1.0, 2.0]}
    scores = cross_sectional_momentum(panel, lookback=2)
    # base price 0 → return defined as 0.0 to avoid division-by-zero
    assert scores["A"] == 0.0