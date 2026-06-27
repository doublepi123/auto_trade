"""Tests for P318 multitimeframe coherence."""

from __future__ import annotations

import pytest

from app.platform.multitimeframe_coherence import multitimeframe_coherence_report


class TestMultitimeframeCoherenceReport:
    def test_all_same_direction_agreement_one(self):
        """When all timeframes agree on sign, agreement_ratio == 1.0."""
        signals = {
            "1d": [0.5, 0.8, -0.3],
            "1w": [0.2, 0.6, -0.1],
            "1m": [0.9, 1.0, -0.5],
        }
        result = multitimeframe_coherence_report(signals)
        body = result.to_dict()
        assert body["agreement_ratio"] == 1.0
        assert len(body["coherence_scores"]) == 3

    def test_mixed_direction_reduces_agreement(self):
        """Opposing signals lower agreement_ratio below 1.0."""
        signals = {
            "1d": [1.0, -1.0],
            "1w": [-1.0, 1.0],
        }
        result = multitimeframe_coherence_report(signals)
        body = result.to_dict()
        assert 0.0 <= body["agreement_ratio"] < 1.0

    def test_zero_signal_produces_zero_coherence(self):
        """All-zero signals produce coherence_score == 0.0."""
        signals = {
            "1d": [0.0, 0.0],
            "1w": [0.0, 0.0],
        }
        result = multitimeframe_coherence_report(signals)
        body = result.to_dict()
        assert body["agreement_ratio"] == 0.0
        assert body["coherence_scores"] == [0.0, 0.0]

    def test_custom_weights(self):
        """Custom weights are reflected in weighted sum."""
        signals = {
            "1d": [1.0, 1.0],
            "1w": [1.0, 1.0],
        }
        weights = {"1d": 0.8, "1w": 0.2}
        result = multitimeframe_coherence_report(signals, weights=weights)
        body = result.to_dict()
        assert body["agreement_ratio"] == 1.0
        # Weighted average of all ones should be 1.0
        assert body["coherence_scores"] == [1.0, 1.0]

    def test_rejects_empty_signals(self):
        with pytest.raises(ValueError):
            multitimeframe_coherence_report({})

    def test_rejects_unequal_lengths(self):
        with pytest.raises(ValueError):
            multitimeframe_coherence_report({"a": [1.0, 2.0], "b": [1.0]})

    def test_rejects_non_finite_values(self):
        with pytest.raises(ValueError):
            multitimeframe_coherence_report({"a": [float("inf")]})

    def test_rejects_missing_weight_key(self):
        signals = {"1d": [1.0], "1w": [2.0]}
        weights = {"1d": 0.5}
        with pytest.raises(ValueError):
            multitimeframe_coherence_report(signals, weights=weights)
