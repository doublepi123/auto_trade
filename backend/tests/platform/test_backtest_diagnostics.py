"""Tests for P267 backtest diagnostics (expectancy, payoff, streaks, bootstrap CI).

Pure unit tests — no FastAPI / app import. Covers the documented edge cases:
zero trades, no-loss infinity, streak resets on neutral (0) trades, deterministic
bootstrap CI, frozen dataclasses, and ValueError on every invalid-input path.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from app.platform.backtest_diagnostics import (
    BacktestDiagnosticsResult,
    BootstrapCI,
    backtest_diagnostics_report,
    bootstrap_expectancy_ci,
    payoff_ratio,
    profit_factor,
    streaks,
    trade_expectancy,
)


# ---------------------------------------------------------------------------
# trade_expectancy
# ---------------------------------------------------------------------------


def test_expectancy_documented_case():
    # (1 + (-0.5) + 2 + 0) / 4 = 2.5 / 4 = 0.625
    assert trade_expectancy([1.0, -0.5, 2.0, 0.0]) == pytest.approx(0.625)


def test_expectancy_neutral_counts_in_average():
    # 0 trades count toward n (and thus expectancy), but not toward win/loss.
    assert trade_expectancy([0.0, 0.0, 0.0]) == 0.0
    assert trade_expectancy([2.0, 0.0]) == pytest.approx(1.0)


def test_expectancy_single_trade():
    assert trade_expectancy([3.0]) == pytest.approx(3.0)
    assert trade_expectancy([-1.5]) == pytest.approx(-1.5)


# ---------------------------------------------------------------------------
# profit_factor
# ---------------------------------------------------------------------------


def test_profit_factor_normal_case():
    # gross_profit = 3.0, gross_loss = 1.5 -> 2.0
    pf = profit_factor([2.0, 1.0, -1.0, -0.5])
    assert pf == pytest.approx(2.0)


def test_profit_factor_infinite_when_no_losses_but_gains():
    # No losses, has profit -> math.inf
    assert profit_factor([1.0, 2.0, 0.5]) == math.inf


def test_profit_factor_zero_when_no_gains_and_no_losses():
    # All zero -> 0.0 (documented: no profit AND no loss returns 0)
    assert profit_factor([0.0, 0.0, 0.0]) == 0.0


def test_profit_factor_zero_when_only_losses():
    # gross_profit = 0 -> ratio is 0 by the "no profit" rule
    assert profit_factor([-1.0, -2.0]) == 0.0


# ---------------------------------------------------------------------------
# payoff_ratio
# ---------------------------------------------------------------------------


def test_payoff_ratio_normal_case():
    # avg_win = (2 + 1)/2 = 1.5 ; avg_loss = (1 + 0.5)/2 = 0.75 -> 2.0
    pr = payoff_ratio([2.0, 1.0, -1.0, -0.5])
    assert pr == pytest.approx(2.0)


def test_payoff_ratio_infinite_when_no_losses_but_gains():
    assert payoff_ratio([1.0, 2.0, 0.5]) == math.inf


def test_payoff_ratio_zero_when_no_gains():
    # No wins -> avg_win = 0 -> ratio = 0 (documented)
    assert payoff_ratio([-1.0, -2.0]) == 0.0


def test_payoff_ratio_zero_when_all_zero():
    # No wins and no losses -> documented: returns 0.0
    assert payoff_ratio([0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# streaks
# ---------------------------------------------------------------------------


def test_streaks_basic_win_loss():
    # W W L W L L -> max win streak 2, max loss streak 2
    max_win, max_loss = streaks([1.0, 2.0, -1.0, 0.5, -2.0, -0.5])
    assert max_win == 2
    assert max_loss == 2


def test_streaks_neutral_resets_and_does_not_count():
    # W W 0 W -> neutral breaks the streak but is not itself a win/loss.
    # After the 0, a fresh single-win streak starts.
    # Max win streak here = 2 (from the first two wins).
    max_win, max_loss = streaks([1.0, 1.0, 0.0, 1.0])
    assert max_win == 2
    assert max_loss == 0


def test_streaks_all_neutral():
    # No wins, no losses -> both streaks 0
    assert streaks([0.0, 0.0, 0.0]) == (0, 0)


def test_streaks_leading_neutrals():
    # 0 0 W W -> max win streak 2
    assert streaks([0.0, 0.0, 1.0, 1.0]) == (2, 0)


def test_streaks_single_win():
    assert streaks([1.0]) == (1, 0)
    assert streaks([-1.0]) == (0, 1)


# ---------------------------------------------------------------------------
# bootstrap_expectancy_ci
# ---------------------------------------------------------------------------


def test_bootstrap_ci_deterministic_same_seed():
    trades = [1.0, -0.5, 2.0, -1.5, 0.5, 0.8, -0.3, 1.2, -0.7, 0.4]
    ci_a = bootstrap_expectancy_ci(trades, n_bootstrap=500, seed=42)
    ci_b = bootstrap_expectancy_ci(trades, n_bootstrap=500, seed=42)
    assert ci_a.low == ci_b.low
    assert ci_a.high == ci_b.high
    assert ci_a.seed == 42
    assert ci_a.n_bootstrap == 500


def test_bootstrap_ci_low_le_high():
    trades = [1.0, -0.5, 2.0, -1.5, 0.5, 0.8, -0.3, 1.2, -0.7, 0.4]
    ci = bootstrap_expectancy_ci(trades, n_bootstrap=300, seed=7)
    assert ci.low <= ci.high


def test_bootstrap_ci_default_seed_is_none_still_valid():
    trades = [1.0, -0.5, 2.0, -1.5, 0.5]
    ci = bootstrap_expectancy_ci(trades, n_bootstrap=100)
    assert ci.low <= ci.high
    assert ci.seed is None
    assert ci.n_bootstrap == 100


def test_bootstrap_ci_to_dict():
    ci = BootstrapCI(low=-0.1, high=0.2, seed=123, n_bootstrap=1000)
    d = ci.to_dict()
    assert d == {"low": -0.1, "high": 0.2, "seed": 123, "n_bootstrap": 1000}


def test_bootstrap_ci_different_seeds_usually_differ():
    trades = [1.0, -0.5, 2.0, -1.5, 0.5, 0.8, -0.3, 1.2, -0.7, 0.4] * 5
    ci_a = bootstrap_expectancy_ci(trades, n_bootstrap=500, seed=1)
    ci_b = bootstrap_expectancy_ci(trades, n_bootstrap=500, seed=2)
    # Not a strict guarantee in pathological cases, but with 50 trades and
    # different seeds the bounds should differ in practice.
    assert (ci_a.low, ci_a.high) != (ci_b.low, ci_b.high)


# ---------------------------------------------------------------------------
# invalid-input handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        [],                  # empty
        [1.0, float("nan")],  # non-finite
        [1.0, float("inf")],  # non-finite
        [1, 2],              # ints are fine actually -> use a truly bad one below
    ],
)
def test_invalid_trades_raise(bad):
    # NOTE: ints [1,2] are accepted (they coerce to float). The parametrize
    # filters that below; this test only asserts the genuinely-invalid cases.
    if bad == [1, 2]:
        pytest.skip("ints are valid numeric inputs")
    with pytest.raises(ValueError):
        trade_expectancy(bad)


def test_empty_trades_raise_value_error():
    with pytest.raises(ValueError):
        trade_expectancy([])


def test_bool_in_trades_rejected():
    with pytest.raises(ValueError):
        trade_expectancy([True, False])


def test_string_in_trades_rejected():
    with pytest.raises(ValueError):
        trade_expectancy(["a", "b"])


def test_dict_in_trades_rejected():
    with pytest.raises(ValueError):
        trade_expectancy([{"x": 1.0}])


def test_non_sequence_trades_rejected():
    with pytest.raises((TypeError, ValueError)):
        trade_expectancy(42)  # type: ignore[arg-type]


def test_nan_in_trades_rejected():
    with pytest.raises(ValueError):
        trade_expectancy([1.0, float("nan")])


def test_profit_factor_invalid_raises():
    with pytest.raises(ValueError):
        profit_factor([])
    with pytest.raises(ValueError):
        profit_factor([1.0, "x"])  # type: ignore[list-item]


def test_payoff_ratio_invalid_raises():
    with pytest.raises(ValueError):
        payoff_ratio([])
    with pytest.raises(ValueError):
        payoff_ratio([True])


def test_streaks_invalid_raises():
    with pytest.raises(ValueError):
        streaks([])
    with pytest.raises(ValueError):
        streaks([1.0, None])  # type: ignore[list-item]


def test_bootstrap_invalid_n_bootstrap_bool_rejected():
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([1.0, -0.5], n_bootstrap=True)  # type: ignore[arg-type]


def test_bootstrap_invalid_n_bootstrap_zero_rejected():
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([1.0, -0.5], n_bootstrap=0)


def test_bootstrap_invalid_n_bootstrap_negative_rejected():
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([1.0, -0.5], n_bootstrap=-5)


def test_bootstrap_seed_bool_rejected():
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([1.0, -0.5], n_bootstrap=10, seed=True)  # type: ignore[arg-type]


def test_bootstrap_invalid_trades_raises():
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([], n_bootstrap=10)
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([1.0, "x"], n_bootstrap=10)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# report + dataclass behaviour
# ---------------------------------------------------------------------------


def test_report_to_dict_contains_all_fields():
    trades = [1.0, -0.5, 2.0, 0.0, -1.0, 0.5]
    rep = backtest_diagnostics_report(trades, n_bootstrap=200, seed=99)
    d = rep.to_dict()
    for key in (
        "expectancy",
        "profit_factor",
        "payoff_ratio",
        "win_rate",
        "loss_rate",
        "max_win_streak",
        "max_loss_streak",
        "bootstrap_expectancy_ci",
        "n_trades",
    ):
        assert key in d
    assert d["n_trades"] == len(trades)
    # bootstrap_expectancy_ci is itself a dict with low/high/seed/n_bootstrap
    bsci = d["bootstrap_expectancy_ci"]
    assert set(bsci.keys()) == {"low", "high", "seed", "n_bootstrap"}
    assert bsci["seed"] == 99
    assert bsci["n_bootstrap"] == 200


def test_report_win_rate_loss_rate():
    # 3 wins, 2 losses out of 5 decisive trades (1 neutral excluded from rates)
    trades = [1.0, 2.0, -0.5, 0.0, -1.5, 0.5]
    rep = backtest_diagnostics_report(trades, n_bootstrap=100, seed=1)
    # win_rate = wins / (wins + losses) = 3/5 = 0.6 ; loss_rate = 2/5 = 0.4
    assert rep.win_rate == pytest.approx(0.6)
    assert rep.loss_rate == pytest.approx(0.4)
    assert rep.win_rate + rep.loss_rate == pytest.approx(1.0)


def test_report_expectancy_matches_helper():
    trades = [1.0, -0.5, 2.0, 0.0]
    rep = backtest_diagnostics_report(trades, n_bootstrap=50, seed=3)
    assert rep.expectancy == pytest.approx(trade_expectancy(trades))
    assert rep.profit_factor == pytest.approx(profit_factor(trades))
    assert rep.payoff_ratio == pytest.approx(payoff_ratio(trades))


def test_report_all_zero_trades_edge():
    trades = [0.0, 0.0, 0.0]
    rep = backtest_diagnostics_report(trades, n_bootstrap=50, seed=3)
    # expectancy 0, profit_factor 0 (no gains & no losses), payoff_ratio 0,
    # win/loss rates both 0 (no decisive trades), streaks 0, n_trades 3.
    assert rep.expectancy == 0.0
    assert rep.profit_factor == 0.0
    assert rep.payoff_ratio == 0.0
    assert rep.win_rate == 0.0
    assert rep.loss_rate == 0.0
    assert rep.max_win_streak == 0
    assert rep.max_loss_streak == 0
    assert rep.n_trades == 3


def test_report_invalid_trades_raise():
    with pytest.raises(ValueError):
        backtest_diagnostics_report([], n_bootstrap=10)
    with pytest.raises(ValueError):
        backtest_diagnostics_report([1.0, "x"], n_bootstrap=10)  # type: ignore[list-item]


def test_report_invalid_n_bootstrap_raises():
    with pytest.raises(ValueError):
        backtest_diagnostics_report([1.0, -0.5], n_bootstrap=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# dataclass frozen / to_dict
# ---------------------------------------------------------------------------


def test_bootstrap_ci_is_frozen():
    ci = BootstrapCI(low=0.0, high=1.0, seed=None, n_bootstrap=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ci.low = 5.0  # type: ignore[misc]


def test_result_is_frozen():
    rep = backtest_diagnostics_report([1.0, -0.5], n_bootstrap=10, seed=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.expectancy = 99.0  # type: ignore[misc]


def test_bootstrap_ci_is_dataclass_with_expected_fields():
    fields = {f.name for f in dataclasses.fields(BootstrapCI)}
    assert fields == {"low", "high", "seed", "n_bootstrap"}


def test_result_is_dataclass_with_expected_fields():
    fields = {f.name for f in dataclasses.fields(BacktestDiagnosticsResult)}
    assert fields == {
        "expectancy",
        "profit_factor",
        "payoff_ratio",
        "win_rate",
        "loss_rate",
        "max_win_streak",
        "max_loss_streak",
        "bootstrap_expectancy_ci",
        "n_trades",
    }


# ---------------------------------------------------------------------------
# to_dict JSON-safe serialization (P267 follow-up)
# ---------------------------------------------------------------------------
#
# FastAPI's default JSON encoder rejects non-finite floats (math.inf / NaN),
# which would 500 the /backtest-diagnostics endpoint for a legitimate
# "no-loss" trade series. The pure functions keep returning math.inf (their
# mathematical behaviour is covered above and must not change); the to_dict
# layer is responsible for emitting a JSON-safe representation instead.


def test_to_dict_serializes_no_loss_profit_factor_and_payoff_as_infinity_string():
    # trades [1.0, 2.0] -> no losses, has profit -> math.inf on both ratios.
    rep = backtest_diagnostics_report([1.0, 2.0], n_bootstrap=10, seed=0)
    d = rep.to_dict()
    # Pure dataclass still carries the mathematical inf for downstream callers.
    assert rep.profit_factor == math.inf
    assert rep.payoff_ratio == math.inf
    # to_dict emits the JSON-safe string convention.
    assert d["profit_factor"] == "Infinity"
    assert d["payoff_ratio"] == "Infinity"
    # The JSON-safe dict must round-trip through stdlib json without error.
    import json

    json.dumps(d)


def test_to_dict_keeps_finite_values_as_plain_floats():
    # A mixed win/loss series produces finite ratios — those must stay as
    # plain floats in to_dict (no spurious stringification).
    rep = backtest_diagnostics_report([2.0, 1.0, -1.0, -0.5], n_bootstrap=10, seed=1)
    d = rep.to_dict()
    assert d["profit_factor"] == pytest.approx(2.0)
    assert d["payoff_ratio"] == pytest.approx(2.0)
    assert isinstance(d["profit_factor"], float)
    assert isinstance(d["payoff_ratio"], float)


def test_to_dict_serializes_zero_ratios_unchanged():
    # Degenerate inputs that yield 0.0 (not inf) must stay 0.0, not a string.
    rep = backtest_diagnostics_report([0.0, 0.0, 0.0], n_bootstrap=10, seed=2)
    d = rep.to_dict()
    assert d["profit_factor"] == 0.0
    assert d["payoff_ratio"] == 0.0


def test_bootstrap_ci_to_dict_is_json_safe():
    # Bootstrap bounds are finite means, so no inf is expected in practice,
    # but the contract is that the dict is always JSON-serializable. Build a
    # BootstrapCI directly and assert json.dumps succeeds.
    import json

    ci = BootstrapCI(low=-0.1, high=0.2, seed=123, n_bootstrap=1000)
    json.dumps(ci.to_dict())
