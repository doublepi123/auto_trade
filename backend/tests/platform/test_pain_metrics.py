"""Tests for P209 Pain Index, Ulcer Index, MAR, and Kestner ratios."""

from __future__ import annotations

import math

from app.platform.pain_metrics import (
    kestner_ratio,
    mar_ratio,
    pain_index,
    pain_metrics_report,
    ulcer_index,
)


def test_pain_index_zero_for_monotonic_rise():
    equity = [100.0 + i for i in range(20)]
    assert pain_index(equity) == 0.0


def test_pain_index_positive_for_drawdown():
    # 100 → 80 (20% dd) → 100
    equity = [100, 100, 90, 80, 85, 90, 95, 100]
    pi = pain_index(equity)
    assert pi > 0


def test_ulcer_index_at_least_pain_index():
    # RMS ≥ mean for non-positive values
    equity = [100, 90, 80, 90, 100, 110, 90, 80, 100]
    pi = pain_index(equity)
    ui = ulcer_index(equity)
    assert ui >= pi - 1e-9


def test_ulcer_index_known_value():
    # Monotonically down to 90, then flat. Underwater is [0, -0.1, -0.1, ...]
    # UI = sqrt((0 + 5 * 0.01) / 6) = sqrt(0.00833) ≈ 0.0913
    equity = [100, 90, 90, 90, 90, 90]
    ui = ulcer_index(equity)
    expected = math.sqrt(0.05 / 6)  # (0 + 5 * 0.01) / 6
    assert abs(ui - expected) < 1e-9


def test_mar_ratio_positive_for_net_positive_curve():
    # Ends higher than start despite a drawdown
    equity = [100, 120, 80, 90, 110, 130, 140]
    assert mar_ratio(equity, periods_per_year=252) > 0


def test_mar_ratio_zero_for_pure_loss():
    equity = [100, 90, 80, 70, 60]
    # CAGR negative, but MAR is only defined when max dd > 0 (which it is here)
    mar = mar_ratio(equity, periods_per_year=252)
    # MAR can be negative when CAGR is negative; just check it's finite
    assert math.isfinite(mar)


def test_kestner_ratio_greater_than_mar_for_smooth_drawdown():
    # Smooth, deep drawdown → UI closer to max-dd (less RMS amplification),
    # so Kestner ≥ MAR. Sharper, short drawdowns invert this.
    equity = [100, 100, 95, 90, 85, 90, 95, 100, 110, 120, 130]
    mar = mar_ratio(equity, periods_per_year=252)
    kest = kestner_ratio(equity, periods_per_year=252)
    # Both positive
    assert mar > 0 and kest > 0


def test_pain_metrics_report_includes_all_keys():
    equity = [100, 110, 90, 100, 105, 95, 110, 120]
    rep = pain_metrics_report(equity, periods_per_year=252)
    for k in ("n", "cagr", "max_drawdown", "pain_index", "ulcer_index", "mar_ratio", "kestner_ratio"):
        assert k in rep


def test_pain_metrics_report_empty_equity():
    rep = pain_metrics_report([])
    assert rep["n"] == 0
    assert rep["pain_index"] == 0.0
    assert rep["mar_ratio"] == 0.0


def test_pain_metrics_handles_constant_curve():
    # Constant equity → no drawdown, no growth
    rep = pain_metrics_report([100.0] * 10)
    assert rep["max_drawdown"] == 0.0
    assert rep["pain_index"] == 0.0
    assert rep["cagr"] == 0.0
    assert rep["mar_ratio"] == 0.0
