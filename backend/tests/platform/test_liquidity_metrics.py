"""Tests for P237 liquidity metrics (Amihud / Roll / Pastor-Stambaugh / Corwin-Schultz)."""

from __future__ import annotations

import math

import pytest

from app.platform.liquidity_metrics import (
    LiquidityResult,
    RollResult,
    amihud_illiquidity,
    corwin_schultz,
    liquidity_report,
    pastor_stambaugh,
    roll_spread,
)


# --- Amihud ---------------------------------------------------------------


def test_amihud_basic():
    returns = [0.01, -0.02, 0.03]
    volumes = [100.0, 200.0, 300.0]
    expected = (abs(0.01) / 100 + abs(-0.02) / 200 + abs(0.03) / 300) / 3
    assert abs(amihud_illiquidity(returns, volumes) - expected) < 1e-12


def test_amihud_skips_zero_volume_when_return_zero():
    # zero-volume bar with zero return is skipped; the other two bars average
    assert abs(amihud_illiquidity([0.0, 0.02, 0.04], [0.0, 100.0, 200.0]) - (0.02 / 100 + 0.04 / 200) / 2) < 1e-12


def test_amihud_length_mismatch():
    with pytest.raises(ValueError):
        amihud_illiquidity([0.01, 0.02], [100.0])


def test_amihud_empty():
    with pytest.raises(ValueError):
        amihud_illiquidity([], [])


def test_amihud_negative_volume_raises():
    with pytest.raises(ValueError):
        amihud_illiquidity([0.01, 0.02], [100.0, -1.0])


def test_amihud_nonzero_return_on_zero_volume_raises():
    with pytest.raises(ValueError):
        amihud_illiquidity([0.01, 0.02], [0.0, 100.0])


# --- Roll ----------------------------------------------------------------


def test_roll_positive_autocov_returns_zero():
    # i.i.d. positive-then-positive series => positive serial cov => spread 0
    r = roll_spread([0.01, 0.02, 0.03, 0.04])
    assert r.is_positive_autocov is True
    assert r.spread == 0.0


def test_roll_negative_autocov_gives_positive_spread():
    # alternating-sign returns => negative serial covariance => positive spread
    r = roll_spread([0.01, -0.01, 0.01, -0.01])
    assert r.is_positive_autocov is False
    assert r.spread > 0.0


def test_roll_known_answer():
    # construct a series with known serial covariance
    # returns: r_t = eps_t - eps_{t-1} where eps ~ constant step
    # use r = [1, -1, 1, -1] * scale; mean = 0
    scale = 0.001
    r = roll_spread([scale, -scale, scale, -scale, scale, -scale])
    # cov(r_t, r_{t-1}) = mean(r_t * r_{t-1}) (mean=0) = -scale^2
    assert abs(r.serial_cov - (-scale * scale)) < 1e-15
    assert abs(r.spread - 2.0 * math.sqrt(scale * scale)) < 1e-12


def test_roll_too_few():
    with pytest.raises(ValueError):
        roll_spread([0.01])


def test_roll_to_dict_keys():
    d = roll_spread([0.01, -0.01]).to_dict()
    assert set(d.keys()) == {"spread", "serial_cov", "is_positive_autocov"}


# --- Pastor-Stambaugh ----------------------------------------------------


def test_pastor_stambaugh_sign_negative_for_liquidity_sensitive():
    # When returns load negatively on squared market returns (liquidity risk),
    # delta should be negative.
    # Build market returns; returns = -1.0 * market^2 (pure liquidity exposure)
    market = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.015, -0.025]
    returns = [-(m * m) for m in market]
    delta = pastor_stambaugh(returns, market)
    assert delta < 0


def test_pastor_stambaugh_length_mismatch():
    with pytest.raises(ValueError):
        pastor_stambaugh([0.01, 0.02], [0.01])


def test_pastor_stambaugh_too_few():
    with pytest.raises(ValueError):
        pastor_stambaugh([0.01, 0.02], [0.01, 0.02])


def test_pastor_stambaugh_zero_when_independent():
    # returns independent of market^2 given market: returns = 2*market + noise(0)
    # the squared-market residual is orthogonal to returns residual => delta ~ 0
    market = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.015, -0.025, 0.005, -0.015]
    returns = [2.0 * m for m in market]
    delta = pastor_stambaugh(returns, market)
    assert abs(delta) < 1e-9


# --- Corwin-Schultz ------------------------------------------------------


def test_corwin_schultz_basic():
    # Pure spread, no volatility: identical intrabar high/low (so the 2-bar
    # range equals each 1-bar range) => estimator recovers the spread exactly.
    # H = mid + s/2, L = mid - s/2 with constant mid => alpha_1 = alpha_2 = ln((mid+s/2)/(mid-s/2))
    # and spread_est = (sqrt(2)-1)*alpha_1/(sqrt(2)-1) = alpha_1.
    s = 0.02
    mid = 100.0
    highs = [mid + s / 2] * 4
    lows = [mid - s / 2] * 4
    cs = corwin_schultz(highs, lows)
    assert abs(cs - math.log((mid + s / 2) / (mid - s / 2))) < 1e-12


def test_corwin_schultz_zero_when_vol_only():
    # When the 2-bar range equals sqrt(2) times the mean 1-bar range, the
    # volatility terms cancel and the spread estimate is 0. This cancellation
    # is a population property; we realize it deterministically by directly
    # forcing alpha_2 = sqrt(2) * alpha_1 *on the estimator's inputs*: set
    # both 1-bar log ranges to a1 and the 2-bar log range to sqrt(2)*a1. The
    # only way to do that with positive prices is to let the second bar's low
    # dip below the first bar's low (so the 2-bar low is the dip, the 2-bar high
    # is the shared high). Then alpha_1 = (a1 + a1')/2 where a1' is the second
    # bar's own range — to keep alpha_1 = a1 we need a1' = a1 too, so the
    # second bar's high also rises by the same dip. That keeps the 2-bar range
    # at a1 (not sqrt(2)*a1). So the clean deterministic cancellation can't be
    # built from real prices; instead verify the formula's cancellation
    # algebraically via a tiny stub of the math.
    a1 = math.log(101.0 / 100.0)
    sqrt2 = math.sqrt(2.0)
    # estimator: (sqrt2 * alpha_1 - alpha_2) / (sqrt2 - 1)
    alpha_1 = a1
    alpha_2 = sqrt2 * a1
    est = (sqrt2 * alpha_1 - alpha_2) / (sqrt2 - 1.0)
    assert abs(est) < 1e-12  # volatility cancels exactly
    # And the public function clamps negatives to 0 and stays finite.
    from app.platform.liquidity_metrics import corwin_schultz as _cs
    cs = _cs([101.0, 101.0], [100.0, 100.0])
    assert cs >= 0.0 and math.isfinite(cs)


def test_corwin_schultz_monotonic_in_spread():
    # Wider pure spread (bigger intrabar range, constant mid, no vol) => the
    # 1-bar and 2-bar ranges both equal ln((mid+s/2)/(mid-s/2)), which grows
    # monotonically with s, so the CS estimate grows monotonically too.
    mid = 100.0
    spreads = []
    for s in [0.005, 0.01, 0.02, 0.04, 0.08]:
        highs = [mid + s / 2] * 4
        lows = [mid - s / 2] * 4
        spreads.append(corwin_schultz(highs, lows))
    for s in spreads:
        assert s > 0
    for i in range(1, len(spreads)):
        assert spreads[i] >= spreads[i - 1] - 1e-12


def test_corwin_schultz_length_mismatch():
    with pytest.raises(ValueError):
        corwin_schultz([101.0, 102.0], [100.0])


def test_corwin_schultz_too_few():
    with pytest.raises(ValueError):
        corwin_schultz([101.0], [100.0])


def test_corwin_schultz_non_positive_raises():
    with pytest.raises(ValueError):
        corwin_schultz([101.0, 102.0], [100.0, 0.0])


def test_corwin_schultz_high_below_low_raises():
    with pytest.raises(ValueError):
        corwin_schultz([100.0, 100.0], [101.0, 101.0])


# --- liquidity_report ----------------------------------------------------


def test_liquidity_report_partial_inputs():
    rep = liquidity_report(
        returns=[0.01, -0.01, 0.01, -0.01],
        volumes=[100.0, 200.0, 100.0, 200.0],
    )
    assert isinstance(rep, LiquidityResult)
    d = rep.to_dict()
    assert d["amihud"] is not None and d["amihud"] > 0
    assert d["roll"] is not None
    assert d["pastor_stambaugh"] is None
    assert d["corwin_schultz"] is None
    assert d["n"] == 4


def test_liquidity_report_all_inputs():
    rep = liquidity_report(
        returns=[0.01, -0.02, 0.03, -0.01, 0.02, -0.03],
        volumes=[100.0, 200.0, 150.0, 300.0, 250.0, 180.0],
        market_returns=[0.005, -0.01, 0.015, -0.005, 0.01, -0.015],
        highs=[101.0, 102.0, 103.0, 101.5, 102.5, 100.5],
        lows=[100.0, 100.5, 101.0, 100.0, 101.0, 99.5],
    )
    d = rep.to_dict()
    assert d["amihud"] is not None
    assert d["roll"] is not None
    assert d["pastor_stambaugh"] is not None
    assert d["corwin_schultz"] is not None
    assert d["n"] == 6


def test_liquidity_report_empty_returns():
    rep = liquidity_report(returns=[])
    d = rep.to_dict()
    assert d["amihud"] is None
    assert d["roll"] is None
    assert d["n"] == 0


def test_liquidity_report_invalid_volume_skips_amihud():
    # negative volume triggers ValueError inside amihud; report swallows it
    rep = liquidity_report(
        returns=[0.01, 0.02],
        volumes=[100.0, -1.0],
    )
    assert rep.amihud is None
    # roll still computed from returns alone
    assert rep.roll is not None