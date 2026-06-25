"""P236: Drawdown Forecast & Recovery-Time distribution.

The descriptive companion to :mod:`app.platform.drawdown_analysis` (which
answers "what did happen"); this module is *predictive* — it answers "what
will happen" given return statistics:

* **underwater_periods** — parse the equity curve into consecutive underwater
  runs (peak-to-recovery), each tagged with peak/trough/recovery indices,
  depth (peak-to-trough pct), duration in bars and recovery bars. Still-open
  drawdowns report ``recovery_idx = None`` and ``recovery_bars = None``.
* **recovery_time_distribution** — over the *completed* recoveries only, the
  empirical distribution of recovery times: mean / median / max, plus the
  survival probability ``P(recovery > k bars)`` for a small set of buckets.
  Raises ``ValueError`` if there are no completed recoveries.
* **expected_drawdown** — model the maximum drawdown over a ``horizon_bars``
  window from the return series' volatility. Two closed-form views are
  provided:

    - **Expected max drawdown** via the reflection-principle surrogate for the
      running maximum of a zero-mean random walk:
      ``E[M_h] = σ · sqrt(2h/π)`` (the expected absolute maximum of a
      Brownian motion of length ``h`` — Magdon-Ismail et al. 2004 show this is
      the leading-order term of the true max-DD distribution).
    - **Percentile drawdown** (VaR-style): ``DD_p ≈ σ · sqrt(h) · Φ^{-1}(p)``
      using the Acklam rational approximation of the standard-normal inverse
      CDF (same algorithm :mod:`app.platform.risk_metrics` uses, ported here so
      this module stays self-contained).

* **drawdown_forecast_report** — aggregate the above for a single dict.

Reference: Burke (1994) "A sharper Sharpe ratio"; Johansen & Sornette (2001)
"Large Stock Market Price Drawdowns"; Magdon-Ismail, Atiya, Pratap & Abu-Mostafa
(2004) "On the Maximum Drawdown of a Brownian Motion". Pure Python, no scipy,
no numpy. Deterministic, closed-form / moment-based only.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "DrawdownPeriod",
    "RecoveryStats",
    "ExpectedDrawdownResult",
    "DrawdownForecastResult",
    "underwater_periods",
    "recovery_time_distribution",
    "expected_drawdown",
    "drawdown_forecast_report",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normal_quantile(p: float) -> float:
    """Acklam's rational approximation of the standard-normal inverse CDF.

    Same algorithm used in :mod:`app.platform.risk_metrics`; ported here so
    this module is self-contained. Accurate to ~1e-9 across the central range.
    """
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0

    a = [
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def _std(returns: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1); 0.0 for <2 observations or no variance."""
    n = len(returns)
    if n < 2:
        return 0.0
    m = sum(returns) / n
    ss = sum((x - m) ** 2 for x in returns)
    if ss <= 0.0:
        return 0.0
    return math.sqrt(ss / (n - 1))


# ---------------------------------------------------------------------------
# DrawdownPeriod & underwater parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawdownPeriod:
    """A single underwater run: peak -> trough -> recovery.

    ``depth`` is the peak-to-trough percentage drop (positive number, e.g.
    0.10 for a 10% drawdown). ``recovery_idx`` / ``recovery_bars`` are ``None``
    when the equity is still underwater at the end of the series.
    """

    peak_idx: int
    trough_idx: int
    recovery_idx: int | None
    depth: float
    duration_bars: int
    recovery_bars: int | None

    def to_dict(self) -> dict:
        return {
            "peak_idx": self.peak_idx,
            "trough_idx": self.trough_idx,
            "recovery_idx": self.recovery_idx,
            "depth": self.depth,
            "duration_bars": self.duration_bars,
            "recovery_bars": self.recovery_bars,
        }


def underwater_periods(equity: Sequence[float]) -> list[DrawdownPeriod]:
    """Parse ``equity`` into consecutive underwater runs (peak-to-recovery).

    A run begins at a running peak and ends when the equity returns to or
    exceeds that peak (``recovery_idx`` set), or at the last sample if it never
    recovers (``recovery_idx = None``). Depth is the peak-to-trough percentage
    drop. ``duration_bars`` counts bars from peak to trough inclusive;
    ``recovery_bars`` counts bars from trough to recovery inclusive (or
    ``None`` if still underwater).

    Formula: for peak value ``P`` and trough value ``T`` the depth is
    ``depth = (P - T) / P`` (with ``P > 0``). When ``P <= 0`` the depth is
    reported as ``0.0`` to avoid division-by-zero on degenerate equity curves.
    """
    n = len(equity)
    if n < 2:
        return []

    periods: list[DrawdownPeriod] = []
    peak_idx = 0
    peak_val = equity[0]
    trough_idx = 0
    trough_val = equity[0]
    in_dd = False

    for i in range(1, n):
        x = equity[i]
        if not in_dd:
            if x < peak_val:
                in_dd = True
                trough_idx = i
                trough_val = x
            else:
                peak_idx = i
                peak_val = x
        else:
            if x < trough_val:
                trough_idx = i
                trough_val = x
            if x >= peak_val:
                depth = (peak_val - trough_val) / peak_val if peak_val > 0 else 0.0
                duration = trough_idx - peak_idx
                recovery_bars = i - trough_idx
                periods.append(
                    DrawdownPeriod(
                        peak_idx=peak_idx,
                        trough_idx=trough_idx,
                        recovery_idx=i,
                        depth=depth,
                        duration_bars=duration,
                        recovery_bars=recovery_bars,
                    )
                )
                in_dd = False
                peak_idx = i
                peak_val = x

    if in_dd:
        depth = (peak_val - trough_val) / peak_val if peak_val > 0 else 0.0
        duration = trough_idx - peak_idx
        periods.append(
            DrawdownPeriod(
                peak_idx=peak_idx,
                trough_idx=trough_idx,
                recovery_idx=None,
                depth=depth,
                duration_bars=duration,
                recovery_bars=None,
            )
        )

    return periods


# ---------------------------------------------------------------------------
# Recovery-time distribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryStats:
    """Empirical distribution of recovery times over completed drawdowns.

    ``survival`` is the empirical survival function ``P(R > k)`` evaluated at
    a few representative buckets (1, 5, 10, 20, 50 bars by default, clamped to
    the observed range). ``n_completed`` is the count of drawdowns that
    actually recovered (``recovery_bars is not None``).
    """

    n_completed: int
    mean: float
    median: float
    max: float
    survival: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "n_completed": self.n_completed,
            "mean": self.mean,
            "median": self.median,
            "max": self.max,
            "survival": self.survival,
        }


def recovery_time_distribution(
    periods: Sequence[DrawdownPeriod],
    buckets: Sequence[int] = (1, 5, 10, 20, 50),
) -> RecoveryStats:
    """Empirical recovery-time distribution over *completed* drawdowns.

    Mean / median / max of the per-period ``recovery_bars`` (trough ->
    recovery). Survival ``P(R > k) = #{R > k} / N`` at each bucket ``k``.
    Raises ``ValueError`` if no period completed recovery (nothing to
    summarise).
    """
    rec: list[int] = [p.recovery_bars for p in periods if p.recovery_bars is not None]
    if not rec:
        raise ValueError("no completed recoveries in periods")
    n = len(rec)
    mean = sum(rec) / n
    median = float(statistics.median(rec))
    mx = float(max(rec))
    survival: dict[str, float] = {}
    for k in buckets:
        if k < 0:
            continue
        gt = sum(1 for r in rec if r > k)
        survival[str(k)] = gt / n
    return RecoveryStats(
        n_completed=n,
        mean=mean,
        median=median,
        max=mx,
        survival=survival,
    )


# ---------------------------------------------------------------------------
# Expected drawdown from return statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedDrawdownResult:
    """Forecast drawdown over a ``horizon_bars`` window from return volatility.

    Two closed-form views:

    - ``expected_max`` -- ``E[M_h] = sigma * sqrt(2h/pi)`` (reflection-principle
      expected absolute maximum of a zero-mean Brownian motion of length
      ``h``; the leading-order term of Magdon-Ismail et al. 2004).
    - ``percentile`` -- ``DD_p = sigma * sqrt(h) * Phi^{-1}(p)`` (VaR-style
      percentile of the max drawdown, using the Acklam inverse normal).
    """

    horizon_bars: int
    confidence: float
    sigma: float
    expected_max: float
    percentile: float

    def to_dict(self) -> dict:
        return {
            "horizon_bars": self.horizon_bars,
            "confidence": self.confidence,
            "sigma": self.sigma,
            "expected_max": self.expected_max,
            "percentile": self.percentile,
        }


def expected_drawdown(
    returns: Sequence[float],
    horizon_bars: int,
    confidence: float = 0.95,
) -> ExpectedDrawdownResult:
    """Forecast the max drawdown over ``horizon_bars`` from return volatility.

    The maximum drawdown of a zero-mean random walk of length ``h`` scales like
    ``sigma * sqrt(h)``. Two closed-form scalars are returned:

    - ``expected_max = sigma * sqrt(2h/pi)`` -- the reflection-principle
      expectation of ``max_{k<=h} |S_k|`` (Magdon-Ismail et al. 2004).
    - ``percentile = sigma * sqrt(h) * Phi^{-1}(p)`` -- VaR-style percentile of
      the drawdown at confidence ``p``, ``Phi^{-1}`` via Acklam.

    Raises ``ValueError`` if ``returns`` is empty, ``horizon_bars < 1``, or
    ``confidence`` is outside ``(0, 1)``.
    """
    if not returns:
        raise ValueError("returns must be non-empty")
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be >= 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    sigma = _std(returns)
    h = float(horizon_bars)
    expected_max = sigma * math.sqrt(2.0 * h / math.pi)
    z = _normal_quantile(confidence)  # positive for confidence > 0.5
    percentile = sigma * math.sqrt(h) * z
    return ExpectedDrawdownResult(
        horizon_bars=horizon_bars,
        confidence=confidence,
        sigma=sigma,
        expected_max=expected_max,
        percentile=percentile,
    )


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawdownForecastResult:
    """Aggregate drawdown forecast: descriptive periods + recovery dist + forecast."""

    n_periods: int
    n_open: int
    max_depth: float
    recovery: RecoveryStats | None
    forecast: ExpectedDrawdownResult

    def to_dict(self) -> dict:
        return {
            "n_periods": self.n_periods,
            "n_open": self.n_open,
            "max_depth": self.max_depth,
            "recovery": self.recovery.to_dict() if self.recovery is not None else None,
            "forecast": self.forecast.to_dict(),
        }


def drawdown_forecast_report(
    equity_or_returns: Sequence[float],
    *,
    horizon_bars: int,
    confidence: float = 0.95,
    input_mode: str = "equity",
) -> DrawdownForecastResult:
    """Aggregate the drawdown forecast.

    ``input_mode``:

    - ``"equity"`` (default): ``equity_or_returns`` is an equity curve; the
      underwater periods + recovery distribution are computed from it, and
      the returns are derived as ``r_t = E_t / E_{t-1} - 1`` for the
      ``expected_drawdown`` forecast.
    - ``"returns"``: ``equity_or_returns`` is a return series; only the
      ``expected_drawdown`` forecast is computed and ``recovery`` is ``None``
      (no equity curve to parse).

    Raises ``ValueError`` on invalid/insufficient inputs (delegated to the
    underlying functions for the forecast; ``equity`` mode requires ``>= 2``
    samples to derive returns).
    """
    if input_mode not in ("equity", "returns"):
        raise ValueError("input_mode must be 'equity' or 'returns'")

    if input_mode == "returns":
        forecast = expected_drawdown(equity_or_returns, horizon_bars, confidence)
        return DrawdownForecastResult(
            n_periods=0,
            n_open=0,
            max_depth=0.0,
            recovery=None,
            forecast=forecast,
        )

    n = len(equity_or_returns)
    if n < 2:
        raise ValueError("equity curve must have >= 2 points")

    periods = underwater_periods(equity_or_returns)
    n_open = sum(1 for p in periods if p.recovery_idx is None)
    max_depth = max((p.depth for p in periods), default=0.0)

    eq = list(equity_or_returns)
    rets: list[float] = []
    for i in range(1, n):
        prev = eq[i - 1]
        if prev != 0.0:
            rets.append(eq[i] / prev - 1.0)
        else:
            rets.append(0.0)

    recovery: RecoveryStats | None
    try:
        recovery = recovery_time_distribution(periods)
    except ValueError:
        recovery = None

    forecast = expected_drawdown(rets, horizon_bars, confidence)
    return DrawdownForecastResult(
        n_periods=len(periods),
        n_open=n_open,
        max_depth=max_depth,
        recovery=recovery,
        forecast=forecast,
    )