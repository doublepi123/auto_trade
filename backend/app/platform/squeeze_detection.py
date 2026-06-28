"""P352: Squeeze detection — Bollinger Bands vs Keltner Channel.

A "squeeze" occurs when Bollinger Bands are fully inside the Keltner Channel,
indicating compressed volatility that typically precedes a breakout.

Public surface
--------------
* **squeeze_detection_report(prices, bb_window, kc_window, bb_mult, kc_mult)**
  — frozen :class:`SqueezeDetectionResult` with squeeze status, band series,
  squeeze-point indices, and post-squeeze direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SqueezeDetectionResult",
    "squeeze_detection_report",
]

_MIN_WINDOW = 2
"""Minimum window size for rolling computations."""


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _validate_prices(prices: Sequence[float], min_window: int) -> list[float]:
    """Coerce ``prices`` to ``list[float]``, validating each entry.

    Raises ``ValueError`` for empty, non-finite, non-numeric, or boolean
    entries, or if the series is shorter than ``min_window``.
    """
    if isinstance(prices, list):
        materialised = prices
    else:
        try:
            materialised = list(prices)
        except TypeError as exc:
            raise ValueError("prices must be a sequence of finite numbers") from exc
    if len(materialised) < min_window:
        raise ValueError(f"prices must contain at least {min_window} values")
    coerced: list[float] = []
    for value in materialised:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("prices entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("prices entries must be finite numbers")
        coerced.append(number)
    return coerced


def _require_positive_int(value: Any, name: str) -> int:
    """Validate a positive integer parameter."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int")
    if value < _MIN_WINDOW:
        raise ValueError(f"{name} must be >= {_MIN_WINDOW}")
    return value


def _require_positive_float(value: Any, name: str) -> float:
    """Validate a positive finite float parameter."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


# ---------------------------------------------------------------------------
# rolling statistics
# ---------------------------------------------------------------------------


def _rolling_sma(prices: list[float], window: int) -> list[float | None]:
    """Simple moving average. Returns None for indices < window - 1."""
    n = len(prices)
    result: list[float | None] = [None] * n
    if n < window:
        return result
    # Use running sum for O(n)
    running_sum = sum(prices[:window])
    result[window - 1] = running_sum / window
    for i in range(window, n):
        running_sum += prices[i] - prices[i - window]
        result[i] = running_sum / window
    return result


def _rolling_std(prices: list[float], sma: list[float | None], window: int) -> list[float | None]:
    """Rolling population standard deviation. Returns None for indices < window - 1."""
    n = len(prices)
    result: list[float | None] = [None] * n
    if n < window:
        return result
    # First valid index
    for i in range(window - 1, n):
        mu = sma[i]
        if mu is None:
            continue
        var_sum = 0.0
        for j in range(i - window + 1, i + 1):
            diff = prices[j] - mu
            var_sum += diff * diff
        result[i] = math.sqrt(var_sum / window)
    return result


def _atr(prices: list[float], window: int) -> list[float | None]:
    """Average True Range approximated from a single price series using |diff|.

    For a single price series, ATR uses abs(price_i - price_{i-1}) as the
    true range proxy (since high/low/close are not available).
    Returns None for the first index and indices before window bars.
    """
    n = len(prices)
    result: list[float | None] = [None] * n
    if n < 2:
        return result
    # True range approximation: abs(diff)
    tr: list[float] = [0.0]  # first bar has no TR
    for i in range(1, n):
        tr.append(abs(prices[i] - prices[i - 1]))
    # Rolling average of TR
    if n < window:
        return result
    # First valid ATR at index window-1: average tr[0..window-1]
    # tr[0] = 0.0 (no prior close for bar 0), subsequent entries are |diff|.
    running_sum = sum(tr[:window])
    result[window - 1] = running_sum / window
    for i in range(window, n):
        running_sum += tr[i] - tr[i - window]
        result[i] = running_sum / window
    return result


# ---------------------------------------------------------------------------
# squeeze detection
# ---------------------------------------------------------------------------


def _squeeze_direction(prices: list[float], window: int) -> str:
    """Guess the post-squeeze breakout direction from the most recent trend.

    Compares the last ``window`` prices to their SMA: positive slope → "up",
    negative slope → "down", flat → "neutral".
    """
    if len(prices) < 2:
        return "neutral"
    tail = prices[-min(window, len(prices)):]
    if len(tail) < 2:
        return "neutral"
    # Simple linear slope: (last - first) / (n - 1)
    slope = (tail[-1] - tail[0]) / (len(tail) - 1)
    if slope > 1e-8:
        return "up"
    elif slope < -1e-8:
        return "down"
    return "neutral"


@dataclass(frozen=True)
class SqueezeDetectionResult:
    """Result of :func:`squeeze_detection_report`."""

    squeeze_on: bool
    squeeze_points: list[int]
    bb_upper: list[float | None]
    bb_lower: list[float | None]
    kc_upper: list[float | None]
    kc_lower: list[float | None]
    direction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "squeeze_on": self.squeeze_on,
            "squeeze_points": self.squeeze_points,
            "bb_upper": self.bb_upper,
            "bb_lower": self.bb_lower,
            "kc_upper": self.kc_upper,
            "kc_lower": self.kc_lower,
            "direction": self.direction,
        }


def squeeze_detection_report(
    prices: Sequence[float],
    *,
    bb_window: int = 20,
    kc_window: int = 20,
    bb_mult: float = 2.0,
    kc_mult: float = 1.5,
) -> SqueezeDetectionResult:
    """Detect Bollinger Band / Keltner Channel squeeze.

    A squeeze is active at index ``i`` when both:

    * ``BB_lower[i] > KC_lower[i]`` — BB lower band is *above* KC lower band.
    * ``BB_upper[i] < KC_upper[i]`` — BB upper band is *below* KC upper band.

    In other words, Bollinger Bands are fully *inside* the Keltner Channel.

    Parameters
    ----------
    prices:
        Price series (close prices).
    bb_window:
        Rolling window for Bollinger Band SMA and standard deviation.
    kc_window:
        Rolling window for Keltner Channel SMA and ATR.
    bb_mult:
        Standard-deviation multiplier for Bollinger Bands.
    kc_mult:
        ATR multiplier for Keltner Channel.

    Returns
    -------
    SqueezeDetectionResult
        Frozen dataclass with squeeze status, squeeze-point indices, band
        series, and post-squeeze direction estimate.

    Raises
    ------
    ValueError
        On any invalid input.
    """
    bb_window_int = _require_positive_int(bb_window, "bb_window")
    kc_window_int = _require_positive_int(kc_window, "kc_window")
    bb_mult_float = _require_positive_float(bb_mult, "bb_mult")
    kc_mult_float = _require_positive_float(kc_mult, "kc_mult")
    max_window = max(bb_window_int, kc_window_int)

    data = _validate_prices(prices, max_window)
    n = len(data)

    # Bollinger Bands
    sma_bb = _rolling_sma(data, bb_window_int)
    std_bb = _rolling_std(data, sma_bb, bb_window_int)
    bb_upper: list[float | None] = [None] * n
    bb_lower: list[float | None] = [None] * n
    for i in range(n):
        if sma_bb[i] is not None and std_bb[i] is not None:
            bb_upper[i] = sma_bb[i] + bb_mult_float * std_bb[i]  # type: ignore[operator]
            bb_lower[i] = sma_bb[i] - bb_mult_float * std_bb[i]  # type: ignore[operator]

    # Keltner Channel
    sma_kc = _rolling_sma(data, kc_window_int)
    atr_kc = _atr(data, kc_window_int)
    kc_upper: list[float | None] = [None] * n
    kc_lower: list[float | None] = [None] * n
    for i in range(n):
        if sma_kc[i] is not None and atr_kc[i] is not None:
            kc_upper[i] = sma_kc[i] + kc_mult_float * atr_kc[i]  # type: ignore[operator]
            kc_lower[i] = sma_kc[i] - kc_mult_float * atr_kc[i]  # type: ignore[operator]

    # Detect squeeze: BB fully inside KC
    squeeze_points: list[int] = []
    for i in range(n):
        if (
            bb_lower[i] is not None
            and bb_upper[i] is not None
            and kc_lower[i] is not None
            and kc_upper[i] is not None
        ):
            if bb_lower[i] > kc_lower[i] and bb_upper[i] < kc_upper[i]:  # type: ignore[operator]
                squeeze_points.append(i)

    squeeze_on = len(squeeze_points) > 0
    direction = _squeeze_direction(data, max_window)

    return SqueezeDetectionResult(
        squeeze_on=squeeze_on,
        squeeze_points=squeeze_points,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        kc_upper=kc_upper,
        kc_lower=kc_lower,
        direction=direction,
    )
