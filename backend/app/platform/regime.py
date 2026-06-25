"""P213: Market Regime Detection.

Deterministic regime classifier (BULL / BEAR / SIDEWAYS) combining three
signals over a rolling window:

* **SMA cross** — short-period vs long-period moving average (trend
  direction).
* **Trend strength** — Wilder's ADX when high/low are supplied (the standard
  trend-strength oscillator), falling back to a slope/volatility proxy when
  only closes are available.
* **Realized volatility** — annualized std of returns over the window.

Decision logic (deterministic thresholds):

    directional = BULL  if sma_short > sma_long·(1+thr) and slope > 0
                = BEAR  if sma_short < sma_long·(1−thr) and slope < 0
                = None  otherwise
    strong = adx >= adx_threshold (or slope/vol proxy >= threshold)
    regime = directional if directional is not None and strong else SIDEWAYS

Reference: Nautilus ``MarketRegimeModel`` regime pattern; RiskMetrics/MSCI
regime classification. Pure Python, no new deps; emits ``RegimeEvent`` on the
event bus when the regime changes.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource

__all__ = [
    "Regime",
    "RegimeConfig",
    "RegimeSnapshot",
    "RegimeEvent",
    "RegimeModel",
    "classify",
    "rolling_regime",
    "regime_report",
]


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"


@dataclass(frozen=True)
class RegimeConfig:
    short_period: int = 20
    long_period: int = 50
    vol_window: int = 20
    adx_period: int = 14
    periods_per_year: int = 252
    trend_threshold: float = 0.0
    adx_threshold: float = 25.0
    min_bars: int = 51


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: Regime
    slope: float
    realized_vol: float
    adx: float | None
    sma_short: float
    sma_long: float
    confidence: float
    window: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "slope": self.slope,
            "realized_vol": self.realized_vol,
            "adx": self.adx,
            "sma_short": self.sma_short,
            "sma_long": self.sma_long,
            "confidence": self.confidence,
            "window": self.window,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sma(values: list[float], period: int) -> list[float | None]:
    """Trailing SMA aligned to input length; leading None during warmup."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    acc = sum(values[:period])
    out[period - 1] = acc / period
    for i in range(period, len(values)):
        acc += values[i] - values[i - period]
        out[i] = acc / period
    return out


def _slope(values: list[float]) -> float:
    """Least-squares slope of ``y`` vs ``x = 0..n-1``; 0.0 if degenerate."""
    n = len(values)
    if n < 2:
        return 0.0
    sx = 0.0
    sy = 0.0
    sxx = 0.0
    sxy = 0.0
    for i, v in enumerate(values):
        sx += i
        sy += v
        sxx += i * i
        sxy += i * v
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _realized_vol(returns: list[float], period: int, periods_per_year: int) -> float:
    if len(returns) < 2 or period < 1:
        return 0.0
    window = returns[-period:] if len(returns) >= period else returns
    if len(window) < 2:
        return 0.0
    m = sum(window) / len(window)
    var = sum((r - m) ** 2 for r in window) / (len(window) - 1)
    return math.sqrt(max(var, 0.0)) * math.sqrt(max(periods_per_year, 0))


def _wilder_adx(closes: list[float], highs: list[float], lows: list[float], period: int) -> float | None:
    """Classic Wilder ADX. Returns None if not enough data to warm up."""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    tr: list[float] = [0.0] * n
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    # Wilder smoothing seeded by the sum of the first `period` values.
    smoothed_tr = sum(tr[1 : period + 1])
    smoothed_plus = sum(plus_dm[1 : period + 1])
    smoothed_minus = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []
    for i in range(period + 1, n):
        smoothed_tr = smoothed_tr - smoothed_tr / period + tr[i]
        smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[i]
        smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[i]
        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denom = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0)
    if len(dx_values) < period:
        return None
    # ADX = Wilder-smoothed DX over the period.
    adx = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx = (adx * (period - 1) + dx_values[i]) / period
    return max(0.0, min(100.0, adx))


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


def _check_no_nan(values: list[float], name: str) -> None:
    for v in values:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"NaN/inf in {name}")


def classify(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    config: RegimeConfig | None = None,
) -> RegimeSnapshot:
    """Classify the latest regime from a close (and optional high/low) series."""
    if not closes:
        raise ValueError("series too short for window")
    cfg = config or RegimeConfig()
    _check_no_nan(closes, "closes")
    if highs is not None:
        if len(highs) != len(closes):
            raise ValueError("highs length must match closes")
        _check_no_nan(highs, "highs")
    if lows is not None:
        if len(lows) != len(closes):
            raise ValueError("lows length must match closes")
        _check_no_nan(lows, "lows")
    if len(closes) < cfg.min_bars:
        raise ValueError("series too short for window")

    sma_short_series = _sma(closes, cfg.short_period)
    sma_long_series = _sma(closes, cfg.long_period)
    sma_short = sma_short_series[-1]
    sma_long = sma_long_series[-1]
    if sma_short is None or sma_long is None:
        raise ValueError("series too short for window")

    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] != 0]
    rv = _realized_vol(returns, cfg.vol_window, cfg.periods_per_year)

    # slope over the last `vol_window` short-SMA values that are defined.
    defined_short = [v for v in sma_short_series if v is not None]
    if len(defined_short) >= cfg.vol_window:
        slope = _slope(defined_short[-cfg.vol_window:])
    elif len(defined_short) >= 2:
        slope = _slope(defined_short)
    else:
        slope = 0.0

    adx: float | None = None
    if highs is not None and lows is not None:
        adx = _wilder_adx(closes, highs, lows, cfg.adx_period)

    # trend direction
    if sma_short > sma_long * (1.0 + cfg.trend_threshold) and slope > 0:
        directional = Regime.BULL
    elif sma_short < sma_long * (1.0 - cfg.trend_threshold) and slope < 0:
        directional = Regime.BEAR
    else:
        directional = None

    # trend strength (ADX preferred; slope/vol proxy fallback)
    if adx is not None:
        trend_strength = adx
    else:
        trend_strength = abs(slope) / (rv + 1e-12) * 100.0
    strong = trend_strength >= cfg.adx_threshold

    regime = directional if (directional is not None and strong) else Regime.SIDEWAYS
    confidence = max(0.0, min(1.0, trend_strength / 100.0))
    return RegimeSnapshot(
        regime=regime,
        slope=slope,
        realized_vol=rv,
        adx=adx,
        sma_short=sma_short,
        sma_long=sma_long,
        confidence=confidence,
        window=cfg.vol_window,
    )


def rolling_regime(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    config: RegimeConfig | None = None,
) -> list[RegimeSnapshot | None]:
    """Per-bar regime classification; leading None during warmup."""
    if not closes:
        return []
    cfg = config or RegimeConfig()
    out: list[RegimeSnapshot | None] = [None] * len(closes)
    for i in range(cfg.min_bars, len(closes)):
        sub_closes = closes[: i + 1]
        sub_highs = highs[: i + 1] if highs is not None else None
        sub_lows = lows[: i + 1] if lows is not None else None
        try:
            out[i] = classify(sub_closes, sub_highs, sub_lows, cfg)
        except ValueError:
            out[i] = None
    return out


def regime_report(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    config: RegimeConfig | None = None,
) -> dict[str, Any]:
    """Current snapshot + bull/bear/sideways tallies from the rolling series."""
    if not closes:
        return {"n": 0, "regime": None, "bull": 0, "bear": 0, "sideways": 0}
    rolling = rolling_regime(closes, highs, lows, config)
    bull = sum(1 for r in rolling if r is not None and r.regime == Regime.BULL)
    bear = sum(1 for r in rolling if r is not None and r.regime == Regime.BEAR)
    sideways = sum(1 for r in rolling if r is not None and r.regime == Regime.SIDEWAYS)
    last = next((r for r in reversed(rolling) if r is not None), None)
    return {
        "n": len(closes),
        "regime": last.regime.value if last else None,
        "bull": bull,
        "bear": bear,
        "sideways": sideways,
        "snapshot": last.as_dict() if last else None,
    }


# ---------------------------------------------------------------------------
# event
# ---------------------------------------------------------------------------

# RegimeEvent is defined in app.platform.events and registered in the
# EVENT_REGISTRY there; re-export it here so callers can import from either
# location. RegimeModel.on_bar constructs instances of this canonical class.
from app.platform.events import RegimeEvent  # noqa: E402,F401  (re-export)


class RegimeModel:
    """Streaming regime classifier fed by BarEvent; emits RegimeEvent on change.

    Maintains a per-symbol rolling buffer of (close, high, low, timestamp); on
    each bar past warmup it classifies the regime and, if it changed since the
    last bar for that symbol, publishes a :class:`RegimeEvent` on the bus.
    """

    def __init__(self, config: RegimeConfig | None = None, bus: EventBus | None = None) -> None:
        self.config = config or RegimeConfig()
        self.bus = bus
        self._closes: dict[str, deque] = {}
        self._highs: dict[str, deque] = {}
        self._lows: dict[str, deque] = {}
        self._last_regime: dict[str, Regime] = {}
        self.latest: dict[str, RegimeSnapshot] = {}

    def on_bar(self, bar: BarEvent, bus: EventBus | None = None) -> Event | None:
        sym = bar.symbol or ""
        cap = self.config.min_bars
        if sym not in self._closes:
            self._closes[sym] = deque(maxlen=cap)
            self._highs[sym] = deque(maxlen=cap)
            self._lows[sym] = deque(maxlen=cap)
        self._closes[sym].append(float(bar.close))
        self._highs[sym].append(float(bar.high))
        self._lows[sym].append(float(bar.low))
        if len(self._closes[sym]) < self.config.min_bars:
            return None
        try:
            snap = classify(
                list(self._closes[sym]),
                list(self._highs[sym]),
                list(self._lows[sym]),
                self.config,
            )
        except ValueError:
            return None
        self.latest[sym] = snap
        prev = self._last_regime.get(sym)
        if prev != snap.regime:
            self._last_regime[sym] = snap.regime
            event = RegimeEvent(
                timestamp=bar.timestamp,
                source=EventSource.SYSTEM,
                symbol=sym,
                regime=snap.regime.value,
                slope=snap.slope,
                realized_vol=snap.realized_vol,
                adx=snap.adx,
                sma_short=snap.sma_short,
                sma_long=snap.sma_long,
                confidence=snap.confidence,
                reason="regime_change",
            )
            target_bus = bus or self.bus
            if target_bus is not None:
                target_bus.publish(event)
            return event
        return None

    def snapshot(self, symbol: str | None = None) -> RegimeSnapshot | None:
        if symbol is None:
            if not self.latest:
                return None
            return next(reversed(self.latest.values()))
        return self.latest.get(symbol)