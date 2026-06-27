"""P270: factor information-coefficient decay diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson, spearman, validate_pair


@dataclass(frozen=True)
class FactorDecayResult:
    decay: dict[str, dict[str, float]]
    best_horizon: str
    half_life_horizon: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"decay": self.decay, "best_horizon": self.best_horizon, "half_life_horizon": self.half_life_horizon}


def factor_decay_report(factor: list[float], forward_returns: dict[str, list[float]]) -> FactorDecayResult:
    if not isinstance(forward_returns, dict) or not forward_returns:
        raise ValueError("forward_returns must be a non-empty horizon mapping")
    decay: dict[str, dict[str, float]] = {}
    if not all(
        isinstance(horizon, str)
        and horizon.isdigit()
        and int(horizon) > 0
        and horizon == str(int(horizon))
        for horizon in forward_returns
    ):
        raise ValueError("forward_returns horizons must be positive integer labels")
    ordered_horizons = sorted(forward_returns, key=int)
    for horizon in ordered_horizons:
        f, r = validate_pair(factor, forward_returns[horizon], x_name="factor", y_name=f"forward_returns[{horizon}]")
        decay[str(horizon)] = {"ic": pearson(f, r), "rank_ic": spearman(f, r)}
    best_horizon = max(decay, key=lambda h: abs(decay[h]["ic"]))
    best_ic = decay[best_horizon]["ic"]
    max_abs = abs(best_ic)
    half_life = None
    if max_abs > 0:
        best_index = ordered_horizons.index(best_horizon)
        for horizon in ordered_horizons[best_index + 1 :]:
            current_ic = decay[horizon]["ic"]
            if (best_ic >= 0 and current_ic <= best_ic * 0.5) or (best_ic < 0 and current_ic >= best_ic * 0.5):
                half_life = horizon
                break
    return FactorDecayResult(decay=decay, best_horizon=best_horizon, half_life_horizon=half_life)


__all__ = ["FactorDecayResult", "factor_decay_report"]
