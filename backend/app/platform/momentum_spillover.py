"""P307: cross-asset momentum spillover diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, validate_pair


@dataclass(frozen=True)
class MomentumSpilloverResult:
    best_lag: int
    f_statistic: float
    r_squared: float

    def to_dict(self) -> dict[str, Any]:
        return {"best_lag": self.best_lag, "f_statistic": self.f_statistic, "r_squared": self.r_squared}


def momentum_spillover_report(leader_returns: list[float], lagger_returns: list[float], *, max_lag: int = 5) -> MomentumSpilloverResult:
    leader, lagger = validate_pair(leader_returns, lagger_returns, x_name="leader_returns", y_name="lagger_returns")
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or max_lag < 1:
        raise ValueError("max_lag must be a positive int")
    if len(leader) < max_lag + 3:
        raise ValueError("series must contain at least max_lag+3 values")
    best_lag = 1
    best_r2 = -1.0
    best_f = 0.0
    for lag in range(1, max_lag + 1):
        y = lagger[lag:]
        x_lagged = leader[:-lag]
        r2, f_stat = _regression_fit(x_lagged, y)
        if r2 > best_r2:
            best_r2 = r2
            best_lag = lag
            best_f = f_stat
    return MomentumSpilloverResult(best_lag, best_f, best_r2)


def _regression_fit(x: list[float], y: list[float]) -> tuple[float, float]:
    n = len(y)
    if n < 3:
        return 0.0, 0.0
    mx = mean(x)
    my = mean(y)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den_x = sum((xi - mx) ** 2 for xi in x)
    if den_x == 0:
        return 0.0, 0.0
    beta = num / den_x
    alpha = my - beta * mx
    sse = sum((yi - (alpha + beta * xi)) ** 2 for xi, yi in zip(x, y))
    sst = sum((yi - my) ** 2 for yi in y)
    if sst == 0:
        return 0.0, 0.0
    r2 = 1.0 - sse / sst
    df_resid = n - 2
    if df_resid <= 0 or sse == 0:
        return r2, 0.0
    f_stat = (r2 / 1) / ((1 - r2) / df_resid) if r2 < 1 else 0.0
    return r2, f_stat


__all__ = ["MomentumSpilloverResult", "momentum_spillover_report"]
