"""P315: Correlation risk premium – implied vs realized correlation spread.

Computes the correlation risk premium (CRP) as the mean difference between
implied and realized correlation series, with z-score and regime labeling.

Reference: correlation risk premium literature in options/fx markets.
Pure-Python — no scipy, no NumPy, no RNG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_pair

__all__ = ["CorrelationRiskPremiumResult", "correlation_risk_premium_report"]


@dataclass(frozen=True)
class CorrelationRiskPremiumResult:
    crp: float
    z_score: float
    regime: str
    realized_mean: float
    implied_mean: float
    spread_series: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "crp": self.crp,
            "z_score": self.z_score,
            "regime": self.regime,
            "realized_mean": self.realized_mean,
            "implied_mean": self.implied_mean,
            "spread_series": self.spread_series,
        }


def correlation_risk_premium_report(
    realized_corr: list[float],
    implied_corr: list[float],
) -> CorrelationRiskPremiumResult:
    """Compute the correlation risk premium.

    CRP = mean(implied - realized), with rolling z-score and regime label.

    Args:
        realized_corr: Realized correlation time series.
        implied_corr: Implied correlation time series (same length).

    Returns:
        CorrelationRiskPremiumResult with crp, z_score, regime, and spread series.
    """
    realized, implied = validate_pair(
        realized_corr, implied_corr,
        x_name="realized_corr", y_name="implied_corr",
    )

    spread = [imp - real for imp, real in zip(implied, realized)]
    crp = mean(spread)
    sigma = std(spread)

    if sigma > 0:
        z = crp / sigma
    else:
        z = 0.0

    if z > 1.0:
        regime = "rich"
    elif z < -1.0:
        regime = "cheap"
    else:
        regime = "normal"

    return CorrelationRiskPremiumResult(
        crp=crp,
        z_score=z,
        regime=regime,
        realized_mean=mean(realized),
        implied_mean=mean(implied),
        spread_series=spread,
    )
