"""P292: ensemble forecast blending diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, pearson, validate_series, std


@dataclass(frozen=True)
class EnsembleBlendingResult:
    weights: dict[str, float]
    model_scores: dict[str, dict[str, float]]
    ensemble_r2: float
    contributions: dict[str, float]
    redundant_pairs: list[dict[str, float | str]]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def ensemble_blending_report(predictions_panel: dict[str, list[float]], actuals: list[float], *, redundancy_threshold: float = 0.95) -> EnsembleBlendingResult:
    y = validate_series(actuals, name="actuals", min_len=2)
    if not isinstance(predictions_panel, dict) or not predictions_panel:
        raise ValueError("predictions_panel must be non-empty")
    if isinstance(redundancy_threshold, bool) or not isinstance(redundancy_threshold, (int, float)) or not math.isfinite(float(redundancy_threshold)):
        raise ValueError("redundancy_threshold must be a finite number")
    threshold = float(redundancy_threshold)
    if threshold < 0 or threshold > 1:
        raise ValueError("redundancy_threshold must be in [0, 1]")
    panel = {str(name): validate_series(series, name=str(name), min_len=len(y)) for name, series in predictions_panel.items()}
    if any(len(series) != len(y) for series in panel.values()):
        raise ValueError("prediction lengths must match actuals")
    scores: dict[str, dict[str, float]] = {}
    inv_mse: dict[str, float] = {}
    for name, pred in panel.items():
        mse = mean([(a - b) ** 2 for a, b in zip(pred, y)])
        r2 = _r2(pred, y)
        scores[name] = {"mse": mse, "r2": r2}
        inv_mse[name] = 1.0 / (mse + 1e-12)
    total = sum(inv_mse.values())
    weights = {name: value / total for name, value in inv_mse.items()}
    blended = [sum(weights[name] * panel[name][i] for name in panel) for i in range(len(y))]
    redundant = []
    names = list(panel)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            corr = pearson(panel[left], panel[right]) if std(panel[left]) and std(panel[right]) else 0.0
            if abs(corr) >= threshold:
                redundant.append({"left": left, "right": right, "correlation": corr})
    return EnsembleBlendingResult(weights, scores, _r2(blended, y), {name: weights[name] * scores[name]["r2"] for name in panel}, redundant)


def _r2(pred: list[float], actual: list[float]) -> float:
    base = mean(actual)
    sst = sum((value - base) ** 2 for value in actual)
    if sst == 0:
        return 0.0
    sse = sum((a - p) ** 2 for p, a in zip(pred, actual))
    return 1.0 - sse / sst


__all__ = ["EnsembleBlendingResult", "ensemble_blending_report"]
