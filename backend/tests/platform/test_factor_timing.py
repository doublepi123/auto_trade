"""Tests for P362 factor timing."""

from __future__ import annotations

import pytest

from app.platform.factor_timing import factor_timing_report


class TestFactorTimingReport:
    def test_high_ic_factor_ranks_higher(self):
        """Factor with higher recent IC ranks higher in timing_score."""
        factor_ic = {
            "momentum": [0.05, 0.06, 0.04, 0.05, 0.07, 0.05, 0.06, 0.04, 0.05, 0.06, 0.08, 0.07],
            "value": [-0.03, -0.02, -0.04, -0.01, -0.03, -0.02, -0.04, -0.03, -0.02, -0.01, -0.03, -0.02],
        }
        factor_returns = {
            "momentum": [0.01, 0.02, 0.01, 0.03, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.03, 0.02],
            "value": [0.01, -0.01, 0.02, -0.01, 0.01, -0.01, 0.02, -0.01, 0.01, -0.01, 0.01, -0.01],
        }
        result = factor_timing_report(factor_ic, factor_returns, lookback=6)
        body = result.to_dict()
        ranking = body["ranking"]
        # momentum has higher recent IC, should rank first
        assert ranking[0] == "momentum"
        assert ranking[-1] == "value"
        # momentum should have positive timing_score
        signals = {s["factor"]: s for s in body["factor_signals"]}
        assert signals["momentum"]["timing_score"] > signals["value"]["timing_score"]

    def test_tilt_classification(self):
        """Tilt is overweight/underweight/neutral based on score thresholds."""
        # Use non-constant IC to produce meaningful z-scores
        factor_ic = {
            "strong": [0.10, 0.12, 0.08, 0.15, 0.11, 0.09, 0.13, 0.07, 0.14, 0.10, 0.12, 0.11],
            "weak": [-0.10, -0.08, -0.12, -0.07, -0.11, -0.09, -0.13, -0.06, -0.10, -0.08, -0.12, -0.11],
        }
        factor_returns = {
            "strong": [0.02, 0.03, 0.01, 0.04, 0.02, 0.03, 0.01, 0.05, 0.02, 0.03, 0.04, 0.03],
            "weak": [-0.02, -0.01, -0.03, -0.01, -0.02, -0.03, -0.01, -0.04, -0.02, -0.01, -0.02, -0.03],
        }
        result = factor_timing_report(factor_ic, factor_returns, lookback=6)
        body = result.to_dict()
        signals = {s["factor"]: s for s in body["factor_signals"]}
        # strong factor with positive IC should rank higher
        assert signals["strong"]["timing_score"] > signals["weak"]["timing_score"]
        # strong factor should be overweight
        assert signals["strong"]["tilt"] == "overweight"
        # weak factor should be underweight or at least ranked lower
        assert signals["weak"]["tilt"] in ("underweight", "neutral")

    def test_lookback_subset(self):
        """lookback uses only the most recent periods."""
        factor_ic = {
            "f": [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        }
        factor_returns = {
            "f": [0.01] * 12,
        }
        # lookback=3: only [0.0, 0.0, 0.0] used, mean = 0
        result = factor_timing_report(factor_ic, factor_returns, lookback=3)
        body = result.to_dict()
        signals = {s["factor"]: s for s in body["factor_signals"]}
        # IC mean over lookback=3 is 0, full-sample mean is 0.25, std > 0
        # valuation_z = (0 - 0.25) / std < 0 → negative valuation contribution
        assert signals["f"]["valuation_z"] < 0

    def test_rejects_empty_dict(self):
        with pytest.raises(ValueError):
            factor_timing_report({}, {})

    def test_rejects_unequal_lengths(self):
        with pytest.raises(ValueError):
            factor_timing_report(
                {"a": [1.0, 2.0], "b": [1.0]},
                {"a": [0.01, 0.02], "b": [0.01]},
            )

    def test_rejects_missing_factor_returns(self):
        with pytest.raises(ValueError):
            factor_timing_report(
                {"a": [1.0, 2.0]},
                {"b": [0.01, 0.02]},
            )

    def test_all_fields_present(self):
        """Each factor_signal has all required fields."""
        factor_ic = {"f": [0.05] * 12}
        factor_returns = {"f": [0.01] * 12}
        result = factor_timing_report(factor_ic, factor_returns, lookback=6)
        body = result.to_dict()
        for signal in body["factor_signals"]:
            assert "factor" in signal
            assert "valuation_z" in signal
            assert "crowding" in signal
            assert "momentum" in signal
            assert "timing_score" in signal
            assert "tilt" in signal
