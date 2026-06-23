from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["LatencyModel", "FixedLatencyModel"]


@runtime_checkable
class LatencyModel(Protocol):
    """订单延迟模型（参考 Nautilus LatencyModel）：submit/fill 延迟以 bar 数计。"""

    def submit_delay(self) -> int: ...  # bars before an order becomes SUBMITTED (eligible)

    def fill_delay(self) -> int: ...    # bars a matched fill is held before emitting


@dataclass(frozen=True)
class FixedLatencyModel:
    submit_bars: int = 0
    fill_bars: int = 0

    def submit_delay(self) -> int:
        return self.submit_bars

    def fill_delay(self) -> int:
        return self.fill_bars
