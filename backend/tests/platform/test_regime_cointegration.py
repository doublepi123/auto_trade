"""P337: regime cointegration report tests."""

import pytest

from app.platform.regime_cointegration import (
    RegimeCointegrationResult,
    regime_cointegration_report,
)


class TestRegimeCointegration:
    def test_basic_two_regime(self) -> None:
        """Construct 2-regime data with known hedge ratios."""
        # Regime 0: y = 2*x + noise
        # Regime 1: y = 0.5*x + noise
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
             1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [2.1, 4.0, 5.9, 8.1, 10.0, 12.1, 14.0, 16.1, 17.9, 20.0,
             0.6, 1.0, 1.6, 2.0, 2.6, 3.1, 3.4, 4.1, 4.5, 4.9]
        regimes = ["R0"] * 10 + ["R1"] * 10

        result = regime_cointegration_report(y, x, regimes)

        assert isinstance(result, RegimeCointegrationResult)
        assert "R0" in result.per_regime
        assert "R1" in result.per_regime

        for regime in ("R0", "R1"):
            entry = result.per_regime[regime]
            assert "hedge_ratio" in entry
            assert "half_life" in entry
            assert "residual_autocorr" in entry
            assert "n_samples" in entry
            assert entry["n_samples"] == 10
            assert entry["sufficient"] is True

        # R0 hedge_ratio should be close to 2
        assert abs(result.per_regime["R0"]["hedge_ratio"] - 2.0) < 0.5
        # R1 hedge_ratio should be close to 0.5
        assert abs(result.per_regime["R1"]["hedge_ratio"] - 0.5) < 0.5

    def test_insufficient_samples(self) -> None:
        """Regimes with fewer than min_regime_samples should be marked insufficient."""
        x = list(range(20))
        y = [2 * xi for xi in x]
        regimes = ["A"] * 5 + ["B"] * 15

        result = regime_cointegration_report(y, x, regimes, min_regime_samples=10)

        assert result.per_regime["A"]["sufficient"] is False
        assert result.per_regime["B"]["sufficient"] is True
        assert "A" in result.breakdown_regimes
        assert "B" not in result.breakdown_regimes

    def test_stability_score(self) -> None:
        """stability_score should be finite."""
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
             1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [2.1, 4.0, 5.9, 8.1, 10.0, 12.1, 14.0, 16.1, 17.9, 20.0,
             3.1, 5.9, 9.0, 12.2, 15.1, 18.2, 21.0, 24.1, 26.9, 30.2]
        regimes = ["R0"] * 10 + ["R1"] * 10

        result = regime_cointegration_report(y, x, regimes)

        assert result.stability_score is not None
        assert result.stability_score > 0

    def test_invalid_mismatched_lengths(self) -> None:
        with pytest.raises(ValueError):
            regime_cointegration_report([1.0, 2.0], [1.0], ["A", "B"])

    def test_invalid_empty_input(self) -> None:
        with pytest.raises(ValueError):
            regime_cointegration_report([], [], [])

    def test_invalid_non_finite(self) -> None:
        with pytest.raises(ValueError):
            regime_cointegration_report(
                [float("nan"), 1.0], [1.0, 2.0], ["A", "B"]
            )

    def test_to_dict(self) -> None:
        x = list(range(15))
        y = [2 * xi + 0.5 for xi in x]
        regimes = ["A"] * 15
        result = regime_cointegration_report(y, x, regimes)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_regime" in d
        assert "stability_score" in d
        assert "breakdown_regimes" in d
