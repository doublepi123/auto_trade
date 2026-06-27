"""P320: Factor momentum ranking.

Pure-Python factor momentum estimator: computes each factor's cumulative
return over the trailing ``lookback`` periods, ranks them, and derives a
top-bottom long/short signal.

Public surface
--------------

* **factor_momentum_report(factor_returns, lookback)** → frozen
  :class:`FactorMomentumResult` with ``momentum`` (per-factor scalar),
  ``ranking`` (factor names ordered by descending momentum), and
  ``long_short_signal`` (dict with ``long`` / ``short``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["FactorMomentumResult", "factor_momentum_report"]


@dataclass(frozen=True)
class FactorMomentumResult:
    momentum: dict[str, float]
    ranking: list[str]
    long_short_signal: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "momentum": dict(self.momentum),
            "ranking": list(self.ranking),
            "long_short_signal": dict(self.long_short_signal),
        }


def factor_momentum_report(
    factor_returns: dict[str, list[float]],
    *,
    lookback: int = 12,
) -> FactorMomentumResult:
    if not isinstance(factor_returns, dict) or not factor_returns:
        raise ValueError("factor_returns must be a non-empty dict of factor return series")

    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in factor_returns.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"factor_returns['{name}'] must be a list of finite numbers")
        vec = validate_series(series, name=f"factor_returns['{name}']", min_len=2)
        if length is None:
            length = len(vec)
        elif len(vec) != length:
            raise ValueError("all factor return series must have equal length")
        validated[str(name)] = vec

    if isinstance(lookback, bool) or not isinstance(lookback, int):
        raise ValueError("lookback must be an int >= 2")
    if lookback < 2:
        raise ValueError("lookback must be an int >= 2")

    lb = min(lookback, length)  # type: ignore[arg-type]
    momentum: dict[str, float] = {}
    for name, series in validated.items():
        recent = series[-lb:]
        momentum[name] = sum(recent)

    # Ranking by descending momentum
    ranking = sorted(momentum, key=momentum.get, reverse=True)  # type: ignore[arg-type]

    long_factor = ranking[0]
    short_factor = ranking[-1]
    return FactorMomentumResult(
        momentum=momentum,
        ranking=ranking,
        long_short_signal={"long": long_factor, "short": short_factor},
    )
