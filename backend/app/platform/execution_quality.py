"""P241: Execution Quality Scorecard — fill statistics, slippage, reversion.

Aggregates per-fill TCA into a single execution-quality grade, the way
broker execution-quality (FIX Tag 7600-series) and TCA reports grade fills
against an arrival / VWAP benchmark: fill ratio, participation rate,
signed slippage distribution, and post-fill adverse-selection (reversion).

* **fill_stats** — per-fill and aggregate fill statistics. ``fill_ratio =
  Σqty / Σorder_qty``; ``participation_rate = Σqty / Σorder_qty`` (capped at
  1, the standard broker "fill ratio" when no market volume is supplied);
  ``vwap_fill_price = Σ(qty·price)/Σqty``; per-fill signed slippage
  ``s_bps = (fill − bench)/bench · 1e4`` for BUY and
  ``(bench − fill)/bench · 1e4`` for SELL (positive ⇒ we paid more / received
  less than benchmark ⇒ unfavorable). Mean / median / p95 of the per-fill
  slippage series.
* **price_reversion** — did price revert *against* us after the fill
  (adverse selection)? ``reversion = mean(post[:window]) − fill`` for BUY
  (positive ⇒ we bought high and the market rolled back ⇒ adverse) and
  ``fill − mean(post[:window])`` for SELL (positive ⇒ we sold low and the
  market bounced ⇒ adverse). ``reversion_bps = reversion / fill · 1e4``;
  ``is_adverse`` fires when the sign is unfavorable and ``|reversion_bps|``
  exceeds ``adverse_threshold_bps`` (default 5 bps).
* **execution_scorecard** — top-level grade aggregating FillStats + mean
  slippage + adverse-selection rate + an overall letter grade A/B/C/D.

Grading rubric (explicit, deterministic, thresholds in bps of mean absolute
signed slippage against benchmark):

    A : mean_abs_slippage_bps < 2   AND fill_ratio >= 0.99
    B : mean_abs_slippage_bps < 5   AND fill_ratio >= 0.95
    C : mean_abs_slippage_bps < 10
    D : otherwise

Adverse-selection rate > 50% caps the grade at C (information leakage is a
structural cost, not a one-off).

Reference: Kissell (2013) "The Science of Algorithmic Trading", Almgren
execution benchmarks, FIX Protocol execution-quality tags. Pure Python,
no scipy.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

__all__ = [
    "FillStats",
    "ReversionResult",
    "ExecutionScorecard",
    "fill_stats",
    "price_reversion",
    "execution_scorecard",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile on an already-sorted list (p in [0,100])."""
    if not sorted_vals:
        raise ValueError("cannot take percentile of empty series")
    if not 0.0 <= pct <= 100.0:
        raise ValueError("pct must be in [0, 100]")
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_vals[lo]
    frac = rank - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# --------------------------------------------------------------------------- #
# fill statistics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FillStats:
    n_fills: int
    total_qty: float
    total_order_qty: float
    fill_ratio: float
    participation_rate: float
    avg_fill_price: float
    vwap_fill_price: float
    slippage_bps: list[float]  # per-fill signed slippage
    mean_slippage_bps: float
    median_slippage_bps: float
    p95_slippage_bps: float
    mean_abs_slippage_bps: float

    def to_dict(self) -> dict:
        return {
            "n_fills": self.n_fills,
            "total_qty": self.total_qty,
            "total_order_qty": self.total_order_qty,
            "fill_ratio": self.fill_ratio,
            "participation_rate": self.participation_rate,
            "avg_fill_price": self.avg_fill_price,
            "vwap_fill_price": self.vwap_fill_price,
            "slippage_bps": list(self.slippage_bps),
            "mean_slippage_bps": self.mean_slippage_bps,
            "median_slippage_bps": self.median_slippage_bps,
            "p95_slippage_bps": self.p95_slippage_bps,
            "mean_abs_slippage_bps": self.mean_abs_slippage_bps,
        }


_REQUIRED_FILL_KEYS = ("qty", "price", "side", "order_qty", "benchmark_price")


def fill_stats(fills: Sequence[dict]) -> FillStats:
    """Aggregate per-fill statistics.

    Each fill dict must contain: ``qty``, ``price``, ``side`` ('BUY'/'SELL'),
    ``order_qty`` (intended parent quantity), ``benchmark_price`` (arrival
    or VWAP reference). Computes:

    * ``fill_ratio = Σqty / Σorder_qty``
    * ``participation_rate = Σqty / Σorder_qty`` (capped at 1.0; absent an
      explicit market-volume input we use the intended order quantity as
      the denominator — i.e. fraction of the parent order filled, which is
      the standard "fill ratio" reported by brokers).
    * ``vwap_fill_price = Σ(qty·price) / Σqty``
    * per-fill ``slippage_bps = (fill − bench)/bench · 1e4`` for BUY and
      ``(bench − fill)/bench · 1e4`` for SELL (positive = unfavorable).

    Raises ``ValueError`` on empty fills or any missing/invalid key.
    """
    if not fills:
        raise ValueError("fills must be non-empty")
    for i, f in enumerate(fills):
        for k in _REQUIRED_FILL_KEYS:
            if k not in f:
                raise ValueError(f"fill[{i}] missing required key '{k}'")
        if f["qty"] is None or f["qty"] < 0:
            raise ValueError(f"fill[{i}].qty must be non-negative")
        if f["price"] is None or not math.isfinite(f["price"]) or f["price"] <= 0:
            raise ValueError(f"fill[{i}].price must be a positive finite number")
        if f["order_qty"] is None or f["order_qty"] <= 0:
            raise ValueError(f"fill[{i}].order_qty must be positive")
        bp = f["benchmark_price"]
        if bp is None or not math.isfinite(bp) or bp <= 0:
            raise ValueError(f"fill[{i}].benchmark_price must be a positive finite number")
        side = f["side"]
        if side not in ("BUY", "SELL"):
            raise ValueError(f"fill[{i}].side must be 'BUY' or 'SELL'")

    total_qty = sum(float(f["qty"]) for f in fills)
    total_order_qty = sum(float(f["order_qty"]) for f in fills)
    if total_qty <= 0:
        raise ValueError("total fill qty must be positive")
    if total_order_qty <= 0:
        raise ValueError("total order qty must be positive")

    fill_ratio = total_qty / total_order_qty
    participation_rate = min(1.0, fill_ratio)

    vwap = sum(float(f["qty"]) * float(f["price"]) for f in fills) / total_qty
    avg = sum(float(f["price"]) for f in fills) / len(fills)

    slippages: list[float] = []
    for f in fills:
        price = float(f["price"])
        bench = float(f["benchmark_price"])
        if f["side"] == "BUY":
            slip = (price - bench) / bench * 1e4  # positive = paid more than benchmark
        else:
            slip = (bench - price) / bench * 1e4  # positive = received less than benchmark
        slippages.append(slip)

    mean_slip = statistics.fmean(slippages)
    median_slip = statistics.median(slippages)
    sorted_slip = sorted(slippages)
    p95 = _percentile(sorted_slip, 95.0)
    mean_abs = statistics.fmean(abs(s) for s in slippages)

    return FillStats(
        n_fills=len(fills),
        total_qty=total_qty,
        total_order_qty=total_order_qty,
        fill_ratio=fill_ratio,
        participation_rate=participation_rate,
        avg_fill_price=avg,
        vwap_fill_price=vwap,
        slippage_bps=slippages,
        mean_slippage_bps=mean_slip,
        median_slippage_bps=median_slip,
        p95_slippage_bps=p95,
        mean_abs_slippage_bps=mean_abs,
    )


# --------------------------------------------------------------------------- #
# price reversion / adverse selection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReversionResult:
    side: str
    fill_price: float
    benchmark_price: float
    window: int
    post_fill_mean: float
    reversion: float  # signed, in price units (positive = adverse)
    reversion_bps: float
    reversion_sign: int  # +1 / 0 / -1
    is_adverse: bool
    adverse_threshold_bps: float

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "fill_price": self.fill_price,
            "benchmark_price": self.benchmark_price,
            "window": self.window,
            "post_fill_mean": self.post_fill_mean,
            "reversion": self.reversion,
            "reversion_bps": self.reversion_bps,
            "reversion_sign": self.reversion_sign,
            "is_adverse": self.is_adverse,
            "adverse_threshold_bps": self.adverse_threshold_bps,
        }


def price_reversion(
    benchmark_price: float,
    fill_price: float,
    post_fill_prices: Sequence[float],
    window: int = 5,
    side: str = "BUY",
    adverse_threshold_bps: float = 5.0,
) -> ReversionResult:
    """Adverse-selection / price-reversion check after a fill.

    ``reversion = mean(post_fill_prices[:window]) − fill_price`` for BUY
    (positive ⇒ we bought high, market rolled back ⇒ adverse), and
    ``fill_price − mean(post_fill_prices[:window])`` for SELL (positive ⇒
    we sold low, market bounced ⇒ adverse). ``reversion_bps =
    reversion / fill_price · 1e4``. ``is_adverse`` is true when ``reversion``
    is positive (unfavorable under the side convention above) AND
    ``|reversion_bps| > adverse_threshold_bps``.

    Raises ``ValueError`` on empty post-fill prices, non-positive prices,
    a non-positive ``window``, or an unknown ``side``.
    """
    if not math.isfinite(benchmark_price) or benchmark_price <= 0:
        raise ValueError("benchmark_price must be a positive finite number")
    if not math.isfinite(fill_price) or fill_price <= 0:
        raise ValueError("fill_price must be a positive finite number")
    if not post_fill_prices:
        raise ValueError("post_fill_prices must be non-empty")
    if window <= 0:
        raise ValueError("window must be positive")
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be 'BUY' or 'SELL'")
    if adverse_threshold_bps < 0:
        raise ValueError("adverse_threshold_bps must be non-negative")

    for i, p in enumerate(post_fill_prices):
        if p is None or not math.isfinite(p) or p <= 0:
            raise ValueError(f"post_fill_prices[{i}] must be a positive finite number")

    w = min(int(window), len(post_fill_prices))
    slice_ = post_fill_prices[:w]
    post_mean = statistics.fmean(slice_)

    if side == "BUY":
        reversion = post_mean - fill_price  # +: market reverted down after we bought
    else:
        reversion = fill_price - post_mean  # +: market reverted up after we sold

    reversion_bps = reversion / fill_price * 1e4
    sign = _sign(reversion)
    is_adverse = (reversion > 0) and (abs(reversion_bps) > adverse_threshold_bps)

    return ReversionResult(
        side=side,
        fill_price=fill_price,
        benchmark_price=benchmark_price,
        window=w,
        post_fill_mean=post_mean,
        reversion=reversion,
        reversion_bps=reversion_bps,
        reversion_sign=sign,
        is_adverse=is_adverse,
        adverse_threshold_bps=adverse_threshold_bps,
    )


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecutionScorecard:
    fill_stats: FillStats
    mean_slippage_bps: float
    mean_abs_slippage_bps: float
    adverse_selection_rate: float  # fraction of fills flagged adverse
    n_adverse: int
    n_reversion_checked: int
    grade: str  # A / B / C / D
    reversion_results: list[ReversionResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fill_stats": self.fill_stats.to_dict(),
            "mean_slippage_bps": self.mean_slippage_bps,
            "mean_abs_slippage_bps": self.mean_abs_slippage_bps,
            "adverse_selection_rate": self.adverse_selection_rate,
            "n_adverse": self.n_adverse,
            "n_reversion_checked": self.n_reversion_checked,
            "grade": self.grade,
            "reversion_results": [r.to_dict() for r in self.reversion_results],
        }


def _grade(mean_abs_bps: float, fill_ratio: float, adverse_rate: float) -> str:
    """Letter grade from the rubric in the module docstring."""
    if mean_abs_bps < 2.0 and fill_ratio >= 0.99:
        base = "A"
    elif mean_abs_bps < 5.0 and fill_ratio >= 0.95:
        base = "B"
    elif mean_abs_bps < 10.0:
        base = "C"
    else:
        base = "D"
    # Adverse-selection rate > 50% is structural leakage → cap at C.
    if adverse_rate > 0.5 and base in ("A", "B"):
        return "C"
    return base


def execution_scorecard(
    fills: Sequence[dict],
    benchmark_prices: Sequence[float] | None = None,
    post_fill_prices: Sequence[Sequence[float]] | None = None,
    window: int = 5,
    adverse_threshold_bps: float = 5.0,
) -> ExecutionScorecard:
    """Top-level execution-quality grade.

    Builds :func:`fill_stats` from ``fills``. If ``benchmark_prices`` is
    provided (one per fill, same length), it overrides each fill's
    ``benchmark_price`` (useful when the caller computes a single benchmark
    series separately). If ``post_fill_prices`` is provided (one sequence
    per fill), :func:`price_reversion` is run per fill to compute the
    adverse-selection rate (fraction of fills flagged adverse); otherwise
    the rate is 0 and the grade is driven only by slippage + fill ratio.

    Letter grade rubric (documented in the module docstring):

        A : mean_abs_slippage_bps < 2  AND fill_ratio >= 0.99
        B : mean_abs_slippage_bps < 5  AND fill_ratio >= 0.95
        C : mean_abs_slippage_bps < 10
        D : otherwise

    Adverse-selection rate > 50% caps the grade at C.
    """
    if not fills:
        raise ValueError("fills must be non-empty")
    if benchmark_prices is not None and len(benchmark_prices) != len(fills):
        raise ValueError("benchmark_prices length must match fills length")
    if post_fill_prices is not None and len(post_fill_prices) != len(fills):
        raise ValueError("post_fill_prices length must match fills length")

    work: list[dict] = []
    for i, f in enumerate(fills):
        d = dict(f)
        if benchmark_prices is not None:
            bp = benchmark_prices[i]
            if bp is None or not math.isfinite(bp) or bp <= 0:
                raise ValueError(f"benchmark_prices[{i}] must be a positive finite number")
            d["benchmark_price"] = bp
        work.append(d)

    stats = fill_stats(work)

    reversion_results: list[ReversionResult] = []
    if post_fill_prices is not None:
        for i, f in enumerate(work):
            post = post_fill_prices[i]
            if not post:
                continue
            rv = price_reversion(
                benchmark_price=float(f["benchmark_price"]),
                fill_price=float(f["price"]),
                post_fill_prices=post,
                window=window,
                side=f["side"],
                adverse_threshold_bps=adverse_threshold_bps,
            )
            reversion_results.append(rv)

    n_checked = len(reversion_results)
    n_adverse = sum(1 for r in reversion_results if r.is_adverse)
    adverse_rate = (n_adverse / n_checked) if n_checked > 0 else 0.0

    grade = _grade(stats.mean_abs_slippage_bps, stats.fill_ratio, adverse_rate)

    return ExecutionScorecard(
        fill_stats=stats,
        mean_slippage_bps=stats.mean_slippage_bps,
        mean_abs_slippage_bps=stats.mean_abs_slippage_bps,
        adverse_selection_rate=adverse_rate,
        n_adverse=n_adverse,
        n_reversion_checked=n_checked,
        grade=grade,
        reversion_results=reversion_results,
    )