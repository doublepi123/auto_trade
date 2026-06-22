from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.platform.events import BarEvent

__all__ = ["Universe", "StaticUniverse", "TopNByVolumeUniverse"]


@runtime_checkable
class Universe(Protocol):
    """标的全集选择（参考 Lean Universe / Nautilus Universe）。

    ``contains`` 在每根 bar 上调用，可基于 bar 更新内部状态（如滚动成交量排名）。
    """

    @property
    def name(self) -> str: ...

    def contains(self, symbol: str, bar: BarEvent | None = None) -> bool: ...


@dataclass
class StaticUniverse:
    """固定标的全集。"""

    symbols: set[str] = field(default_factory=set)
    name: str = "static"

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = set(symbols)
        self.name = "static"

    def contains(self, symbol: str, bar: BarEvent | None = None) -> bool:
        return symbol in self.symbols


@dataclass
class TopNByVolumeUniverse:
    """按滚动成交量排名取前 N 标的（参考 Lean dynamic universe）。"""

    n: int
    lookback: int = 20
    name: str = "topn_volume"

    def __post_init__(self) -> None:
        self._volumes: dict[str, deque[int]] = {}

    def _ranking(self) -> set[str]:
        ranked = sorted(self._volumes.items(), key=lambda kv: sum(kv[1]), reverse=True)
        return {sym for sym, _ in ranked[: self.n]}

    def contains(self, symbol: str, bar: BarEvent | None = None) -> bool:
        if bar is not None and bar.volume is not None:
            buf = self._volumes.setdefault(symbol, deque(maxlen=self.lookback))
            buf.append(int(bar.volume))
        return symbol in self._ranking()
