"""P355: Bayesian Model Averaging — unit tests.

Pure-Python BMA: SSE, BIC (k=1), posterior weights ∝ exp(-BIC/2), weighted
ensemble prediction, and per-model diagnostics.
"""

from __future__ import annotations

import pytest

from app.platform.bayesian_model_averaging import (
    BayesianModelAveragingResult,
    bayesian_model_averaging_report,
)


class TestBayesianModelAveraging:
    """BMA unit tests."""

    def test_two_models_good_beats_bad(self) -> None:
        """A well-fitting model receives higher posterior weight than a bad one."""
        actuals = [1.0, 2.0, 3.0, 4.0, 5.0]
        predictions: dict[str, list[float]] = {
            "good": [0.9, 2.1, 3.0, 3.9, 5.1],   # near-perfect
            "bad": [10.0, 20.0, 30.0, 40.0, 50.0],  # wildly off
        }
        result = bayesian_model_averaging_report(predictions, actuals)
        assert result.weights["good"] > result.weights["bad"]
        assert abs(result.weights["good"] + result.weights["bad"] - 1.0) < 1e-9

    def test_equal_models_get_equal_weights(self) -> None:
        """Two models with identical predictions get equal posterior weights."""
        actuals = [1.0, 2.0, 3.0]
        predictions: dict[str, list[float]] = {
            "a": [1.5, 2.5, 3.5],
            "b": [1.5, 2.5, 3.5],
        }
        result = bayesian_model_averaging_report(predictions, actuals)
        assert abs(result.weights["a"] - 0.5) < 1e-9
        assert abs(result.weights["b"] - 0.5) < 1e-9

    def test_bma_predictions_are_weighted_average(self) -> None:
        """BMA prediction is the posterior-weighted average of model predictions."""
        actuals = [1.0, 2.0, 3.0]
        predictions: dict[str, list[float]] = {
            "m1": [0.0, 1.0, 2.0],
            "m2": [2.0, 3.0, 4.0],
        }
        result = bayesian_model_averaging_report(predictions, actuals)
        w1, w2 = result.weights["m1"], result.weights["m2"]
        for i in range(3):
            expected = w1 * predictions["m1"][i] + w2 * predictions["m2"][i]
            assert abs(result.bma_predictions[i] - expected) < 1e-9

    def test_single_model_weight_is_one(self) -> None:
        """A single model gets weight 1.0."""
        actuals = [1.0, 2.0, 3.0]
        predictions: dict[str, list[float]] = {
            "only": [1.0, 2.0, 3.0],
        }
        result = bayesian_model_averaging_report(predictions, actuals)
        assert abs(result.weights["only"] - 1.0) < 1e-9
        assert result.bma_predictions == predictions["only"]

    def test_bma_sse_and_model_bics_present(self) -> None:
        """Result carries bma_sse and per-model BICs."""
        actuals = [1.0, 2.0, 3.0]
        predictions: dict[str, list[float]] = {
            "m1": [1.1, 2.1, 3.1],
        }
        result = bayesian_model_averaging_report(predictions, actuals)
        assert result.bma_sse > 0
        assert "m1" in result.model_bics
        assert result.model_bics["m1"] is not None

    def test_mismatched_lengths_raises_value_error(self) -> None:
        """Mismatched prediction and actual length raises ValueError."""
        actuals = [1.0, 2.0]
        predictions: dict[str, list[float]] = {"m": [1.0, 2.0, 3.0]}
        with pytest.raises(ValueError):
            bayesian_model_averaging_report(predictions, actuals)

    def test_non_numeric_raises_type_error(self) -> None:
        """Non-numeric entries raise TypeError."""
        actuals = [1.0, 2.0]
        predictions: dict[str, list[float]] = {"m": ["x", "y"]}  # type: ignore[dict-item]
        with pytest.raises(TypeError):
            bayesian_model_averaging_report(predictions, actuals)

    def test_to_dict_roundtrip(self) -> None:
        """to_dict returns serialisable dict with all keys."""
        actuals = [1.0, 2.0, 3.0]
        predictions: dict[str, list[float]] = {"m": [1.0, 2.0, 3.0]}
        result = bayesian_model_averaging_report(predictions, actuals)
        d = result.to_dict()
        assert "weights" in d
        assert "bma_predictions" in d
        assert "bma_sse" in d
        assert "model_bics" in d
        assert isinstance(d["bma_predictions"], list)

    def test_infinite_entries_raises_value_error(self) -> None:
        """Infinite predictions raise ValueError."""
        actuals = [1.0, 2.0]
        predictions: dict[str, list[float]] = {"m": [1.0, float("inf")]}
        with pytest.raises(ValueError):
            bayesian_model_averaging_report(predictions, actuals)

    def test_nan_entries_raises_value_error(self) -> None:
        """NaN predictions raise ValueError."""
        actuals = [1.0, 2.0]
        predictions: dict[str, list[float]] = {"m": [1.0, float("nan")]}
        with pytest.raises(ValueError):
            bayesian_model_averaging_report(predictions, actuals)
