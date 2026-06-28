"""Tests for P348 vol surface arbitrage detection module."""

from __future__ import annotations

import pytest

from app.platform.vol_surface_arbitrage import (
    VolSurfaceArbitrageResult,
    vol_surface_arbitrage_report,
)


class TestVolSurfaceArbitrage:
    def test_no_arbitrage_clean_surface(self):
        """A well-behaved IV surface should have no violations."""
        options = [
            {"strike": 95.0, "expiry_days": 30, "iv": 0.25, "type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.22, "type": "call"},
            {"strike": 105.0, "expiry_days": 30, "iv": 0.24, "type": "call"},
            {"strike": 95.0, "expiry_days": 60, "iv": 0.27, "type": "call"},
            {"strike": 100.0, "expiry_days": 60, "iv": 0.24, "type": "call"},
            {"strike": 105.0, "expiry_days": 60, "iv": 0.26, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert isinstance(result, VolSurfaceArbitrageResult)
        assert result.has_arbitrage is False
        assert result.violation_count == 0
        assert len(result.calendar_violations) == 0
        assert len(result.butterfly_violations) == 0
        assert len(result.pcp_violations) == 0

    def test_calendar_arbitrage_inverted(self):
        """Same strike, longer expiry has lower IV → calendar arbitrage."""
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.30, "type": "call"},
            {"strike": 100.0, "expiry_days": 60, "iv": 0.20, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert result.has_arbitrage is True
        assert result.violation_count >= 1
        assert len(result.calendar_violations) >= 1

    def test_butterfly_arbitrage_convexity(self):
        """Middle IV too high relative to neighbors → butterfly arbitrage."""
        options = [
            {"strike": 95.0, "expiry_days": 30, "iv": 0.20, "type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.50, "type": "call"},
            {"strike": 105.0, "expiry_days": 30, "iv": 0.20, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert result.has_arbitrage is True
        assert len(result.butterfly_violations) >= 1

    def test_put_call_parity_violation(self):
        """Same strike + expiry, call IV != put IV (beyond tolerance)."""
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.25, "type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.40, "type": "put"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert result.has_arbitrage is True
        assert len(result.pcp_violations) >= 1

    def test_put_call_parity_within_tolerance(self):
        """Call and put IVs within tolerance should not trigger PCP violation."""
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.25, "type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.2501, "type": "put"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert len(result.pcp_violations) == 0

    def test_to_dict(self):
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.30, "type": "call"},
            {"strike": 100.0, "expiry_days": 60, "iv": 0.20, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "calendar_violations" in d
        assert "butterfly_violations" in d
        assert "pcp_violations" in d
        assert "has_arbitrage" in d
        assert "violation_count" in d
        assert d["has_arbitrage"] is True

    def test_empty_options_raises(self):
        with pytest.raises(ValueError):
            vol_surface_arbitrage_report([], spot=100.0)

    def test_invalid_option_type_raises(self):
        with pytest.raises(ValueError):
            vol_surface_arbitrage_report([
                {"strike": 100.0, "expiry_days": 30, "iv": 0.30, "type": "invalid"}
            ], spot=100.0)

    def test_non_finite_iv_raises(self):
        with pytest.raises(ValueError):
            vol_surface_arbitrage_report([
                {"strike": 100.0, "expiry_days": 30, "iv": float("nan"), "type": "call"}
            ], spot=100.0)

    def test_negative_spot_raises(self):
        with pytest.raises(ValueError):
            vol_surface_arbitrage_report([
                {"strike": 100.0, "expiry_days": 30, "iv": 0.30, "type": "call"}
            ], spot=-100.0)

    def test_multiple_arbitrage_types(self):
        """A surface with both calendar and butterfly arbitrage."""
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.30, "type": "call"},
            {"strike": 100.0, "expiry_days": 60, "iv": 0.20, "type": "call"},
            {"strike": 95.0, "expiry_days": 30, "iv": 0.20, "type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.50, "type": "call"},
            {"strike": 105.0, "expiry_days": 30, "iv": 0.20, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert result.has_arbitrage is True
        assert result.violation_count >= 2

    def test_single_option_no_arbitrage(self):
        """Single option should have no arbitrage (nothing to compare)."""
        options = [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.25, "type": "call"},
        ]
        result = vol_surface_arbitrage_report(options, spot=100.0)
        assert result.has_arbitrage is False
        assert result.violation_count == 0
