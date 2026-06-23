from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.sdk import OrderIntent

__all__ = [
    "ExecutionAlgorithm",
    "TWAPAlgorithm",
    "VWAPAlgorithm",
    "IcebergAlgorithm",
    "ExecutionAlgorithmRegistry",
    "get_default_registry",
]


@runtime_checkable
class ExecutionAlgorithm(Protocol):
    """算法执行（参考 Nautilus ExecutionAlgorithm）：把父单拆成子切片（按时间/成交量/隐藏量）。"""

    @property
    def name(self) -> str: ...

    def slice(self, intent: OrderIntent) -> list[OrderIntent]: ...


def _child(intent: OrderIntent, quantity: int, label: str) -> OrderIntent:
    return OrderIntent(
        symbol=intent.symbol,
        side=intent.side,
        quantity=quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        reason=label,
    )


@dataclass(frozen=True)
class TWAPAlgorithm:
    """按时间均分：把父单拆成 num_slices 个近似等量的子单（最后一个吸收余数）。"""

    num_slices: int = 5
    name: str = "twap"

    def slice(self, intent: OrderIntent) -> list[OrderIntent]:
        if self.num_slices <= 0:
            raise ValueError("num_slices must be positive")
        total = intent.quantity
        base = total // self.num_slices
        remainder = total - base * self.num_slices
        out: list[OrderIntent] = []
        for i in range(self.num_slices):
            qty = base + (1 if i < remainder else 0)
            if qty <= 0:
                continue
            out.append(_child(intent, qty, f"twap_slice_{i + 1}/{self.num_slices}"))
        return out


@dataclass(frozen=True)
class VWAPAlgorithm:
    """按成交量分布加权：每个时段切片数量 ∝ volume_profile 权重。"""

    volume_profile: tuple[float, ...] = (1.0,)
    name: str = "vwap"

    def slice(self, intent: OrderIntent) -> list[OrderIntent]:
        if not self.volume_profile:
            raise ValueError("volume_profile must be non-empty")
        total_weight = sum(self.volume_profile)
        if total_weight <= 0:
            raise ValueError("volume_profile weights must sum positive")
        total = intent.quantity
        raw = [total * w / total_weight for w in self.volume_profile]
        floor_qtys = [int(r) for r in raw]
        assigned = sum(floor_qtys)
        leftover = total - assigned
        # distribute leftover by largest fractional remainder
        fracs = sorted(range(len(raw)), key=lambda i: raw[i] - floor_qtys[i], reverse=True)
        for k in range(leftover):
            floor_qtys[fracs[k % len(fracs)]] += 1
        out: list[OrderIntent] = []
        n = len(self.volume_profile)
        for i, qty in enumerate(floor_qtys):
            if qty <= 0:
                continue
            out.append(_child(intent, qty, f"vwap_slice_{i + 1}/{n}"))
        return out


@dataclass(frozen=True)
class IcebergAlgorithm:
    """冰山：每次只展示 display_quantity，最后一个吸收余数。"""

    display_quantity: int = 10
    name: str = "iceberg"

    def slice(self, intent: OrderIntent) -> list[OrderIntent]:
        if self.display_quantity <= 0:
            raise ValueError("display_quantity must be positive")
        out: list[OrderIntent] = []
        remaining = intent.quantity
        idx = 0
        while remaining > 0:
            idx += 1
            qty = min(self.display_quantity, remaining)
            out.append(_child(intent, qty, f"iceberg_slice_{idx}"))
            remaining -= qty
        return out


class ExecutionAlgorithmRegistry:
    def __init__(self) -> None:
        self._algos: dict[str, ExecutionAlgorithm] = {}

    def register(self, algo: ExecutionAlgorithm) -> None:
        if algo.name in self._algos:
            raise ValueError(f"ExecutionAlgorithm '{algo.name}' already registered")
        self._algos[algo.name] = algo

    def get(self, name: str) -> ExecutionAlgorithm:
        if name not in self._algos:
            raise KeyError(f"ExecutionAlgorithm '{name}' not found")
        return self._algos[name]

    def list(self) -> list[dict[str, Any]]:
        return [{"name": a.name} for a in self._algos.values()]


_DEFAULT = ExecutionAlgorithmRegistry()
_DEFAULT.register(TWAPAlgorithm())
_DEFAULT.register(VWAPAlgorithm())
_DEFAULT.register(IcebergAlgorithm())


def get_default_registry() -> ExecutionAlgorithmRegistry:
    return _DEFAULT
