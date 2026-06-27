"""P321 causal_impact tests — TDD RED phase."""

from __future__ import annotations

import math


class TestCausalImpact:
    """Test causal_impact_report."""

    def test_flat_control_jump_target(self):
        """Control is flat, target jumps post-intervention → effect > 0."""
        from app.platform.causal_impact import causal_impact_report

        control = [10.0] * 20
        target = [10.0] * 10 + [13.0] * 10  # jump at index 10
        result = causal_impact_report(target, control, intervention_index=10)
        assert result.causal_effect > 0.0
        assert math.isfinite(result.causal_effect)
        assert result.standard_error >= 0.0
        assert 0.0 <= result.p_value <= 1.0
        assert result.n_pre == 10
        assert result.n_post == 10
        # Check that effect is close to 3.0 (the jump)
        assert 2.0 < result.causal_effect < 4.0

    def test_no_effect_when_no_change(self):
        """When target follows control exactly, effect ~= 0."""
        from app.platform.causal_impact import causal_impact_report

        control = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        target = list(control)  # identical
        result = causal_impact_report(target, control, intervention_index=5)
        assert abs(result.causal_effect) < 0.01

    def test_invalid_inputs_raise(self):
        """Invalid inputs raise ValueError."""
        import pytest
        from app.platform.causal_impact import causal_impact_report

        with pytest.raises(ValueError):
            causal_impact_report([1.0, 2.0], [3.0, 4.0], intervention_index=3)  # too small

        with pytest.raises(ValueError):
            causal_impact_report([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], intervention_index=2)  # n_post < 2

        with pytest.raises(ValueError):
            causal_impact_report([1.0, 2.0], [3.0], intervention_index=1)  # length mismatch

        with pytest.raises(ValueError):
            causal_impact_report([float('nan'), 2.0], [3.0, 4.0], intervention_index=1)  # NaN

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.causal_impact import causal_impact_report

        control = [10.0] * 20
        target = [10.0] * 10 + [13.0] * 10
        result = causal_impact_report(target, control, intervention_index=10)
        d = result.to_dict()
        assert d["causal_effect"] == result.causal_effect
        assert d["standard_error"] == result.standard_error
        assert d["p_value"] == result.p_value
        assert d["n_pre"] == result.n_pre
        assert d["n_post"] == result.n_post
