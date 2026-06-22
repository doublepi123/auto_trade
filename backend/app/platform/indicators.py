from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.platform.events import BarEvent

__all__ = ["Indicator", "SMA", "EMA", "RSI", "ATR", "IndicatorService"]


@runtime_checkable
class Indicator(Protocol):
    """参考 Backtrader Indicator / TA-Lib：对 bar 序列计算一个标量。"""

    @property
    def name(self) -> str: ...

    def compute(self, bars: list[BarEvent]) -> Decimal | None: ...


@dataclass(frozen=True)
class SMA:
    period: int
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"sma_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        if len(bars) < self.period:
            return None
        window = bars[-self.period:]
        return sum((b.close for b in window), Decimal("0")) / Decimal(self.period)


@dataclass(frozen=True)
class EMA:
    period: int
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"ema_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        if len(bars) < self.period:
            return None
        multiplier = Decimal("2") / (Decimal(self.period) + Decimal("1"))
        ema = sum((b.close for b in bars[:self.period]), Decimal("0")) / Decimal(self.period)
        for bar in bars[self.period:]:
            ema = (bar.close - ema) * multiplier + ema
        return ema


@dataclass(frozen=True)
class RSI:
    period: int = 14
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"rsi_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        if len(bars) < self.period + 1:
            return None
        gains: list[Decimal] = []
        losses: list[Decimal] = []
        for i in range(1, len(bars)):
            change = bars[i].close - bars[i - 1].close
            gains.append(change if change > 0 else Decimal("0"))
            losses.append(-change if change < 0 else Decimal("0"))
        avg_gain = sum(gains[:self.period], Decimal("0")) / Decimal(self.period)
        avg_loss = sum(losses[:self.period], Decimal("0")) / Decimal(self.period)
        for i in range(self.period, len(gains)):
            avg_gain = (avg_gain * Decimal(self.period - 1) + gains[i]) / Decimal(self.period)
            avg_loss = (avg_loss * Decimal(self.period - 1) + losses[i]) / Decimal(self.period)
        if avg_loss == 0:
            return Decimal("100")
        rs = avg_gain / avg_loss
        return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


@dataclass(frozen=True)
class ATR:
    period: int = 14
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"atr_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        if len(bars) < self.period + 1:
            return None
        trs: list[Decimal] = []
        for i in range(1, len(bars)):
            prev_close = bars[i - 1].close
            bar = bars[i]
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)
        window = trs[-self.period:]
        return sum(window, Decimal("0")) / Decimal(self.period)


class IndicatorService:
    """维护每标的 bar 滚动缓冲并缓存最新指标值（参考 Backtrader Lines/Iterator 缓存）。"""

    def __init__(self, indicators: list[Indicator], buffer_size: int = 500) -> None:
        self._indicators = {ind.name: ind for ind in indicators}
        self._buffers: dict[str, deque[BarEvent]] = {}
        self._buffer_size = buffer_size
        self._cache: dict[str, dict[str, Decimal | None]] = {}

    def on_bar(self, bar: BarEvent) -> None:
        symbol = bar.symbol or ""
        buf = self._buffers.setdefault(symbol, deque(maxlen=self._buffer_size))
        buf.append(bar)
        bars = list(buf)
        symbol_cache: dict[str, Decimal | None] = {}
        for name, ind in self._indicators.items():
            symbol_cache[name] = ind.compute(bars)
        self._cache[symbol] = symbol_cache

    def value(self, symbol: str, name: str) -> Decimal | None:
        return self._cache.get(symbol, {}).get(name)

    def snapshot(self, symbol: str) -> dict[str, Decimal | None]:
        return dict(self._cache.get(symbol, {}))
