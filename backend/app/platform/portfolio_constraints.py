"""P288: read-only portfolio constraints and capacity diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class PortfolioConstraintsResult:
    passed: bool
    exposures: dict[str, float]
    turnover: float
    group_weights: dict[str, float]
    capacity: dict[str, Any]
    violations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def portfolio_constraints_report(weights: dict[str, float], *, prev_weights: dict[str, float] | None = None, groups: dict[str, str] | None = None, adv: dict[str, float] | None = None, nav: float = 1.0, constraints: dict[str, float] | None = None) -> PortfolioConstraintsResult:
    if not isinstance(weights, dict) or not weights:
        raise ValueError("weights must be non-empty")
    w = {str(k): _finite(v, "weights") for k, v in weights.items()}
    cons = {str(k): _finite(v, "constraints") for k, v in (constraints or {}).items()}
    nav_value = _finite(nav, "nav")
    if nav_value <= 0:
        raise ValueError("nav must be positive")
    if any(limit < 0 for limit in cons.values()):
        raise ValueError("constraint limits must be non-negative")
    if groups is not None and not isinstance(groups, dict):
        raise ValueError("groups must be a dict")
    violations: list[dict[str, Any]] = []
    gross = sum(abs(v) for v in w.values())
    net = sum(w.values())
    if "max_position_weight" in cons:
        for asset, value in w.items():
            if abs(value) > float(cons["max_position_weight"]):
                violations.append({"constraint": "max_position_weight", "asset": asset, "actual": abs(value), "limit": float(cons["max_position_weight"])})
    prev = {str(k): _finite(v, "prev_weights") for k, v in (prev_weights or {}).items()}
    turnover = sum(abs(w.get(k, 0.0) - prev.get(k, 0.0)) for k in set(w) | set(prev)) / 2.0 if prev else 0.0
    if "max_turnover" in cons and turnover > float(cons["max_turnover"]):
        violations.append({"constraint": "max_turnover", "actual": turnover, "limit": float(cons["max_turnover"])})
    group_weights: dict[str, float] = {}
    if groups:
        for asset, value in w.items():
            group_weights[str(groups.get(asset, "unknown"))] = group_weights.get(str(groups.get(asset, "unknown")), 0.0) + value
    if "max_group_weight" in cons:
        for group, value in group_weights.items():
            if abs(value) > float(cons["max_group_weight"]):
                violations.append({"constraint": "max_group_weight", "group": group, "actual": abs(value), "limit": float(cons["max_group_weight"])})
    participation: dict[str, float] = {}
    if adv:
        for asset, value in w.items():
            adv_value = _finite(adv.get(asset, 0.0), "adv")
            if adv_value <= 0:
                raise ValueError("adv values must be positive")
            participation[asset] = abs(value) * nav_value / adv_value
            if "max_adv_participation" in cons and participation[asset] > float(cons["max_adv_participation"]):
                violations.append({"constraint": "max_adv_participation", "asset": asset, "actual": participation[asset], "limit": float(cons["max_adv_participation"])})
    return PortfolioConstraintsResult(not violations, {"gross": gross, "net": net}, turnover, group_weights, {"participation_rates": participation}, violations)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must contain finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must contain finite numbers")
    return number


__all__ = ["PortfolioConstraintsResult", "portfolio_constraints_report"]
