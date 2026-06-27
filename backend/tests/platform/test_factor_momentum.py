"""Tests for P320 factor momentum."""

from __future__ import annotations

import pytest

from app.platform.factor_momentum import factor_momentum_report


class TestFactorMomentumReport:
    def test_ranking_orders_by_momentum(self):
        """Factor with positive cumulative returns ranks higher."""
        factor_returns = {
            "momentum": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
            "value": [-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0, -8.0, -9.0, -10.0, -11.0, -12.0],
        }
        result = factor_momentum_report(factor_returns, lookback=12)
        body = result.to_dict()
        ranking = body["ranking"]
        # momentum should rank #1, value #2
        assert ranking[0] == "momentum"
        assert ranking[-1] == "value"
        # momentum > 0, value < 0
        assert body["momentum"]["momentum"] > 0
        assert body["momentum"]["value"] < 0
        # long_short_signal: long momentum, short value
        assert body["long_short_signal"]["long"] == "momentum"
        assert body["long_short_signal"]["short"] == "value"

    def test_lookback_subset(self):
        """lookback < full length uses only the most recent returns."""
        factor_returns = {
            "f1": [100.0, 0.1, 0.2, 0.3],
            "f2": [-100.0, 0.1, 0.2, 0.3],
        }
        # With lookback=3, only [0.1, 0.2, 0.3] are used
        result = factor_momentum_report(factor_returns, lookback=3)
        body = result.to_dict()
        # Both should have the same recent momentum
        assert body["momentum"]["f1"] == pytest.approx(body["momentum"]["f2"])

    def test_single_factor(self):
        """Single factor: long_short_signal long==short."""
        factor_returns = {"f": [1.0, 2.0, 3.0]}
        result = factor_momentum_report(factor_returns, lookback=3)
        body = result.to_dict()
        assert body["long_short_signal"]["long"] == "f"
        assert body["long_short_signal"]["short"] == "f"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            factor_momentum_report({})

    def test_rejects_unequal_lengths(self):
        with pytest.raises(ValueError):
            factor_momentum_report({"a": [1.0, 2.0], "b": [1.0]})

    def test_rejects_lookback_too_small(self):
        with pytest.raises(ValueError):
            factor_momentum_report({"a": [1.0, 2.0]}, lookback=1)
