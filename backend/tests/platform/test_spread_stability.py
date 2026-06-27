"""P322 spread_stability tests — TDD RED phase."""

from __future__ import annotations

import math


class TestSpreadStability:
    """Test spread_stability_report."""

    def test_cointegrated_pair_stable_hedge(self):
        """A constructed cointegrated pair should yield stable hedge ratios."""
        from app.platform.spread_stability import spread_stability_report

        n = 100
        # x is a random walk
        import random
        random.seed(42)
        x = [0.0]
        for _ in range(n - 1):
            x.append(x[-1] + random.gauss(0, 0.01))
        # y = 2 * x + stationary noise (cointegrated)
        y = [2.0 * xi + random.gauss(0, 0.005) for xi in x]

        result = spread_stability_report(y, x, window=30)
        assert len(result.hedge_ratios) > 0
        assert len(result.half_lives) == len(result.hedge_ratios)
        assert len(result.breakdown_flags) == len(result.hedge_ratios)
        # Hedge ratios should be near 2.0
        valid_hr = [h for h in result.hedge_ratios if h is not None]
        assert len(valid_hr) > 0
        avg_hr = sum(valid_hr) / len(valid_hr)
        assert 1.5 < avg_hr < 2.5

    def test_invalid_inputs_raise(self):
        """Invalid inputs raise ValueError."""
        import pytest
        from app.platform.spread_stability import spread_stability_report

        with pytest.raises(ValueError):
            spread_stability_report([1.0, 2.0], [3.0], window=2)

        with pytest.raises(ValueError):
            spread_stability_report([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], window=4)

        with pytest.raises(ValueError):
            spread_stability_report([float('nan'), 2.0], [3.0, 4.0])

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.spread_stability import spread_stability_report

        import random
        random.seed(42)
        n = 60
        x = [0.0]
        for _ in range(n - 1):
            x.append(x[-1] + random.gauss(0, 0.01))
        y = [2.0 * xi + random.gauss(0, 0.005) for xi in x]

        result = spread_stability_report(y, x, window=20)
        d = result.to_dict()
        assert d["hedge_ratios"] == result.hedge_ratios
        assert d["half_lives"] == result.half_lives
        assert d["breakdown_flags"] == result.breakdown_flags
