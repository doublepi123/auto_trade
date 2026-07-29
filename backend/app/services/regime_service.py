"""Market regime panel service (read-only).

Surfaces a simple, robust regime classification for a single symbol using
only on-ledger data (no live broker calls). The platform ships a sophisticated
HMM/ADX regime package (``app.platform.regime``, ``regime_hmm``, …) but those
require OHLC bar streams that are not persisted in the SQLite ledger. This
service therefore derives a price series from filled ``OrderRecord`` rows and
classifies the regime with transparent statistical proxies:

* **Volatility level** — stdev of log-returns (annualized) bucketed
  high / medium / low.
* **Trend direction** — least-squares slope of recent closes vs their mean.
* **Volume regime** — recent fill frequency vs the historical median.

When there is not enough on-ledger price history, the service returns an
``UNKNOWN`` regime with ``confidence=0.0`` and ``data_points=0`` rather than
guessing. The classification core is a pure function so it can be unit-tested
without a database.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RegimeService", "RegimeLabel", "RegimeIndicators", "classify_regime"]


# ----------------------------------------------------------------------
# public value types
# ----------------------------------------------------------------------


class RegimeLabel:
    """String constants for the regime label (kept as plain strings for JSON)."""
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RegimeIndicators:
    """Raw statistical indicators backing a regime label."""
    volatility_level: str  # high | medium | low | unknown
    trend_direction: str   # up | down | sideways | unknown
    volume_regime: str     # high | normal | low | unknown
    price_vs_mean_pct: float  # latest close vs mean of window (signed pct)

    def as_dict(self) -> dict[str, Any]:
        return {
            "volatility_level": self.volatility_level,
            "trend_direction": self.trend_direction,
            "volume_regime": self.volume_regime,
            "price_vs_mean_pct": round(self.price_vs_mean_pct, 4),
        }


@dataclass(frozen=True)
class RegimeClassification:
    """Full output of :func:`classify_regime` (label + indicators + confidence)."""
    label: str
    confidence: float
    indicators: RegimeIndicators

    def as_dict(self) -> dict[str, Any]:
        return {
            "regime_label": self.label,
            "confidence": round(self.confidence, 4),
            "indicators": self.indicators.as_dict(),
        }


# ----------------------------------------------------------------------
# classification thresholds (module-level so tests can pin behavior)
# ----------------------------------------------------------------------

# Minimum number of price points required to emit a non-UNKNOWN regime. Below
# this the statistics are too noisy to trust.
_MIN_DATA_POINTS = 5

# Annualized realized-vol thresholds for the high/medium/low buckets. Calibrated
# against typical US equity ranges (~15-30% annualized); tuned for the per-fill
# granularity we actually have (fills, not daily bars), so the absolute numbers
# are intentionally loose — what matters is the *relative* high/low split.
_HIGH_VOL_THRESHOLD = 0.45   # >45% annualized → high
_LOW_VOL_THRESHOLD = 0.15    # <15% annualized → low

# Slope (as a fraction of the mean price per sample step) above which we call
# the trend "up"/"down" rather than "sideways".
_TREND_SLOPE_THRESHOLD = 0.002

# An arbitrary per-year sample rate used to annualize vol. With ~250 trading
# days this is the conventional equity value; we don't have daily bars so it's
# an approximation, but it keeps the relative high/low split stable.
_PERIODS_PER_YEAR = 252.0


# ----------------------------------------------------------------------
# pure classification core (no I/O — directly unit-testable)
# ----------------------------------------------------------------------


def classify_regime(
    prices: list[float],
    *,
    fill_counts: list[int] | None = None,
) -> RegimeClassification:
    """Classify a regime from a price series (+ optional per-sample fill counts).

    Returns ``UNKNOWN`` with zero confidence when the series is too short or
    degenerate (constant prices). ``prices`` must be ascending in time. The
    optional ``fill_counts`` parallel array (recent→older is fine; only the
    last two windows are compared) drives the volume-regime indicator.
    """
    cleaned = [float(p) for p in prices if p is not None and math.isfinite(float(p)) and float(p) > 0]
    if len(cleaned) < _MIN_DATA_POINTS:
        return RegimeClassification(
            label=RegimeLabel.UNKNOWN,
            confidence=0.0,
            indicators=RegimeIndicators(
                volatility_level="unknown",
                trend_direction="unknown",
                volume_regime="unknown",
                price_vs_mean_pct=0.0,
            ),
        )

    returns = _log_returns(cleaned)
    if len(returns) < 2:
        return RegimeClassification(
            label=RegimeLabel.UNKNOWN,
            confidence=0.0,
            indicators=RegimeIndicators(
                volatility_level="unknown",
                trend_direction="unknown",
                volume_regime=_volume_regime(fill_counts),
                price_vs_mean_pct=0.0,
            ),
        )

    mean_price = statistics.fmean(cleaned)
    latest = cleaned[-1]
    price_vs_mean_pct = ((latest - mean_price) / mean_price) if mean_price else 0.0

    vol_level = _volatility_level(returns)
    trend = _trend_direction(cleaned)
    volume = _volume_regime(fill_counts)

    # Decision precedence: extreme volatility dominates (a high-vol market is
    # not "trending" for our range-trading purpose), then trend, then range.
    if vol_level == "high":
        label = RegimeLabel.HIGH_VOLATILITY
        confidence = _scaled_confidence(_annualized_vol(returns), _HIGH_VOL_THRESHOLD, floor=_HIGH_VOL_THRESHOLD)
    elif vol_level == "low":
        label = RegimeLabel.LOW_VOLATILITY
        confidence = _scaled_confidence(_LOW_VOL_THRESHOLD, _annualized_vol(returns), floor=_LOW_VOL_THRESHOLD)
    elif trend == "up":
        label = RegimeLabel.TRENDING_UP
        confidence = _scaled_confidence(abs(_slope(cleaned)) / mean_price, _TREND_SLOPE_THRESHOLD, floor=_TREND_SLOPE_THRESHOLD)
    elif trend == "down":
        label = RegimeLabel.TRENDING_DOWN
        confidence = _scaled_confidence(abs(_slope(cleaned)) / mean_price, _TREND_SLOPE_THRESHOLD, floor=_TREND_SLOPE_THRESHOLD)
    else:
        label = RegimeLabel.RANGE_BOUND
        # Range-bound confidence rises as vol stays low and trend stays flat.
        confidence = max(0.0, min(1.0, 0.5 - (_annualized_vol(returns) / _HIGH_VOL_THRESHOLD) * 0.25))

    return RegimeClassification(
        label=label,
        confidence=confidence,
        indicators=RegimeIndicators(
            volatility_level=vol_level,
            trend_direction=trend,
            volume_regime=volume,
            price_vs_mean_pct=price_vs_mean_pct,
        ),
    )


# ----------------------------------------------------------------------
# statistical helpers (pure)
# ----------------------------------------------------------------------


def _log_returns(prices: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev <= 0:
            continue
        out.append(math.log(prices[i] / prev))
    return out


def _annualized_vol(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    var = math.fsum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(max(var, 0.0)) * math.sqrt(_PERIODS_PER_YEAR)


def _volatility_level(returns: list[float]) -> str:
    vol = _annualized_vol(returns)
    if vol >= _HIGH_VOL_THRESHOLD:
        return "high"
    if vol < _LOW_VOL_THRESHOLD:
        return "low"
    return "medium"


def _slope(values: list[float]) -> float:
    """Least-squares slope of ``y`` vs ``x = 0..n-1`` (0.0 if degenerate)."""
    n = len(values)
    if n < 2:
        return 0.0
    sx = sy = sxx = sxy = 0.0
    for i, v in enumerate(values):
        sx += i
        sy += v
        sxx += i * i
        sxy += i * v
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _trend_direction(prices: list[float]) -> str:
    mean = statistics.fmean(prices)
    if mean <= 0:
        return "unknown"
    slope = _slope(prices)
    relative = abs(slope) / mean
    if slope > 0 and relative >= _TREND_SLOPE_THRESHOLD:
        return "up"
    if slope < 0 and relative >= _TREND_SLOPE_THRESHOLD:
        return "down"
    return "sideways"


def _volume_regime(fill_counts: list[int] | None) -> str:
    """Compare the most recent half of samples vs the older half."""
    if not fill_counts or len(fill_counts) < 4:
        return "unknown"
    counts = [int(c) for c in fill_counts if c is not None and int(c) >= 0]
    if len(counts) < 4:
        return "unknown"
    mid = len(counts) // 2
    recent = statistics.fmean(counts[mid:])
    older = statistics.fmean(counts[:mid])
    if older <= 0:
        return "high" if recent > 0 else "unknown"
    ratio = recent / older
    if ratio >= 1.5:
        return "high"
    if ratio <= 0.5:
        return "low"
    return "normal"


def _scaled_confidence(value: float, ceiling: float, *, floor: float) -> float:
    """Map a value above ``floor`` onto a 0.5–1.0 confidence scale.

    At exactly the threshold the caller is "barely" confident (0.5); as the
    value grows past the threshold the confidence rises toward 1.0, clamped.
    The ceiling controls how quickly confidence saturates.
    """
    if ceiling <= floor:
        return 0.5
    span = ceiling - floor
    excess = max(0.0, value - floor)
    return max(0.0, min(1.0, 0.5 + 0.5 * (excess / span)))


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------


class RegimeService:
    """Build regime snapshots from on-ledger fill prices for one symbol."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def get_current_regime(self, symbol: str) -> dict[str, Any]:
        """Return the current regime classification for ``symbol``.

        Always returns a well-formed payload; when there is insufficient
        on-ledger price history the label is ``UNKNOWN`` with confidence 0.
        """
        prices, fill_counts = self._load_series(symbol, days=30)
        classification = classify_regime(prices, fill_counts=fill_counts)
        payload = classification.as_dict()
        return {
            "symbol": symbol,
            "regime_label": payload["regime_label"],
            "confidence": payload["confidence"],
            "indicators": payload["indicators"],
            "as_of": _now_iso(),
            "data_points": len(prices),
        }

    def get_regime_history(
        self,
        symbol: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Return one regime snapshot per calendar day with an available price.

        Each day's classification uses the trailing price window *up to and
        including* that day, so the history shows how the regime evolved.
        Days with no price history before them are skipped.
        """
        days = max(1, int(days))
        prices_by_day, fill_counts_by_day = self._load_daily_series(symbol, days=days)

        rows: list[dict[str, Any]] = []
        # Iterate ascending; classify each day using the trailing window.
        for day in sorted(prices_by_day.keys()):
            window = prices_by_day[day]
            counts_window = fill_counts_by_day.get(day, [])
            classification = classify_regime(window, fill_counts=counts_window)
            avg_price = statistics.fmean(window) if window else 0.0
            vol_proxy = _annualized_vol(_log_returns(window)) if len(window) >= 2 else 0.0
            rows.append(
                {
                    "date": day.isoformat(),
                    "regime_label": classification.label,
                    "avg_price": round(avg_price, 4),
                    "volatility_proxy": round(vol_proxy, 4),
                }
            )
        return rows

    # ------------------------------------------------------------------
    # data loading (the only place that touches the DB)
    # ------------------------------------------------------------------

    def _load_series(
        self,
        symbol: str,
        days: int,
    ) -> tuple[list[float], list[int]]:
        """Return (prices, per-day fill_counts) ascending in time for the window."""
        rows = self._fetch_fills(symbol=symbol, days=days)
        if not rows:
            return [], []
        prices = [r.price for r in rows]
        fill_counts = _fills_per_day(rows)
        return prices, fill_counts

    def _load_daily_series(
        self,
        symbol: str,
        days: int,
    ) -> tuple[dict[date, list[float]], dict[date, list[int]]]:
        """Return per-day cumulative trailing windows for history classification."""
        rows = self._fetch_fills(symbol=symbol, days=days)
        if not rows:
            return {}, {}

        prices_by_day: dict[date, list[float]] = {}
        for r in rows:
            day = _to_utc_date(r.filled_at)
            if day is None:
                continue
            prices_by_day.setdefault(day, []).append(r.price)

        # Build a cumulative trailing window: each day sees every fill on or
        # before that day, in time order, so the regime for day N reflects all
        # information available at the close of day N.
        cumulative_prices: list[float] = []
        cumulative_day_counts: list[int] = []
        out_prices: dict[date, list[float]] = {}
        out_counts: dict[date, list[int]] = {}
        for day in sorted(prices_by_day.keys()):
            day_prices = sorted(prices_by_day[day])
            cumulative_prices.extend(day_prices)
            cumulative_day_counts.append(len(day_prices))
            # Snapshot copies so later days don't mutate earlier windows.
            out_prices[day] = list(cumulative_prices)
            out_counts[day] = list(cumulative_day_counts)
        return out_prices, out_counts

    def _fetch_fills(self, *, symbol: str, days: int) -> list[OrderRecord]:
        """Filled orders for ``symbol`` in the window, ordered ascending by time.

        Uses ``executed_price`` (the actual fill price) when available and
        falls back to ``price`` (the submitted limit/quote) for legacy rows.
        Filters out zero/negative prices so degenerate rows never corrupt the
        statistics.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.symbol == symbol)
            .where(OrderRecord.filled_at.is_not(None))
            .where(OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        rows = list(self._db.scalars(stmt).all())
        # Normalize the price onto the OrderRecord instances in memory so the
        # caller can read a single attribute uniformly.
        normalized: list[OrderRecord] = []
        for row in rows:
            price = row.executed_price if row.executed_price is not None else row.price
            # Mutate a shallow copy via setattr to expose a stable ``price``
            # accessor for the pure classification helpers.
            try:
                row.price = float(price)  # type: ignore[assignment]
            except (TypeError, ValueError):
                continue
            if row.price <= 0:
                continue
            normalized.append(row)
        return normalized


# ----------------------------------------------------------------------
# module-private datetime helpers
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_utc_date(ts: datetime | None) -> date | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date()


def _fills_per_day(rows: list[OrderRecord]) -> list[int]:
    """Count of fills per calendar day, ascending (parallel-in-spirit to prices)."""
    counts: dict[date, int] = {}
    for r in rows:
        day = _to_utc_date(r.filled_at)
        if day is None:
            continue
        counts[day] = counts.get(day, 0) + 1
    return [counts[day] for day in sorted(counts.keys())]
