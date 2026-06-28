"""Tests for P363 rebalancing optimization."""

from __future__ import annotations

import pytest

from app.platform.rebalancing_optimization import rebalancing_optimization_report


class TestRebalancingOptimizationReport:
    def test_frontier_nonempty_and_optimal_steps_positive(self):
        """Frontier is non-empty and optimal_steps >= 1."""
        current = {"A": 0.5, "B": 0.5}
        target = {"A": 0.3, "B": 0.7}
        cov = {
            "A": {"A": 0.04, "B": 0.01},
            "B": {"A": 0.01, "B": 0.09},
        }
        result = rebalancing_optimization_report(current, target, cov, max_steps=5)
        body = result.to_dict()
        assert len(body["frontier"]) > 0
        assert body["optimal_steps"] >= 1

    def test_frontier_length_equals_max_steps(self):
        """Frontier has max_steps entries."""
        current = {"A": 1.0, "B": 0.0}
        target = {"A": 0.0, "B": 1.0}
        cov = {
            "A": {"A": 0.04, "B": 0.01},
            "B": {"A": 0.01, "B": 0.09},
        }
        result = rebalancing_optimization_report(current, target, cov, max_steps=10)
        body = result.to_dict()
        assert len(body["frontier"]) == 10

    def test_current_tracking_error_zero_when_equal(self):
        """Tracking error is zero when current == target."""
        w = {"A": 0.5, "B": 0.5}
        cov = {
            "A": {"A": 0.04, "B": 0.01},
            "B": {"A": 0.01, "B": 0.09},
        }
        result = rebalancing_optimization_report(w, w, cov, max_steps=5)
        body = result.to_dict()
        assert body["current_tracking_error"] == pytest.approx(0.0, abs=1e-10)

    def test_tracking_error_nonzero_when_different(self):
        """Tracking error is positive when current != target."""
        current = {"A": 1.0, "B": 0.0}
        target = {"A": 0.0, "B": 1.0}
        cov = {
            "A": {"A": 0.04, "B": 0.01},
            "B": {"A": 0.01, "B": 0.09},
        }
        result = rebalancing_optimization_report(current, target, cov, max_steps=5)
        body = result.to_dict()
        assert body["current_tracking_error"] > 0

    def test_tracking_error_increases_with_more_steps(self):
        """More steps → intermediate weight further from target → higher TE.

        With k=1, you jump directly to target (TE=0). With k>1, you only
        move partway per step, so the intermediate TE is positive.
        """
        current = {"A": 1.0, "B": 0.0}
        target = {"A": 0.0, "B": 1.0}
        cov = {
            "A": {"A": 0.04, "B": 0.01},
            "B": {"A": 0.01, "B": 0.09},
        }
        result = rebalancing_optimization_report(current, target, cov, max_steps=5)
        body = result.to_dict()
        frontier = body["frontier"]
        # k=1: TE=0 (immediately at target)
        # k=5: TE positive (only 20% toward target)
        assert frontier[0]["tracking_error"] == pytest.approx(0.0, abs=1e-10)
        assert frontier[-1]["tracking_error"] > 0

    def test_rejects_empty_weights(self):
        with pytest.raises(ValueError):
            rebalancing_optimization_report({}, {"A": 1.0}, {"A": {"A": 0.04}}, max_steps=5)

    def test_rejects_mismatched_covariance(self):
        current = {"A": 0.5, "B": 0.5}
        target = {"A": 0.3, "B": 0.7}
        cov = {"A": {"A": 0.04}}  # missing B
        with pytest.raises(ValueError):
            rebalancing_optimization_report(current, target, cov, max_steps=5)
