"""Momentum indicators matching TA-Lib conventions (pure Python).

MACD, Bollinger Bands, Stochastic, Williams %R and OBV. EMA seeding uses the
SMA of the first ``period`` values (TA-Lib ``TA_MA_CLASSIC``), Bollinger Bands
use the population standard deviation (``ddof=0``), and Stochastic / Williams %R
emit ``0.0`` on a flat (``HH == LL``) window instead of NaN.

Each indicator is exposed two ways: a pure function on plain float arrays
(returning a frozen dataclass with ``to_dict()``) for one-shot/API use, and a
single-output ``Indicator``-compatible class (``compute(bars) -> Decimal|None``)
for the streaming ``IndicatorService``.

Reference: TA-Lib C source (ta_MACD.c, ta_BBANDS.c, ta_STOCH.c, ta_WILLR.c,
ta_OBV.c) and pandas-ta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.platform.events import BarEvent

__all__ = [
    "MacdValue",
    "BollingerValue",
    "StochValue",
    "macd",
    "bollinger",
    "stochastic",
    "williams_r",
    "obv",
    "MACDLine",
    "MACDSignal",
    "MACDHistogram",
    "BollingerUpper",
    "BollingerMiddle",
    "BollingerLower",
    "StochK",
    "StochD",
    "WilliamsR",
    "OBV",
]


@dataclass(frozen=True)
class MacdValue:
    macd: float
    signal: float
    histogram: float

    def to_dict(self) -> dict[str, float]:
        return {"macd": self.macd, "signal": self.signal, "histogram": self.histogram}


@dataclass(frozen=True)
class BollingerValue:
    upper: float
    middle: float
    lower: float

    def to_dict(self) -> dict[str, float]:
        return {"upper": self.upper, "middle": self.middle, "lower": self.lower}


@dataclass(frozen=True)
class StochValue:
    k: float
    d: float

    def to_dict(self) -> dict[str, float]:
        return {"k": self.k, "d": self.d}


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """EMA aligned to ``values``; SMA-seeded at index ``period-1`` (TA-Lib)."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = (values[i] - ema) * k + ema
        out[i] = ema
    return out


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MacdValue | None:
    """MACD line / signal / histogram at the last close (TA-Lib seeding)."""
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("MACD periods must be positive")
    if len(closes) < slow + signal - 1:
        return None
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    macd_line: list[float] = []
    for i in range(slow - 1, len(closes)):
        fe = fast_ema[i]
        se = slow_ema[i]
        if fe is None or se is None:
            raise ValueError("fast period must not exceed slow period")
        macd_line.append(fe - se)
    k_sig = 2.0 / (signal + 1)
    sig = sum(macd_line[:signal]) / signal
    for value in macd_line[signal:]:
        sig = (value - sig) * k_sig + sig
    last_macd = macd_line[-1]
    return MacdValue(macd=last_macd, signal=sig, histogram=last_macd - sig)


def bollinger(
    closes: list[float],
    period: int = 20,
    nbdev: float = 2.0,
) -> BollingerValue | None:
    """Bollinger Bands at the last close (population stddev, ddof=0)."""
    if period <= 0:
        raise ValueError("Bollinger period must be positive")
    if nbdev < 0:
        raise ValueError("nbdev must be non-negative")
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return BollingerValue(
        upper=middle + nbdev * std,
        middle=middle,
        lower=middle - nbdev * std,
    )


def stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    fastk: int = 14,
    slowk: int = 3,
    slowd: int = 3,
) -> StochValue | None:
    """Slow stochastic %K/%D at the last bar (HH==LL emits 0.0)."""
    if fastk <= 0 or slowk <= 0 or slowd <= 0:
        raise ValueError("Stochastic periods must be positive")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal length")
    n = len(closes)
    if n < fastk + slowk + slowd - 2:
        return None
    raw_k: list[float] = []
    for i in range(fastk - 1, n):
        hh = max(highs[i - fastk + 1 : i + 1])
        ll = min(lows[i - fastk + 1 : i + 1])
        diff = hh - ll
        raw_k.append(100.0 * (closes[i] - ll) / diff if diff != 0.0 else 0.0)
    slow_k: list[float] = []
    for i in range(slowk - 1, len(raw_k)):
        slow_k.append(sum(raw_k[i - slowk + 1 : i + 1]) / slowk)
    slow_d: list[float] = []
    for i in range(slowd - 1, len(slow_k)):
        slow_d.append(sum(slow_k[i - slowd + 1 : i + 1]) / slowd)
    if not slow_k or not slow_d:
        return None
    return StochValue(k=slow_k[-1], d=slow_d[-1])


def williams_r(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """Williams %R at the last bar (HH==LL emits 0.0); range [-100, 0]."""
    if period <= 0:
        raise ValueError("Williams %R period must be positive")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal length")
    if len(closes) < period:
        return None
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    diff = hh - ll
    if diff == 0.0:
        return 0.0
    return -100.0 * (hh - closes[-1]) / diff


def obv(closes: list[float], volumes: list[float]) -> list[float]:
    """On-Balance Volume series; ``obv[0] == volumes[0]`` (TA-Lib convention)."""
    if len(closes) != len(volumes):
        raise ValueError("closes and volumes must have equal length")
    if not closes:
        return []
    result = [float(volumes[0])]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            result.append(result[-1] + float(volumes[i]))
        elif closes[i] < closes[i - 1]:
            result.append(result[-1] - float(volumes[i]))
        else:
            result.append(result[-1])
    return result


def _closes(bars: list[BarEvent]) -> list[float]:
    return [float(bar.close) for bar in bars]


def _highs(bars: list[BarEvent]) -> list[float]:
    return [float(bar.high) for bar in bars]


def _lows(bars: list[BarEvent]) -> list[float]:
    return [float(bar.low) for bar in bars]


def _volumes(bars: list[BarEvent]) -> list[float]:
    return [float(bar.volume) for bar in bars]


@dataclass(frozen=True)
class MACDLine:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"macd_line_{self.fast}_{self.slow}_{self.signal}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = macd(_closes(bars), fast=self.fast, slow=self.slow, signal=self.signal)
        return None if value is None else Decimal(str(value.macd))


@dataclass(frozen=True)
class MACDSignal:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"macd_signal_{self.fast}_{self.slow}_{self.signal}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = macd(_closes(bars), fast=self.fast, slow=self.slow, signal=self.signal)
        return None if value is None else Decimal(str(value.signal))


@dataclass(frozen=True)
class MACDHistogram:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"macd_histogram_{self.fast}_{self.slow}_{self.signal}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = macd(_closes(bars), fast=self.fast, slow=self.slow, signal=self.signal)
        return None if value is None else Decimal(str(value.histogram))


@dataclass(frozen=True)
class BollingerUpper:
    period: int = 20
    nbdev: float = 2.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"bollinger_upper_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = bollinger(_closes(bars), period=self.period, nbdev=self.nbdev)
        return None if value is None else Decimal(str(value.upper))


@dataclass(frozen=True)
class BollingerMiddle:
    period: int = 20
    nbdev: float = 2.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"bollinger_middle_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = bollinger(_closes(bars), period=self.period, nbdev=self.nbdev)
        return None if value is None else Decimal(str(value.middle))


@dataclass(frozen=True)
class BollingerLower:
    period: int = 20
    nbdev: float = 2.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"bollinger_lower_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = bollinger(_closes(bars), period=self.period, nbdev=self.nbdev)
        return None if value is None else Decimal(str(value.lower))


@dataclass(frozen=True)
class StochK:
    fastk: int = 14
    slowk: int = 3
    slowd: int = 3
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"stoch_k_{self.fastk}_{self.slowk}_{self.slowd}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = stochastic(
            _highs(bars), _lows(bars), _closes(bars),
            fastk=self.fastk, slowk=self.slowk, slowd=self.slowd,
        )
        return None if value is None else Decimal(str(value.k))


@dataclass(frozen=True)
class StochD:
    fastk: int = 14
    slowk: int = 3
    slowd: int = 3
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"stoch_d_{self.fastk}_{self.slowk}_{self.slowd}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = stochastic(
            _highs(bars), _lows(bars), _closes(bars),
            fastk=self.fastk, slowk=self.slowk, slowd=self.slowd,
        )
        return None if value is None else Decimal(str(value.d))


@dataclass(frozen=True)
class WilliamsR:
    period: int = 14
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"williams_r_{self.period}")

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        value = williams_r(_highs(bars), _lows(bars), _closes(bars), period=self.period)
        return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class OBV:
    name: str = "obv"

    def compute(self, bars: list[BarEvent]) -> Decimal | None:
        if not bars:
            return None
        series = obv(_closes(bars), _volumes(bars))
        return Decimal(str(series[-1])) if series else None
