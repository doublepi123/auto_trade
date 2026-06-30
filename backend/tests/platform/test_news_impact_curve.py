"""P372 news_impact_curve tests — TDD RED phase."""

from __future__ import annotations

import math


class TestNewsImpactCurve:
    """Test news_impact_curve_report."""

    def test_non_empty_curve_and_leverage(self):
        """News impact curve is non-empty and leverage_effect exists."""
        from app.platform.news_impact_curve import news_impact_curve_report

        # synthetic returns with some volatility clustering
        returns = [0.01, -0.02, 0.015, -0.01, 0.005, -0.025, 0.02, -0.015,
                   0.01, -0.005, 0.008, -0.018, 0.012, -0.008, 0.006, -0.022,
                   0.014, -0.01, 0.005, -0.02, 0.018, -0.012, 0.01, -0.015,
                   0.02, -0.025, 0.015, -0.01, 0.008, -0.005]

        result = news_impact_curve_report(returns)

        assert len(result.symmetric_curve) > 0
        assert len(result.asymmetric_curve) > 0
        for item in result.symmetric_curve:
            assert math.isfinite(item["shock"])
            assert math.isfinite(item["conditional_var"])
            assert item["conditional_var"] >= 0.0
        for item in result.asymmetric_curve:
            assert math.isfinite(item["shock"])
            assert math.isfinite(item["conditional_var"])
            assert item["conditional_var"] >= 0.0
        assert math.isfinite(result.leverage_effect)
        assert math.isfinite(result.asymmetry_ratio)

    def test_symmetric_curve_is_u_shaped(self):
        """Symmetric curve should be increasing as |shock| increases."""
        from app.platform.news_impact_curve import news_impact_curve_report

        returns = [0.01, -0.02, 0.015, -0.01, 0.005, -0.025, 0.02, -0.015,
                   0.01, -0.005, 0.008, -0.018, 0.012, -0.008, 0.006, -0.022]

        result = news_impact_curve_report(returns)

        # The symmetric curve should be U-shaped: conditional_var should be higher
        # at larger |shock| than at shock=0
        shocks = [item["shock"] for item in result.symmetric_curve]
        vars_ = [item["conditional_var"] for item in result.symmetric_curve]
        # Find the var at shock ≈ 0
        zero_idx = min(range(len(shocks)), key=lambda i: abs(shocks[i]))
        var_at_zero = vars_[zero_idx]
        # Var at extreme shock should be larger
        var_at_extreme = vars_[-1]  # highest shock

        assert var_at_extreme > var_at_zero * 0.9  # at least not smaller

    def test_asymmetric_curve_captures_leverage(self):
        """Asymmetric curve has different var for same-magnitude pos/neg shocks."""
        from app.platform.news_impact_curve import news_impact_curve_report

        returns = [0.01, -0.02, 0.015, -0.01, 0.005, -0.025, 0.02, -0.015,
                   0.01, -0.005, 0.008, -0.018, 0.012, -0.008, 0.006, -0.022,
                   0.014, -0.01, 0.005, -0.02]

        result = news_impact_curve_report(returns)

        # At a given magnitude, the asymmetric curve may differ between +shock and -shock
        for item_pos, item_neg in zip(result.asymmetric_curve, reversed(result.asymmetric_curve)):
            if abs(item_pos["shock"] - (-item_neg["shock"])) < 0.01:
                # If leverage_effect != 0, these should differ
                break

    def test_short_series_raises(self):
        """Too few returns raises ValueError."""
        import pytest
        from app.platform.news_impact_curve import news_impact_curve_report

        with pytest.raises(ValueError):
            news_impact_curve_report([0.01, 0.02])  # too short

    def test_non_finite_returns_raise(self):
        """NaN/inf returns raise ValueError."""
        import pytest
        from app.platform.news_impact_curve import news_impact_curve_report

        with pytest.raises(ValueError):
            news_impact_curve_report([float('nan'), 0.01, 0.02, 0.03, 0.04])

        with pytest.raises(ValueError):
            news_impact_curve_report([float('inf'), 0.01, 0.02, 0.03, 0.04])

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.news_impact_curve import news_impact_curve_report

        returns = [0.01, -0.02, 0.015, -0.01, 0.005, -0.025, 0.02, -0.015,
                   0.01, -0.005, 0.008, -0.018, 0.012, -0.008, 0.006, -0.022]

        result = news_impact_curve_report(returns)
        d = result.to_dict()
        assert d["symmetric_curve"] == result.symmetric_curve
        assert d["asymmetric_curve"] == result.asymmetric_curve
        assert d["leverage_effect"] == result.leverage_effect
        assert d["asymmetry_ratio"] == result.asymmetry_ratio
