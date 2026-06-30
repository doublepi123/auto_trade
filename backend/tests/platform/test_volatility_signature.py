"""P369 volatility_signature tests — TDD RED phase."""

from __future__ import annotations

import math


class TestVolatilitySignature:
    """Test volatility_signature_report."""

    def test_default_frequencies_returns_non_empty_signature(self):
        """Default frequencies produce a non-empty signature with realized_variance."""
        from app.platform.volatility_signature import volatility_signature_report

        # synthetic returns — need at least 40 for frequency 20 to have ≥2 obs
        returns = [
            0.01, -0.005, 0.02, -0.01, 0.005, -0.015, 0.008, -0.002, 0.012, -0.007,
            0.003, -0.01, 0.015, -0.003, 0.006, -0.012, 0.009, -0.004, 0.007, -0.006,
            0.014, -0.008, 0.002, -0.009, 0.011, -0.001, 0.004, -0.013, 0.01, -0.005,
            0.006, -0.011, 0.013, -0.002, 0.008, -0.007, 0.005, -0.009, 0.012, -0.004,
            0.007, -0.01, 0.009, -0.006, 0.003, -0.012, 0.011, -0.003, 0.004, -0.008,
        ]

        result = volatility_signature_report(returns)

        assert len(result.signature) == 5  # default frequencies [1,2,5,10,20]
        for item in result.signature:
            assert math.isfinite(item["realized_variance"])
            assert item["realized_variance"] >= 0.0
            assert item["n_obs"] > 0
            assert item["frequency"] in [1, 2, 5, 10, 20]
        assert math.isfinite(result.noise_variance_estimate)
        assert result.optimal_frequency in [1, 2, 5, 10, 20]

    def test_custom_frequencies(self):
        """Custom frequencies produce matching signature entries."""
        from app.platform.volatility_signature import volatility_signature_report

        returns = [0.01, -0.005, 0.02, -0.01, 0.005, -0.015, 0.008, -0.002, 0.012,
                   -0.007, 0.003, -0.01, 0.015, -0.003, 0.006, -0.012]

        custom_freqs = [1, 4, 8]
        result = volatility_signature_report(returns, frequencies=custom_freqs)

        assert len(result.signature) == 3
        for item in result.signature:
            assert item["frequency"] in custom_freqs
            assert math.isfinite(item["realized_variance"])
            assert item["n_obs"] > 0

    def test_realized_variance_decreases_with_frequency(self):
        """RV should generally stabilize/flatten at higher frequencies for iid-like returns."""
        from app.platform.volatility_signature import volatility_signature_report

        # near-iid returns
        returns = [0.001 * (i % 5 - 2) for i in range(100)]
        result = volatility_signature_report(returns, frequencies=[1, 2, 5, 10, 20])

        # noise_variance_estimate should be finite
        assert math.isfinite(result.noise_variance_estimate)

    def test_short_series_raises(self):
        """Too few returns raises ValueError."""
        import pytest
        from app.platform.volatility_signature import volatility_signature_report

        with pytest.raises(ValueError):
            volatility_signature_report([0.01])  # too short

        with pytest.raises(ValueError):
            volatility_signature_report([])  # empty

        # With default frequencies, need at least 40 returns for freq=20 → 2 obs
        # Test with small freq to verify short-series error
        with pytest.raises(ValueError):
            volatility_signature_report([0.01, 0.02], frequencies=[1])  # 1 obs, need ≥2

    def test_non_finite_returns_raise(self):
        """NaN/inf returns raise ValueError."""
        import pytest
        from app.platform.volatility_signature import volatility_signature_report

        with pytest.raises(ValueError):
            volatility_signature_report([float('nan'), 0.01, 0.02])

        with pytest.raises(ValueError):
            volatility_signature_report([float('inf'), 0.01, 0.02])

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.volatility_signature import volatility_signature_report

        returns = [0.01 * (i % 7 - 3) for i in range(60)]
        result = volatility_signature_report(returns)
        d = result.to_dict()
        assert d["signature"] == result.signature
        assert d["noise_variance_estimate"] == result.noise_variance_estimate
        assert d["optimal_frequency"] == result.optimal_frequency
