"""P323 regime_transitions tests — TDD RED phase."""

from __future__ import annotations


class TestRegimeTransitions:
    """Test regime_transitions_report."""

    def test_bull_bear_transitions(self):
        """bull→bull prob should be > bear→bull since bull tends to continue."""
        from app.platform.regime_transitions import regime_transitions_report

        # bull tends to be persistent, bear occasional
        regimes = ["bull", "bull", "bear", "bull", "bull", "bull", "bear", "bear",
                   "bull", "bull", "bull", "bull", "bear", "bull", "bull", "bull",
                   "bear", "bear", "bull", "bull", "bull", "bull"]
        result = regime_transitions_report(regimes)

        tm = result.transition_matrix
        assert "bull" in tm and "bear" in tm

        # bull→bull probability
        bull_to_bull = tm["bull"].get("bull", 0.0) / sum(tm["bull"].values())
        # bear→bull probability
        bear_to_bull = tm["bear"].get("bull", 0.0) / sum(tm["bear"].values())

        # bull should stay in bull more than bear transitions to bull
        assert bull_to_bull > bear_to_bull

    def test_expected_durations(self):
        """Expected duration for a state computed as 1/(1-p_stay)."""
        from app.platform.regime_transitions import regime_transitions_report

        regimes = ["A", "A", "A", "B", "A", "A", "B", "B", "A", "A", "A"]
        result = regime_transitions_report(regimes)
        assert len(result.expected_durations) >= 2
        for state, dur in result.expected_durations.items():
            assert dur >= 1.0

    def test_steady_state(self):
        """Steady state probabilities sum to 1."""
        from app.platform.regime_transitions import regime_transitions_report

        regimes = ["bull", "bull", "bear", "bull", "bear", "bull", "bull"]
        result = regime_transitions_report(regimes)
        ss = result.steady_state
        assert len(ss) >= 2
        total = sum(ss.values())
        assert abs(total - 1.0) < 0.01

    def test_single_regime(self):
        """Single regime → trivial transition matrix."""
        from app.platform.regime_transitions import regime_transitions_report

        result = regime_transitions_report(["X", "X", "X"])
        tm = result.transition_matrix
        assert len(tm) == 1
        assert tm.get("X", {}).get("X", 0) > 0

    def test_invalid_inputs_raise(self):
        """Invalid inputs raise ValueError."""
        import pytest
        from app.platform.regime_transitions import regime_transitions_report

        with pytest.raises(ValueError):
            regime_transitions_report([])

        with pytest.raises(ValueError):
            regime_transitions_report(["A"])  # need >= 2

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.regime_transitions import regime_transitions_report

        regimes = ["A", "A", "B", "A"]
        result = regime_transitions_report(regimes)
        d = result.to_dict()
        assert d["transition_matrix"] == result.transition_matrix
        assert d["expected_durations"] == result.expected_durations
        assert d["steady_state"] == result.steady_state
