"""P273: factor panel data-quality diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class FactorDataQualityResult:
    feature_count: int
    issue_count: int
    quality_score: float
    features: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"feature_count": self.feature_count, "issue_count": self.issue_count, "quality_score": self.quality_score, "features": self.features}


def factor_data_quality_report(panel: dict[str, list[float | None]], *, stale_window: int = 3, outlier_z: float = 3.0) -> FactorDataQualityResult:
    if not isinstance(panel, dict) or not panel:
        raise ValueError("panel must be a non-empty mapping")
    if isinstance(stale_window, bool) or not isinstance(stale_window, int) or stale_window < 2:
        raise ValueError("stale_window must be an int >= 2")
    if isinstance(outlier_z, bool) or not isinstance(outlier_z, (int, float)) or not math.isfinite(float(outlier_z)) or outlier_z <= 0:
        raise ValueError("outlier_z must be positive")
    features: dict[str, dict[str, Any]] = {}
    issue_count = 0
    for name, raw_values in panel.items():
        if not isinstance(raw_values, list) or not raw_values:
            raise ValueError("panel values must be non-empty lists")
        values: list[float] = []
        timeline: list[float | None] = []
        missing = 0
        for value in raw_values:
            if value is None:
                missing += 1
                timeline.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError("factor entries must be finite numbers or null")
            number = float(value)
            values.append(number)
            timeline.append(number)
        constant = len(set(values)) <= 1 if values else True
        sigma = std(values)
        mu = mean(values)
        outliers = 0 if sigma == 0 else sum(1 for value in values if abs(value - mu) / sigma >= outlier_z)
        stale_runs = 0
        run = 1
        prev: float | None = None
        for value in timeline:
            if value is None:
                run = 0
                prev = None
                continue
            if prev is not None and value == prev:
                run += 1
                if run == stale_window:
                    stale_runs += 1
            else:
                run = 1
            prev = value
        feature_issues = int(missing > 0) + int(constant) + int(outliers > 0) + int(stale_runs > 0)
        issue_count += feature_issues
        features[str(name)] = {
            "count": len(raw_values),
            "coverage": (len(raw_values) - missing) / len(raw_values),
            "missing_count": missing,
            "is_constant": constant,
            "outlier_count": outliers,
            "stale_run_count": stale_runs,
        }
    quality_score = max(0.0, 1.0 - issue_count / max(1, len(panel) * 4))
    return FactorDataQualityResult(len(panel), issue_count, quality_score, features)


__all__ = ["FactorDataQualityResult", "factor_data_quality_report"]
