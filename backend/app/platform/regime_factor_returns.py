"""P299: regime-conditional factor return diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class RegimeFactorReturnsResult:
    regimes: dict[str, dict[str, float]]
    overall: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def regime_factor_returns_report(factor: dict[str, float], returns: dict[str, float], regimes: list[str]) -> RegimeFactorReturnsResult:
    fac = _validate_map(factor, "factor")
    ret = _validate_map(returns, "returns")
    if set(fac) != set(ret):
        raise ValueError("factor and returns keys must match")
    if not isinstance(regimes, list) or len(regimes) != len(fac):
        raise ValueError("regimes length must match factor length")
    reg_labels = set(regimes)
    bucket: dict[str, list[tuple[float, float]]] = {label: [] for label in reg_labels}
    for asset, label in zip(fac, regimes):
        bucket[str(label)].append((fac[asset], ret[asset]))
    out: dict[str, dict[str, float]] = {}
    for label in sorted(reg_labels):
        pairs = bucket[label]
        ics = _rank_ic([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 2 else 0.0
        rets = [p[1] for p in pairs]
        wins = sum(1 for r in rets if r > 0)
        out[label] = {"mean_return": mean(rets), "std_return": std(rets) if len(rets) > 1 else 0.0, "win_rate": wins / len(rets) if rets else 0.0, "rank_ic": ics, "count": float(len(pairs))}
    all_rets = list(ret.values())
    return RegimeFactorReturnsResult(out, {"mean_return": mean(all_rets), "std_return": std(all_rets), "win_rate": sum(1 for r in all_rets if r > 0) / len(all_rets)})


def _validate_map(values: dict[str, float], name: str) -> dict[str, float]:
    if not isinstance(values, dict) or len(values) < 2:
        raise ValueError(f"{name} must contain at least two assets")
    if len(values) > 50:
        raise ValueError(f"{name} must contain at most 50 assets")
    return {str(k): _finite(v, name) for k, v in values.items()}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} values must be finite numbers")
    return number


def _rank_ic(factor_values: list[float], return_values: list[float]) -> float:
    return _spearman(factor_values, return_values)


def _spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 2:
        return 0.0
    rx = _ranks(x)
    ry = _ranks(y)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return 0.0
    return num / (sx * sy)


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    out = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg = (i + 1 + j) / 2.0
        for k in range(i, j):
            out[indexed[k][0]] = avg
        i = j
    return out


__all__ = ["RegimeFactorReturnsResult", "regime_factor_returns_report"]
