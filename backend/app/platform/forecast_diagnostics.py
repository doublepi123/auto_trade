"""P279: forecast diagnostics for prediction vs realised returns."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, pearson, spearman, std, validate_pair


@dataclass(frozen=True)
class ForecastDiagnosticsResult:
    n: int
    mse: float
    mae: float
    bias: float
    directional_accuracy: float
    pearson_ic: float
    rank_ic: float
    top_bottom_spread: float
    information_ratio: float | None
    beta_to_benchmark: float | None
    alpha_vs_benchmark: float | None
    bucket_returns: list[dict[str, float | int]]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def forecast_diagnostics_report(predictions: list[float], actuals: list[float], *, benchmark: list[float] | None = None, n_buckets: int = 5) -> ForecastDiagnosticsResult:
    preds, acts = validate_pair(predictions, actuals, x_name="predictions", y_name="actuals")
    if isinstance(n_buckets, bool) or not isinstance(n_buckets, int) or n_buckets < 2 or n_buckets > len(preds):
        raise ValueError("n_buckets must be an int between 2 and input length")
    errors = [p - a for p, a in zip(preds, acts)]
    ordered = sorted(zip(preds, acts), key=lambda item: (item[0], item[1]))
    buckets: list[dict[str, float | int]] = []
    bucket_means: list[float] = []
    for i in range(n_buckets):
        start = i * len(ordered) // n_buckets
        end = (i + 1) * len(ordered) // n_buckets
        rows = ordered[start:end]
        avg_pred = mean([p for p, _ in rows])
        avg_actual = mean([a for _, a in rows])
        bucket_means.append(avg_actual)
        buckets.append({"bucket": i + 1, "count": len(rows), "mean_prediction": avg_pred, "mean_actual": avg_actual})
    beta = alpha = ir = None
    if benchmark is not None:
        _, bench = validate_pair(acts, benchmark, x_name="actuals", y_name="benchmark")
        beta = pearson(acts, bench) * (std(acts) / std(bench)) if std(bench) else 0.0
        alpha = mean(acts) - beta * mean(bench)
        active = [a - b for a, b in zip(acts, bench)]
        ir = 0.0 if std(active, sample=True) == 0 else mean(active) / std(active, sample=True) * math.sqrt(len(active))
    return ForecastDiagnosticsResult(
        n=len(preds),
        mse=mean([e * e for e in errors]),
        mae=mean([abs(e) for e in errors]),
        bias=mean(errors),
        directional_accuracy=sum(1 for p, a in zip(preds, acts) if (p >= 0) == (a >= 0)) / len(preds),
        pearson_ic=pearson(preds, acts),
        rank_ic=spearman(preds, acts),
        top_bottom_spread=bucket_means[-1] - bucket_means[0],
        information_ratio=ir,
        beta_to_benchmark=beta,
        alpha_vs_benchmark=alpha,
        bucket_returns=buckets,
    )


__all__ = ["ForecastDiagnosticsResult", "forecast_diagnostics_report"]
