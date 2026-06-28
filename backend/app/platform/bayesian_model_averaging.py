"""P355: Bayesian Model Averaging (BMA) for forecast combination.

Given a dictionary of model predictions and actual values, compute per-model SSE,
BIC (with k=1 parameter), posterior model weights proportional to exp(-BIC/2),
and the BMA ensemble prediction as the posterior-weighted average.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "BayesianModelAveragingResult",
    "bayesian_model_averaging_report",
]


def _validate_numeric_list(values: Sequence[float], label: str) -> list[float]:
    """Validate and coerce a sequence of finite floats."""
    if not isinstance(values, list):
        values = list(values)  # type: ignore[arg-type]
    if not values:
        raise ValueError(f"{label} must be non-empty")
    result: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{label} entries must be finite numbers")
        number = float(v)
        if not math.isfinite(number):
            raise ValueError(f"{label} entries must be finite numbers")
        result.append(number)
    return result


@dataclass(frozen=True)
class BayesianModelAveragingResult:
    """Frozen carrier for the BMA report.

    Attributes
    ----------
    weights: Posterior model weights (normalised to sum to 1).
    bma_predictions: Weighted-average ensemble predictions.
    bma_sse: Sum of squared errors of the BMA prediction against actuals.
    model_bics: Per-model BIC values (k=1).
    """

    weights: dict[str, float]
    bma_predictions: list[float]
    bma_sse: float
    model_bics: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "bma_predictions": self.bma_predictions,
            "bma_sse": self.bma_sse,
            "model_bics": self.model_bics,
        }


def bayesian_model_averaging_report(
    predictions: dict[str, list[float]],
    actuals: list[float],
) -> BayesianModelAveragingResult:
    """Compute BMA ensemble forecast from a set of model predictions.

    Parameters
    ----------
    predictions: {model_name: [predicted_values]}.
    actuals: Ground-truth values, same length as each prediction list.

    Returns a frozen result with posterior weights, ensemble predictions,
    ensemble SSE, and per-model BICs.

    Raises ValueError/TypeError on invalid input.
    """
    if not isinstance(predictions, dict) or not predictions:
        raise ValueError("predictions must be a non-empty dict")
    actuals_validated = _validate_numeric_list(actuals, "actuals")
    n = len(actuals_validated)

    model_names: list[str] = []
    model_predictions: list[list[float]] = []
    for name, preds in predictions.items():
        name_str = str(name)
        preds_validated = _validate_numeric_list(preds, f"predictions['{name_str}']")
        if len(preds_validated) != n:
            raise ValueError(
                f"predictions['{name_str}'] length {len(preds_validated)} "
                f"!= actuals length {n}"
            )
        model_names.append(name_str)
        model_predictions.append(preds_validated)

    # Compute SSE and BIC for each model (k=1 parameter count).
    model_sse: list[float] = []
    model_bic: list[float] = []
    for preds in model_predictions:
        sse = sum((p - a) ** 2 for p, a in zip(preds, actuals_validated))
        model_sse.append(sse)
        if sse > 0.0:
            bic = n * math.log(sse / n) + 1.0 * math.log(n)
        else:
            # Perfect fit: SSE = 0, BIC → -∞, but we use a small floor.
            bic = -float("inf")  # This model will dominate.
        model_bic.append(bic)

    # Posterior weights: w_i ∝ exp(-BIC_i / 2).
    # Handle -inf BIC: exp(+inf) → we handle by setting the weight to 1.
    has_inf = any(b == -float("inf") for b in model_bic)
    if has_inf:
        # Models with -inf BIC get weight 1/(# of -inf models), others get 0.
        inf_indices = [i for i, b in enumerate(model_bic) if b == -float("inf")]
        weight_val = 1.0 / len(inf_indices)
        model_weights = [
            weight_val if i in inf_indices else 0.0 for i in range(len(model_bic))
        ]
    else:
        # Compute exp(-BIC/2) safely.
        # Subtract min BIC first for numerical stability.
        min_bic = min(model_bic)
        raw_weights = [math.exp(-(b - min_bic) / 2.0) for b in model_bic]
        total = sum(raw_weights)
        model_weights = [w / total for w in raw_weights]

    # BMA ensemble predictions.
    bma_preds: list[float] = []
    for t in range(n):
        weighted_sum = sum(
            model_weights[i] * model_predictions[i][t]
            for i in range(len(model_names))
        )
        bma_preds.append(weighted_sum)

    # BMA SSE against actuals.
    bma_sse = sum(
        (bma_preds[t] - actuals_validated[t]) ** 2 for t in range(n)
    )

    weights_dict = {name: model_weights[i] for i, name in enumerate(model_names)}
    bics_dict = {name: model_bic[i] if model_bic[i] != -float("inf") else float("-inf") for i, name in enumerate(model_names)}

    return BayesianModelAveragingResult(
        weights=weights_dict,
        bma_predictions=bma_preds,
        bma_sse=bma_sse,
        model_bics=bics_dict,
    )
