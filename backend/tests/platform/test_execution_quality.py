"""Tests for P241 execution-quality scorecard (fill stats / reversion / grade)."""

from __future__ import annotations

import math

import pytest

from app.platform.execution_quality import (
    ExecutionScorecard,
    FillStats,
    ReversionResult,
    execution_scorecard,
    fill_stats,
    price_reversion,
)


# --------------------------------------------------------------------------- #
# fill_stats
# --------------------------------------------------------------------------- #
def test_fill_stats_basic_known_answer():
    # Two BUY fills, benchmark 100.00. Prices 100.00 and 100.02.
    # slippage: 0 bps and 2 bps.
    fills = [
        {"qty": 100, "price": 100.00, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
        {"qty": 100, "price": 100.02, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
    ]
    fs = fill_stats(fills)
    assert fs.n_fills == 2
    assert fs.total_qty == 200
    assert fs.total_order_qty == 200
    assert abs(fs.fill_ratio - 1.0) < 1e-12
    assert abs(fs.participation_rate - 1.0) < 1e-12
    # vwap = (100*100 + 100*100.02)/200 = 100.01
    assert abs(fs.vwap_fill_price - 100.01) < 1e-9
    assert abs(fs.avg_fill_price - 100.01) < 1e-9
    assert abs(fs.slippage_bps[0]) < 1e-9
    assert abs(fs.slippage_bps[1] - 2.0) < 1e-9
    assert abs(fs.mean_slippage_bps - 1.0) < 1e-9
    assert abs(fs.median_slippage_bps - 1.0) < 1e-9
    assert abs(fs.mean_abs_slippage_bps - 1.0) < 1e-9


def test_fill_stats_sell_slippage_sign():
    # SELL: benchmark 50.00, fill 49.90 → received less → +slippage (unfavorable)
    fills = [
        {"qty": 200, "price": 49.90, "side": "SELL", "order_qty": 250, "benchmark_price": 50.00},
    ]
    fs = fill_stats(fills)
    # slippage = (50 - 49.90)/50 * 1e4 = 20 bps
    assert abs(fs.slippage_bps[0] - 20.0) < 1e-9
    assert abs(fs.fill_ratio - 0.8) < 1e-12
    # participation_rate capped at 1.0, but fill_ratio=0.8 < 1 so equals 0.8
    assert abs(fs.participation_rate - 0.8) < 1e-12


def test_fill_stats_partial_fill_ratio():
    fills = [
        {"qty": 60, "price": 10.0, "side": "BUY", "order_qty": 100, "benchmark_price": 10.0},
        {"qty": 30, "price": 10.0, "side": "BUY", "order_qty": 100, "benchmark_price": 10.0},
    ]
    fs = fill_stats(fills)
    # total_qty=90, total_order_qty=200 → 0.45
    assert abs(fs.fill_ratio - 0.45) < 1e-12
    assert abs(fs.participation_rate - 0.45) < 1e-12


def test_fill_stats_vwap_weighted():
    # Unequal qty → vwap is qty-weighted, not simple avg.
    fills = [
        {"qty": 300, "price": 10.0, "side": "BUY", "order_qty": 300, "benchmark_price": 10.0},
        {"qty": 100, "price": 12.0, "side": "BUY", "order_qty": 100, "benchmark_price": 10.0},
    ]
    fs = fill_stats(fills)
    # vwap = (300*10 + 100*12)/400 = 10.5
    assert abs(fs.vwap_fill_price - 10.5) < 1e-9
    # avg = (10+12)/2 = 11
    assert abs(fs.avg_fill_price - 11.0) < 1e-9


def test_fill_stats_p95_single_fill():
    # With one fill p95 == that fill's slippage.
    fills = [
        {"qty": 10, "price": 100.10, "side": "BUY", "order_qty": 10, "benchmark_price": 100.00},
    ]
    fs = fill_stats(fills)
    # slippage 10 bps
    assert abs(fs.p95_slippage_bps - 10.0) < 1e-9
    assert abs(fs.mean_slippage_bps - 10.0) < 1e-9


def test_fill_stats_empty_raises():
    with pytest.raises(ValueError):
        fill_stats([])


def test_fill_stats_missing_key_raises():
    bad = [{"qty": 10, "price": 100.0, "side": "BUY"}]  # no order_qty, no benchmark_price
    with pytest.raises(ValueError):
        fill_stats(bad)


def test_fill_stats_invalid_side_raises():
    bad = [
        {"qty": 10, "price": 100.0, "side": "BUYX", "order_qty": 10, "benchmark_price": 100.0},
    ]
    with pytest.raises(ValueError):
        fill_stats(bad)


def test_fill_stats_zero_benchmark_raises():
    bad = [
        {"qty": 10, "price": 100.0, "side": "BUY", "order_qty": 10, "benchmark_price": 0.0},
    ]
    with pytest.raises(ValueError):
        fill_stats(bad)


def test_fill_stats_to_dict_keys():
    fills = [
        {"qty": 10, "price": 100.0, "side": "BUY", "order_qty": 10, "benchmark_price": 100.0},
    ]
    d = fill_stats(fills).to_dict()
    for k in (
        "n_fills", "total_qty", "total_order_qty", "fill_ratio", "participation_rate",
        "avg_fill_price", "vwap_fill_price", "slippage_bps", "mean_slippage_bps",
        "median_slippage_bps", "p95_slippage_bps", "mean_abs_slippage_bps",
    ):
        assert k in d


# --------------------------------------------------------------------------- #
# price_reversion
# --------------------------------------------------------------------------- #
def test_price_reversion_buy_adverse():
    # Bought at 100, post prices drift down to 99.5 → +reversion (adverse for BUY)
    post = [99.8, 99.7, 99.6, 99.5, 99.5]
    rv = price_reversion(benchmark_price=100.0, fill_price=100.0, post_fill_prices=post)
    mean_post = sum(post) / len(post)  # 99.7
    assert rv.side == "BUY"
    assert abs(rv.post_fill_mean - mean_post) < 1e-9
    # reversion = 99.7 - 100 = -0.3 → NEGATIVE, not adverse (market went up after buy is favorable? no)
    # Actually market went DOWN after BUY → we bought high → adverse. But our sign convention:
    # reversion = post_mean - fill = 99.7 - 100 = -0.3 → negative sign means... unfavorable?
    # Per spec: positive => adverse. Here reversion is negative → NOT adverse under this convention.
    # Wait: BUY adverse means market reverted DOWN → post < fill → reversion negative.
    # The spec says "reversion = mean(post) - fill for BUY (positive => adverse)".
    # That means market goes UP after buy (post > fill) is adverse? No — buying high then market
    # going up is GOOD. Re-reading: "did price revert after the fill (adverse selection)".
    # For BUY: if post_mean > fill, market went up → we bought LOW, good (reversion positive
    # per spec convention = adverse??). The spec explicitly says positive => adverse for BUY.
    # So positive reversion (post > fill) = market rose after we bought = we LEFT money on
    # table = adverse selection (we could have waited). That's the adverse-selection reading.
    assert rv.reversion < 0  # post < fill → not flagged adverse under spec sign convention
    assert rv.is_adverse is False


def test_price_reversion_buy_adverse_when_market_rises():
    # Bought at 100, market rose to 101 → reversion positive → adverse (left money on table).
    post = [100.5, 100.6, 100.7, 100.8, 100.9]
    rv = price_reversion(benchmark_price=100.0, fill_price=100.0, post_fill_prices=post)
    mean_post = sum(post) / len(post)  # 100.7
    assert rv.reversion > 0  # 100.7 - 100 = 0.7
    # reversion_bps = 0.7 / 100 * 1e4 = 70 bps > 5 bps threshold
    assert rv.reversion_bps > 5.0
    assert rv.is_adverse is True
    assert rv.reversion_sign == 1


def test_price_reversion_sell_adverse():
    # Sold at 100, market rose to 101 → for SELL, reversion = fill - post = 100 - 101 = -1 →
    # negative → not adverse (we sold low and market fell = favorable? no).
    # Per spec: SELL positive => we sold low and market bounced => adverse.
    # Market rose after sell → we sold low → adverse → reversion should be positive.
    # reversion = fill - post_mean = 100 - 101 = -1 → NEGATIVE per our formula. That's wrong sign.
    # Re-reading spec: "(fill_price - mean(post)) for SELL. Return reversion_bps... positive => adverse"
    # For SELL adverse = we sold low and market bounced UP. post > fill → fill - post < 0 → negative.
    # But spec says positive => adverse for SELL. So spec convention: SELL adverse means market
    # went DOWN after we sold (we sold high, good)? That contradicts "sold low and market bounced".
    # The spec text is contradictory; we implement: SELL reversion = fill - post_mean, and
    # is_adverse = (reversion > 0) i.e. post < fill i.e. market fell after we sold = we sold high
    # = GOOD, not adverse. Hmm. Let me just verify our implementation matches the formula exactly:
    # SELL: reversion = fill - post_mean. If post < fill (market fell) → reversion > 0 → is_adverse True.
    # That means: sold at 100, market fell to 99 → we sold HIGH → favorable, but flagged adverse.
    # That seems backwards but it's what the formula in spec says. Let's test the formula only.
    post = [99.5, 99.4, 99.3, 99.2, 99.1]
    rv = price_reversion(benchmark_price=100.0, fill_price=100.0, post_fill_prices=post, side="SELL")
    mean_post = sum(post) / len(post)  # 99.3
    # reversion = fill - post = 100 - 99.3 = 0.7 > 0
    assert rv.reversion > 0
    assert rv.reversion_bps > 5.0
    assert rv.is_adverse is True


def test_price_reversion_window_truncation():
    # window larger than available → uses all.
    post = [100.0, 100.0, 100.0]
    rv = price_reversion(100.0, 100.0, post, window=10)
    assert rv.window == 3
    assert rv.reversion == 0.0
    assert rv.is_adverse is False


def test_price_reversion_empty_post_raises():
    with pytest.raises(ValueError):
        price_reversion(100.0, 100.0, [])


def test_price_reversion_bad_window_raises():
    with pytest.raises(ValueError):
        price_reversion(100.0, 100.0, [100.0], window=0)


def test_price_reversion_bad_side_raises():
    with pytest.raises(ValueError):
        price_reversion(100.0, 100.0, [100.0], side="HOLD")


def test_price_reversion_nonpositive_fill_raises():
    with pytest.raises(ValueError):
        price_reversion(100.0, 0.0, [100.0])


def test_price_reversion_nonpositive_post_raises():
    with pytest.raises(ValueError):
        price_reversion(100.0, 100.0, [100.0, -1.0])


def test_price_reversion_threshold_boundary():
    # reversion_bps exactly at threshold (5.0) → NOT adverse (strict >).
    # fill=100, post_mean=100.05 → reversion=0.05 → bps=5.0 → not > 5 → not adverse.
    post = [100.05, 100.05, 100.05, 100.05, 100.05]
    rv = price_reversion(100.0, 100.0, post, adverse_threshold_bps=5.0)
    assert abs(rv.reversion_bps - 5.0) < 1e-9
    assert rv.is_adverse is False


def test_price_reversion_to_dict_keys():
    rv = price_reversion(100.0, 100.0, [101.0, 101.0], side="BUY")
    d = rv.to_dict()
    for k in (
        "side", "fill_price", "benchmark_price", "window", "post_fill_mean",
        "reversion", "reversion_bps", "reversion_sign", "is_adverse",
        "adverse_threshold_bps",
    ):
        assert k in d


# --------------------------------------------------------------------------- #
# execution_scorecard
# --------------------------------------------------------------------------- #
def _perfect_fills(n: int = 5) -> list[dict]:
    return [
        {"qty": 100, "price": 100.00, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00}
        for _ in range(n)
    ]


def test_scorecard_grade_a():
    # All fills at benchmark, full fill ratio → mean_abs_slippage=0 < 2, fill_ratio=1.0 ≥ 0.99 → A.
    sc = execution_scorecard(_perfect_fills(5))
    assert sc.grade == "A"
    assert sc.n_reversion_checked == 0
    assert sc.adverse_selection_rate == 0.0
    assert abs(sc.mean_abs_slippage_bps) < 1e-9
    assert isinstance(sc, ExecutionScorecard)


def test_scorecard_grade_b():
    # mean_abs_slippage ~3 bps, fill_ratio=1.0 → B (3 < 5, fill_ratio ≥ 0.95).
    fills = [
        {"qty": 100, "price": 100.03, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
        {"qty": 100, "price": 100.03, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
    ]
    sc = execution_scorecard(fills)
    assert sc.grade == "B"
    assert abs(sc.mean_abs_slippage_bps - 3.0) < 1e-9


def test_scorecard_grade_c():
    # mean_abs_slippage ~7 bps, fill_ratio=1.0 → C (5 ≤ 7 < 10).
    fills = [
        {"qty": 100, "price": 100.07, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
        {"qty": 100, "price": 100.07, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
    ]
    sc = execution_scorecard(fills)
    assert sc.grade == "C"


def test_scorecard_grade_d():
    # mean_abs_slippage 20 bps → D.
    fills = [
        {"qty": 100, "price": 100.20, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
        {"qty": 100, "price": 100.20, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
    ]
    sc = execution_scorecard(fills)
    assert sc.grade == "D"


def test_scorecard_grade_set_membership():
    # Grade is always one of A/B/C/D across a range.
    for slip_bps in (0.0, 1.0, 2.0, 4.0, 6.0, 9.0, 15.0, 50.0):
        fills = [
            {"qty": 100, "price": 100.0 * (1 + slip_bps / 1e4),
             "side": "BUY", "order_qty": 100, "benchmark_price": 100.0}
            for _ in range(2)
        ]
        sc = execution_scorecard(fills)
        assert sc.grade in {"A", "B", "C", "D"}


def test_scorecard_adverse_caps_at_c():
    # Perfect fills (would be A) but >50% adverse selection → cap at C.
    fills = _perfect_fills(4)
    # All 4 fills adverse (market rose after buy).
    post = [
        [100.50, 100.50, 100.50, 100.50, 100.50],  # +50 bps
        [100.50, 100.50, 100.50, 100.50, 100.50],
        [100.50, 100.50, 100.50, 100.50, 100.50],
        [100.50, 100.50, 100.50, 100.50, 100.50],
    ]
    sc = execution_scorecard(fills, post_fill_prices=post)
    # All 4 adverse → rate 1.0 > 0.5 → cap C
    assert sc.adverse_selection_rate == 1.0
    assert sc.n_adverse == 4
    assert sc.n_reversion_checked == 4
    assert sc.grade == "C"


def test_scorecard_adverse_partial_does_not_cap():
    # 1 of 4 adverse (25%) → does not cap. Perfect slippage → A.
    fills = _perfect_fills(4)
    post = [
        [100.50, 100.50, 100.50, 100.50, 100.50],  # adverse
        [100.00, 100.00, 100.00, 100.00, 100.00],  # no move
        [99.50, 99.50, 99.50, 99.50, 99.50],       # favorable
        [100.00, 100.00, 100.00, 100.00, 100.00],  # no move
    ]
    sc = execution_scorecard(fills, post_fill_prices=post)
    assert sc.n_adverse == 1
    assert sc.adverse_selection_rate == 0.25
    assert sc.grade == "A"


def test_scorecard_benchmark_override():
    # Original benchmark 100, override to 100.02 → slippage becomes -2 bps (favorable).
    fills = [
        {"qty": 100, "price": 100.00, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
        {"qty": 100, "price": 100.00, "side": "BUY", "order_qty": 100, "benchmark_price": 100.00},
    ]
    sc = execution_scorecard(fills, benchmark_prices=[100.02, 100.02])
    # fill 100 vs bench 100.02 → slippage = (100-100.02)/100.02*1e4 ≈ -2.0 bps
    assert sc.fill_stats.slippage_bps[0] < 0
    # (100 - 100.02)/100.02 * 1e4 = -1.999600... bps (benchmark in denominator)
    assert abs(sc.fill_stats.mean_abs_slippage_bps - 1.9996) < 1e-3


def test_scorecard_benchmark_override_length_mismatch_raises():
    fills = _perfect_fills(2)
    with pytest.raises(ValueError):
        execution_scorecard(fills, benchmark_prices=[100.0])  # len mismatch


def test_scorecard_post_fill_length_mismatch_raises():
    fills = _perfect_fills(2)
    with pytest.raises(ValueError):
        execution_scorecard(fills, post_fill_prices=[[100.0, 100.0]])  # len mismatch


def test_scorecard_empty_raises():
    with pytest.raises(ValueError):
        execution_scorecard([])


def test_scorecard_to_dict_keys():
    sc = execution_scorecard(_perfect_fills(2))
    d = sc.to_dict()
    for k in (
        "fill_stats", "mean_slippage_bps", "mean_abs_slippage_bps",
        "adverse_selection_rate", "n_adverse", "n_reversion_checked",
        "grade", "reversion_results",
    ):
        assert k in d
    assert isinstance(d["fill_stats"], dict)
    assert d["grade"] == "A"


def test_scorecard_uses_fill_stats_dataclass():
    sc = execution_scorecard(_perfect_fills(2))
    assert isinstance(sc.fill_stats, FillStats)
    # reversion_results empty when no post_fill_prices
    assert sc.reversion_results == []


def test_scorecard_reversion_results_populated():
    fills = _perfect_fills(2)
    post = [
        [100.50, 100.50, 100.50, 100.50, 100.50],
        [100.50, 100.50, 100.50, 100.50, 100.50],
    ]
    sc = execution_scorecard(fills, post_fill_prices=post)
    assert len(sc.reversion_results) == 2
    assert all(isinstance(r, ReversionResult) for r in sc.reversion_results)
    assert all(r.is_adverse for r in sc.reversion_results)


def test_scorecard_skips_empty_post_for_fill():
    # A fill with an empty post sequence is skipped (cannot judge) — no crash, no adverse.
    fills = _perfect_fills(2)
    post: list[list[float]] = [[], [100.50, 100.50, 100.50, 100.50, 100.50]]
    sc = execution_scorecard(fills, post_fill_prices=post)
    assert sc.n_reversion_checked == 1
    assert sc.n_adverse == 1
    assert sc.adverse_selection_rate == 1.0
    # 100% adverse but only 1 checked → rate=1.0 > 0.5 → cap C, but slippage is 0 → A base → capped C
    assert sc.grade == "C"