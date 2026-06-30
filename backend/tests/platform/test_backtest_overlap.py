"""Tests for P379 backtest overlap module."""

from __future__ import annotations

import pytest

from app.platform.backtest_overlap import (
    BacktestOverlapResult,
    backtest_overlap_report,
)


def test_backtest_overlap_returns_result():
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    assert isinstance(result, BacktestOverlapResult)


def test_n_trials_positive():
    """With train=60, test=12, total=1200, step=12, n_trials should be > 0."""
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    assert result.n_trials > 0
    # n_trials = (1200 - 60 - 12) / 12 + 1 = 1128/12 + 1 = 94 + 1 = 95
    assert result.n_trials == 95


def test_overlap_rate_in_0_1():
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    assert 0.0 <= result.overlap_rate <= 1.0


def test_effective_sample_size_positive():
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    assert result.effective_sample_size > 0


def test_bias_adjustment_positive():
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    assert result.bias_adjustment > 0


def test_to_dict_roundtrip():
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=12)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "n_trials" in d
    assert "overlap_rate" in d
    assert "effective_sample_size" in d
    assert "bias_adjustment" in d


def test_maximal_overlap_step_1():
    """With step=1, overlap should be very high."""
    result = backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=1)
    assert result.n_trials > 500  # many overlapping trials
    assert result.overlap_rate > 0.5


def test_no_overlap_large_step():
    """With large step = test_window, overlap should be minimal."""
    result = backtest_overlap_report(train_window=60, test_window=30, total_periods=1200, step=30)
    assert result.overlap_rate < 0.5  # should be low when step = test_window


def test_validation_errors():
    """Test that invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        backtest_overlap_report(train_window=0, test_window=12, total_periods=1200)
    with pytest.raises(ValueError):
        backtest_overlap_report(train_window=60, test_window=0, total_periods=1200)
    with pytest.raises(ValueError):
        backtest_overlap_report(train_window=60, test_window=12, total_periods=50)
    with pytest.raises(ValueError):
        backtest_overlap_report(train_window=60, test_window=12, total_periods=1200, step=0)
