from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.events import BarEvent

__all__ = [
    "Factor",
    "MomentumFactor",
    "VolatilityFactor",
    "MeanReversionFactor",
    "FactorRegistry",
    "get_default_registry",
    "information_coefficient",
    "pearson",
]


@runtime_checkable
class Factor(Protocol):
    """因子（参考 Alpha101/alphalens/Qlib）：对 bar 序列返回与 bar 对齐的因子值序列（浮点）。

    暖启动期返回 0.0（与 bar 长度对齐）。"""

    @property
    def name(self) -> str: ...

    def compute(self, bars: list[BarEvent]) -> list[float]: ...


def _returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        out.append((closes[i] / prev - 1.0) if prev != 0 else 0.0)
    return out


@dataclass(frozen=True)
class MomentumFactor:
    period: int = 10
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"momentum_{self.period}")

    def compute(self, bars: list[BarEvent]) -> list[float]:
        closes = [float(b.close) for b in bars]
        out = [0.0] * len(bars)
        for i in range(self.period, len(closes)):
            prev = closes[i - self.period]
            out[i] = (closes[i] / prev - 1.0) if prev != 0 else 0.0
        return out


@dataclass(frozen=True)
class VolatilityFactor:
    period: int = 10
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"volatility_{self.period}")

    def compute(self, bars: list[BarEvent]) -> list[float]:
        closes = [float(b.close) for b in bars]
        rets = _returns(closes)
        out = [0.0] * len(bars)
        for i in range(self.period, len(closes)):
            window = rets[i - self.period : i]
            if not window:
                continue
            mean = sum(window) / len(window)
            var = sum((r - mean) ** 2 for r in window) / len(window)
            out[i] = var ** 0.5
        return out


@dataclass(frozen=True)
class MeanReversionFactor:
    """均值回复因子：当前价相对滚动均值的偏离（负 = 高于均线）。"""

    period: int = 10
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"meanrev_{self.period}")

    def compute(self, bars: list[BarEvent]) -> list[float]:
        closes = [float(b.close) for b in bars]
        out = [0.0] * len(bars)
        for i in range(self.period, len(closes)):
            window = closes[i - self.period : i]
            sma = sum(window) / len(window)
            out[i] = (closes[i] - sma) / sma if sma != 0 else 0.0
        return out


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, Factor] = {}

    def register(self, factor: Factor) -> None:
        if factor.name in self._factors:
            raise ValueError(f"Factor '{factor.name}' already registered")
        self._factors[factor.name] = factor

    def get(self, name: str) -> Factor:
        if name not in self._factors:
            raise KeyError(f"Factor '{name}' not found")
        return self._factors[name]

    def list(self) -> list[dict[str, Any]]:
        return [{"name": f.name} for f in self._factors.values()]


def get_default_registry() -> FactorRegistry:
    reg = FactorRegistry()
    reg.register(MomentumFactor())
    reg.register(VolatilityFactor())
    reg.register(MeanReversionFactor())
    return reg


def _pearson(xs: list[float], ys: list[float]) -> float:
    return pearson(xs, ys)


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation between two equal-position series (public helper)."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs = xs[:n]
    ys = ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx == 0 or dy == 0:
        return 0.0
    return num / ((dx * dy) ** 0.5)


def information_coefficient(
    factor_values: dict[str, list[float]],
    forward_returns: dict[str, list[float]],
) -> dict[str, Any]:
    """横截面信息系数（参考 alphalens IC）：在每个时间点对因子的横截面排序与远期收益做相关，再对时间取均值。

    需要 >=2 个标的才有意义。返回 mean_ic、std_ic、ic_ir（mean/std）、per_period 序列。
    """
    symbols = [s for s in factor_values if s in forward_returns]
    if len(symbols) < 2:
        return {"mean_ic": 0.0, "std_ic": 0.0, "ic_ir": 0.0, "num_periods": 0, "per_period": []}
    min_len = min(len(factor_values[s]) for s in symbols)  # type: ignore[arg-type]
    min_len = min(min_len, *(len(forward_returns[s]) for s in symbols))  # type: ignore[arg-type]
    per_period: list[float] = []
    for t in range(min_len):
        xs = [factor_values[s][t] for s in symbols]
        ys = [forward_returns[s][t] for s in symbols]
        ic = _pearson(xs, ys)
        per_period.append(ic)
    if not per_period:
        return {"mean_ic": 0.0, "std_ic": 0.0, "ic_ir": 0.0, "num_periods": 0, "per_period": []}
    mean_ic = sum(per_period) / len(per_period)
    var = sum((v - mean_ic) ** 2 for v in per_period) / len(per_period)
    std_ic = var ** 0.5
    ic_ir = (mean_ic / std_ic) if std_ic > 0 else 0.0
    return {"mean_ic": mean_ic, "std_ic": std_ic, "ic_ir": ic_ir, "num_periods": len(per_period), "per_period": per_period}
