"""P297: portfolio turnover attribution."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class TurnoverAttributionResult:
    total_turnover: float
    components: dict[str, float]
    per_asset: dict[str, dict[str, float]]
    entered: list[str]
    exited: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def turnover_attribution_report(prev_weights: dict[str, float], current_weights: dict[str, float], *, drifted_weights: dict[str, float] | None = None) -> TurnoverAttributionResult:
    prev = _validate(prev_weights, "prev_weights")
    current = _validate(current_weights, "current_weights")
    drifted = _validate(drifted_weights, "drifted_weights") if drifted_weights is not None else prev
    assets = sorted(set(prev) | set(current) | set(drifted))
    total = _turnover(prev, current, assets)
    drift = _turnover(prev, drifted, assets) if drifted_weights is not None else 0.0
    rebalance = _turnover(drifted, current, assets) if drifted_weights is not None else total
    per_asset = {asset: {"previous": prev.get(asset, 0.0), "drifted": drifted.get(asset, 0.0), "current": current.get(asset, 0.0), "delta": current.get(asset, 0.0) - prev.get(asset, 0.0)} for asset in assets}
    return TurnoverAttributionResult(total, {"drift_turnover": drift, "rebalance_turnover": rebalance}, per_asset, [asset for asset in assets if prev.get(asset, 0.0) == 0 and current.get(asset, 0.0) != 0], [asset for asset in assets if prev.get(asset, 0.0) != 0 and current.get(asset, 0.0) == 0])


def _validate(values: dict[str, float] | None, name: str) -> dict[str, float]:
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{name} must be non-empty")
    return {str(k): _finite(v, name) for k, v in values.items()}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} values must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} values must be finite")
    return number


def _turnover(left: dict[str, float], right: dict[str, float], assets: list[str]) -> float:
    return sum(abs(right.get(asset, 0.0) - left.get(asset, 0.0)) for asset in assets) / 2.0


__all__ = ["TurnoverAttributionResult", "turnover_attribution_report"]
