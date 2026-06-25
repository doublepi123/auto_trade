"""P247: Statistical-arbitrage signal generation (distance method).

The *distance method* of Gatev, Goetzmann & Rouwenhorst (2006), paired with an
Ornstein-Uhlenbeck half-life mean-reversion estimate and z-score entry/exit
thresholds in the spirit of Avellaneda & Lee (2008). Given two price series
``y`` and ``x``:

* **distance_method_spread** — normalise both series to a starting value of 1
  (cumulative-return proxy), form the cumulative-spread distance
  ``S = y_norm − x_norm``, and compute the in-sample mean/std used for z-scoring.
* **zscore_signals** — generate LONG / SHORT / FLAT entry & exit signals from
  rolling z-score thresholds: enter short-the-spread when z > entry, enter
  long-the-spread when z < −entry, exit when |z| < exit.
* **half-life** is reused from :mod:`app.platform.cointegration` (P223) to
  characterise expected holding period.

This module is a *signal generator* (it does not place orders); it produces a
:class:`StatArbResult` with the spread, z-score series, signal timeline, and
the OU half-life. Pure Python, no numpy/scipy.

Reference: Gatev-Goetzmann-Rouwenhorst (2006) "Pairs Trading: Performance of a
Relative-Value Arbitrage Rule"; Avellaneda & Lee (2008) "Statistical
Arbitrage in the US Equities Market". Half-life reuse from P223
(Engle-Granger / OU).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.platform.cointegration import half_life_ou, zscore

__all__ = [
    "StatArbResult",
    "distance_method_spread",
    "zscore_signals",
    "stat_arb_signals",
]

Signal = str  # "FLAT" | "LONG_SPREAD" | "SHORT_SPREAD"


def distance_method_spread(
    y: Sequence[float],
    x: Sequence[float],
    *,
    window: int | None = None,
) -> tuple[list[float], float, float]:
    """Distance-method spread and its (mean, std).

    Normalises ``y`` and ``x`` to start at 1.0 (``p / p[0]``), forms the
    spread ``S = y_norm − x_norm``, and returns ``(spread, mean, std)``. The
    mean/std are computed over the whole sample (or the trailing ``window``
    if given) for z-scoring. Raises ``ValueError`` on length mismatch / empty /
    zero starting price.
    """
    n = len(y)
    if n != len(x):
        raise ValueError("y and x must have equal length")
    if n == 0:
        raise ValueError("series must be non-empty")
    if y[0] == 0.0 or x[0] == 0.0:
        raise ValueError("series must start at a non-zero price")
    y_norm = [y[i] / y[0] for i in range(n)]
    x_norm = [x[i] / x[0] for i in range(n)]
    spread = [y_norm[i] - x_norm[i] for i in range(n)]
    if window is None:
        sample = spread
    else:
        if window <= 0:
            raise ValueError("window must be positive")
        sample = spread[-window:] if window < n else spread
    mu = sum(sample) / len(sample)
    var = sum((s - mu) ** 2 for s in sample) / max(len(sample) - 1, 1)
    std = math.sqrt(var) if var > 0.0 else 1e-12
    return spread, mu, std


def zscore_signals(
    spread: Sequence[float],
    mean: float,
    std: float,
    *,
    entry: float = 2.0,
    exit: float = 0.5,
) -> list[Signal]:
    """Map a spread series to LONG/SHORT/FLAT signals via z-score thresholds.

    Convention (Avellaneda-Lee style):

    * z = (spread − mean) / std
    * z >  entry  → ``SHORT_SPREAD`` (spread too wide; short y, long x)
    * z < −entry  → ``LONG_SPREAD``  (spread too tight; long y, short x)
    * |z| < exit  → ``FLAT`` (close out)

    Between exit and entry the previous signal is held (hysteresis). Returns a
    signal per bar. Raises ``ValueError`` if ``entry <= exit`` or ``std <= 0``.
    """
    if entry <= exit:
        raise ValueError("entry threshold must exceed exit threshold")
    if std <= 0.0:
        raise ValueError("std must be positive")
    signals: list[Signal] = []
    current: Signal = "FLAT"
    for s in spread:
        z = (s - mean) / std
        if z > entry:
            current = "SHORT_SPREAD"
        elif z < -entry:
            current = "LONG_SPREAD"
        elif abs(z) < exit:
            current = "FLAT"
        # else: hold previous (hysteresis)
        signals.append(current)
    return signals


@dataclass(frozen=True)
class StatArbResult:
    spread: list[float]
    zscore: list[float]
    signals: list[str]
    mean: float
    std: float
    half_life: float
    n_bars: int

    def to_dict(self) -> dict:
        return {
            "spread": self.spread,
            "zscore": self.zscore,
            "signals": self.signals,
            "mean": self.mean,
            "std": self.std,
            "half_life": self.half_life,
            "half_life_finite": math.isfinite(self.half_life),
            "n_bars": self.n_bars,
        }


def stat_arb_signals(
    y: Sequence[float],
    x: Sequence[float],
    *,
    entry: float = 2.0,
    exit: float = 0.5,
    window: int | None = None,
) -> StatArbResult:
    """Full statistical-arbitrage signal report for a price pair.

    Combines :func:`distance_method_spread`, :func:`zscore_signals` (hysteresis)
    and the OU half-life from :mod:`cointegration`. Raises ``ValueError`` on
    length mismatch / empty / invalid thresholds.
    """
    spread, mean, std = distance_method_spread(y, x, window=window)
    signals = zscore_signals(spread, mean, std, entry=entry, exit=exit)
    z_series = zscore(spread, window=None)
    hl = half_life_ou(spread)
    return StatArbResult(
        spread=spread,
        zscore=z_series,
        signals=signals,
        mean=mean,
        std=std,
        half_life=hl,
        n_bars=len(spread),
    )