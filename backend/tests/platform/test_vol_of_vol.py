"""P336: vol-of-vol report tests."""

import pytest

from app.platform.vol_of_vol import (
    VolOfVolResult,
    vol_of_vol_report,
)


class TestVolOfVol:
    def _generate_returns(self, n: int, seed: int = 42) -> list[float]:
        """Generate synthetic returns with time-varying volatility."""
        import random
        rng = random.Random(seed)
        returns: list[float] = []
        for i in range(n):
            # Slowly varying volatility
            vol = 0.01 + 0.005 * abs(rng.gauss(0, 0.3))
            returns.append(rng.gauss(0.0002, vol))
        return returns

    def test_basic_default_windows(self) -> None:
        returns = self._generate_returns(120)
        result = vol_of_vol_report(returns)

        assert isinstance(result, VolOfVolResult)
        assert len(result.per_window) == 3  # default windows [10, 20, 60]
        for w in (10, 20, 60):
            assert w in result.per_window
            entry = result.per_window[w]
            assert entry["vol_of_vol"] > 0
            assert entry["mean_realized_vol"] > 0
            assert entry["vol_of_vol_annualized"] > 0

    def test_custom_windows(self) -> None:
        returns = self._generate_returns(80)
        result = vol_of_vol_report(returns, windows=[5, 10])

        assert len(result.per_window) == 2
        assert 5 in result.per_window
        assert 10 in result.per_window

    def test_constant_returns(self) -> None:
        """Constant returns should give vol_of_vol == 0."""
        returns = [0.01] * 100
        result = vol_of_vol_report(returns, windows=[10, 20])

        for entry in result.per_window.values():
            assert entry["vol_of_vol"] == 0.0

    def test_invalid_empty_returns(self) -> None:
        with pytest.raises(ValueError):
            vol_of_vol_report([])

    def test_invalid_empty_windows(self) -> None:
        with pytest.raises(ValueError):
            vol_of_vol_report([0.01, -0.01], windows=[])

    def test_invalid_window_too_large(self) -> None:
        with pytest.raises(ValueError):
            vol_of_vol_report([0.01, -0.01], windows=[10])

    def test_invalid_non_finite(self) -> None:
        with pytest.raises(ValueError):
            vol_of_vol_report([float("inf"), 0.01])

    def test_to_dict(self) -> None:
        returns = self._generate_returns(80)
        result = vol_of_vol_report(returns, windows=[5, 10])
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_window" in d
        assert "vov_term_structure_slope" in d
        assert "autocorr_lag1" in d

    def test_autocorr_lag1(self) -> None:
        """Verify autocorr_lag1 is computed."""
        returns = [0.01, -0.005, 0.02, -0.01, 0.005, 0.015, -0.02, 0.01,
                   0.03, -0.015, 0.005, 0.01, -0.005, 0.02, -0.01] * 5
        result = vol_of_vol_report(returns, windows=[10, 20, 60])
        assert isinstance(result.autocorr_lag1, float)
