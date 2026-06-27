"""P290: variance risk premium diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_pair


@dataclass(frozen=True)
class VarianceRiskPremiumResult:
    series: list[dict[str, float]]
    latest: dict[str, float | str]
    summary: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"series": self.series, "latest": self.latest, "summary": self.summary}


def variance_risk_premium_report(returns: list[float], implied_vols: list[float], *, periods_per_year: int = 252) -> VarianceRiskPremiumResult:
    rets, ivs = validate_pair(returns, implied_vols, x_name="returns", y_name="implied_vols")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if any(iv <= 0 for iv in ivs):
        raise ValueError("implied_vols must be positive")
    rows: list[dict[str, float]] = []
    vrps: list[float] = []
    for idx, iv in enumerate(ivs):
        window = rets[: idx + 1]
        realized = mean([ret * ret for ret in window]) * periods_per_year
        implied = iv * iv
        vrp = implied - realized
        vrps.append(vrp)
        sigma = std(vrps)
        z_score = 0.0 if sigma == 0 else (vrp - mean(vrps)) / sigma
        rows.append({"index": float(idx), "realized_variance": realized, "implied_variance": implied, "vrp": vrp, "z_score": z_score})
    latest_row = rows[-1]
    latest: dict[str, float | str] = dict(latest_row)
    z_score = latest_row["z_score"]
    latest["state"] = "rich" if z_score > 1 else "cheap" if z_score < -1 else "neutral"
    return VarianceRiskPremiumResult(rows, latest, {"mean_vrp": mean(vrps), "std_vrp": std(vrps), "latest_vrp": vrps[-1]})


__all__ = ["VarianceRiskPremiumResult", "variance_risk_premium_report"]
