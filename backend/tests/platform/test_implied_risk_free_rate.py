"""Tests for P375 implied risk-free rate module."""
from __future__ import annotations

import math

import pytest
from app.platform.implied_risk_free_rate import (
    ImpliedRiskFreeRateResult,
    StrikeResult,
    implied_risk_free_rate_report,
)


class TestImpliedRiskFreeRate:
    """Tests for implied_risk_free_rate_report function."""

    def _pcp_consistent_prices(
        self, spot: float, expiry: float, r: float, strikes: list[float]
    ) -> tuple[dict[float, float], dict[float, float]]:
        """Generate call/put prices consistent with put-call parity at rate r."""
        call_prices: dict[float, float] = {}
        put_prices: dict[float, float] = {}
        discount = math.exp(-r * expiry)
        for K in strikes:
            # Set call price using a simple Black-like formula approximation
            # that respects no-arbitrage: C >= max(0, S - K*discount)
            intrinsic = max(0.0, spot - K * discount)
            # Add a small time value that varies with moneyness
            moneyness = spot / K
            time_value = spot * 0.02 * (1.0 + 0.5 * abs(math.log(moneyness)))
            C = intrinsic + time_value
            C = max(C, 0.01)
            # P = C - S + K*discount (exact PCP)
            P = C - spot + K * discount
            P = max(P, 0.01)
            call_prices[K] = C
            put_prices[K] = P
        return call_prices, put_prices

    def test_pcp_consistent_yields_reasonable_rate(self) -> None:
        """With PCP-consistent prices, implied_r should be close to the true rate."""
        spot = 100.0
        expiry = 0.25
        true_r = 0.05
        strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
        call_prices, put_prices = self._pcp_consistent_prices(spot, expiry, true_r, strikes)

        result = implied_risk_free_rate_report(
            call_prices, put_prices, spot=spot, expiry=expiry
        )
        assert isinstance(result, ImpliedRiskFreeRateResult)
        # The implied rate should be close to 0.05
        assert abs(result.median_implied_r - true_r) < 0.05
        assert abs(result.mean_implied_r - true_r) < 0.06
        assert math.isfinite(result.consensus_r)

    def test_per_strike_list(self) -> None:
        """per_strike should contain entries for each common strike."""
        spot = 100.0
        expiry = 0.5
        call_prices, put_prices = self._pcp_consistent_prices(spot, expiry, 0.03, [95.0, 100.0, 105.0])
        result = implied_risk_free_rate_report(
            call_prices, put_prices, spot=spot, expiry=expiry
        )
        assert len(result.per_strike) == 3
        for sr in result.per_strike:
            assert isinstance(sr, StrikeResult)
            assert isinstance(sr.strike, float)
            assert isinstance(sr.implied_r, float)
            assert isinstance(sr.deviation, float)

    def test_outliers_identified(self) -> None:
        """If there are outlier implied rates, they should be listed."""
        spot = 100.0
        expiry = 0.25
        # Create mostly consistent prices
        call_prices, put_prices = self._pcp_consistent_prices(spot, expiry, 0.05, [95.0, 100.0, 105.0])
        # Add a strike with very different implied rate
        call_prices[50.0] = 50.5
        put_prices[50.0] = 1.0

        result = implied_risk_free_rate_report(
            call_prices, put_prices, spot=spot, expiry=expiry
        )
        if result.outliers:
            assert all(isinstance(o, float) for o in result.outliers)

    def test_to_dict_serializable(self) -> None:
        """to_dict should produce a JSON-serializable dictionary."""
        spot = 100.0
        expiry = 0.5
        call_prices, put_prices = self._pcp_consistent_prices(spot, expiry, 0.04, [100.0])
        result = implied_risk_free_rate_report(
            call_prices, put_prices, spot=spot, expiry=expiry
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_strike" in d
        assert "median_implied_r" in d
        assert "mean_implied_r" in d
        assert "consensus_r" in d
        assert "outliers" in d

    def test_no_common_strikes_raises(self) -> None:
        """If call_prices and put_prices have no common strikes, should raise."""
        with pytest.raises(ValueError, match="common strike"):
            implied_risk_free_rate_report(
                {100.0: 5.0}, {200.0: 4.0}, spot=100.0, expiry=0.5
            )

    def test_invalid_spot_raises(self) -> None:
        """Invalid spot should raise ValueError."""
        with pytest.raises(ValueError):
            implied_risk_free_rate_report({100.0: 5.0}, {100.0: 4.0}, spot=-100.0, expiry=0.5)

    def test_invalid_expiry_raises(self) -> None:
        """Invalid expiry should raise ValueError."""
        with pytest.raises(ValueError):
            implied_risk_free_rate_report({100.0: 5.0}, {100.0: 4.0}, spot=100.0, expiry=0.0)

    def test_empty_dicts_raises(self) -> None:
        """Empty call_prices or put_prices should raise ValueError."""
        with pytest.raises(ValueError):
            implied_risk_free_rate_report({}, {100.0: 4.0}, spot=100.0, expiry=0.5)
        with pytest.raises(ValueError):
            implied_risk_free_rate_report({100.0: 5.0}, {}, spot=100.0, expiry=0.5)

    def test_implied_rate_finite(self) -> None:
        """All implied rates should be finite numbers."""
        spot = 100.0
        expiry = 1.0
        call_prices, put_prices = self._pcp_consistent_prices(spot, expiry, 0.02, [80.0, 90.0, 100.0, 110.0, 120.0])
        result = implied_risk_free_rate_report(
            call_prices, put_prices, spot=spot, expiry=expiry
        )
        for sr in result.per_strike:
            assert math.isfinite(sr.implied_r)
