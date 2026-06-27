"""P305: volatility forecast comparison diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, validate_series


@dataclass(frozen=True)
class VolForecastComparisonResult:
    metrics: dict[str, dict[str, float]]
    best_model: str

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics, "best_model": self.best_model}


def vol_forecast_comparison_report(realized_vol: list[float], forecasts_panel: dict[str, list[float]]) -> VolForecastComparisonResult:
    realized = validate_series(realized_vol, name="realized_vol", min_len=2)
    if not isinstance(forecasts_panel, dict) or not forecasts_panel:
        raise ValueError("forecasts_panel must be non-empty")
    panel = {str(name): validate_series(series, name=str(name), min_len=len(realized)) for name, series in forecasts_panel.items()}
    if any(len(series) != len(realized) for series in panel.values()):
        raise ValueError("forecast lengths must match realized_vol length")
    if len(panel) > 50:
        raise ValueError("forecasts_panel must contain at most 50 models")
    metrics: dict[str, dict[str, float]] = {}
    for name, forecast in panel.items():
        errors = [f - r for f, r in zip(forecast, realized)]
        rmse = math.sqrt(mean([e * e for e in errors]))
        qlike = mean([r / max(f, 1e-12) - math.log(max(r, 1e-12) / max(f, 1e-12)) - 1 for r, f in zip(realized, forecast)])
        directional = mean([1.0 if (f - realized[i - 1]) * (r - realized[i - 1]) > 0 else 0.0 for i, (f, r) in enumerate(zip(forecast, realized)) if i > 0]) if len(realized) > 1 else 0.0
        mae = mean([abs(e) for e in errors])
        metrics[name] = {"rmse": rmse, "mae": mae, "qlike": qlike, "directional_accuracy": directional}
    best_model = min(metrics, key=lambda name: metrics[name]["rmse"])
    return VolForecastComparisonResult(metrics, best_model)


__all__ = ["VolForecastComparisonResult", "vol_forecast_comparison_report"]
