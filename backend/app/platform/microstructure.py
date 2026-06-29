"""P226: Microstructure — VPIN & Order-Flow Imbalance.

Toxic-flow detection from trade series, adapted to bar-level data so it runs
offline on `event_log` bars without a real tick feed.

* **VPIN (Volume-Synchronized Probability of Informed Trading)** — Easley,
  López de Prado, O'Hara (2012). Bucket the trade stream into equal-volume
  bins, split each bucket into buy/sell volume via a bulk-classification rule
  (here: bar close vs bar open — close↑ → buy, close↓ → sell), and VPIN is
  ``Σ|V_buy − V_sell| / Σ V_bucket`` over a rolling window of buckets. High
  VPIN ⇒ toxic order flow ⇒ adverse selection risk ahead.
* **Order-Flow Imbalance (OFI)** — Cont, Kukanov, Stoikov (2014). Per bar,
  ``OFI_t = Δbids·P_bid + (−Δasks)·P_ask``; here we approximate with a
  signed-volume proxy ``ΔV_buy − ΔV_sell`` normalized by total volume, which
  captures the same pressure direction without L2 snapshots.
* **Kyle's λ (price impact)** — regression of per-bar mid-return on signed
  order flow; the slope is the price-impact coefficient ("Kyle's lambda").
  High λ ⇒ thin liquidity / large slippage risk per unit of volume.

Deterministic, pure Python. Reference: Easley et al. VPIN, Cont et al. OFI,
Kyle (1985) price-impact model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "VpinResult",
    "classify_bar_volume",
    "vpin",
    "order_flow_imbalance",
    "kyle_lambda",
]


def classify_bar_volume(
    volumes: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
) -> list[tuple[float, float]]:
    """Bulk-classify each bar's volume into (buy_volume, sell_volume) using
    the close-vs-open direction. A bar with close==open is split 50/50.
    """
    n = len(volumes)
    if n != len(opens) or n != len(closes):
        raise ValueError("volumes, opens, closes must be equal-length")
    out: list[tuple[float, float]] = []
    for v, o, c in zip(volumes, opens, closes):
        if v < 0:
            raise ValueError("volume must be >=0")
        if c > o:
            out.append((v, 0.0))
        elif c < o:
            out.append((0.0, v))
        else:
            out.append((v * 0.5, v * 0.5))
    return out


def _bucketize(
    volumes: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
    bucket_size: float,
) -> list[tuple[float, float]]:
    """Accumulate bar-classified buy/sell volume into fixed-size volume buckets.

    Unlike the naive sequential fill, we fill buy and sell proportionally
    (at the same ratio as the bar's classification) into each bucket to
    preserve the VPIN bulk-classification semantics.
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be > 0")
    if bucket_size < 1e-9:
        # Pathological bucket size would cause an effective infinite loop (each
        # fill step advances by ~bucket_size, so consuming a normal bar volume
        # requires ~volume/bucket_size iterations). Reject up front.
        raise ValueError("bucket_size must be >= 1e-9")
    buckets: list[tuple[float, float]] = []
    cur_buy = 0.0
    cur_sell = 0.0
    for (vb, vs) in classify_bar_volume(volumes, opens, closes):
        # pour this bar's buy & sell into buckets until consumed
        remaining_buy, remaining_sell = vb, vs
        while remaining_buy > 0 or remaining_sell > 0:
            space = bucket_size - (cur_buy + cur_sell)
            if space <= 0:
                buckets.append((cur_buy, cur_sell))
                cur_buy, cur_sell = 0.0, 0.0
                space = bucket_size
            # fill buy and sell proportionally into the remaining space
            total_remaining = remaining_buy + remaining_sell
            if total_remaining <= 0:
                break
            buy_ratio = remaining_buy / total_remaining
            sell_ratio = remaining_sell / total_remaining
            take_buy = min(remaining_buy, space * buy_ratio)
            take_sell = min(remaining_sell, space * sell_ratio)
            # Guard against floating-point underflow: if both takes rounded to
            # zero we cannot make progress, so fall back to the naive sequential
            # fill (matches pre-P359 behaviour) to avoid an infinite loop.
            if take_buy <= 0 and take_sell <= 0:
                take_buy = min(remaining_buy, space)
                take_sell = min(max(space - take_buy, 0.0), remaining_sell)
            cur_buy += take_buy
            remaining_buy -= take_buy
            cur_sell += take_sell
            remaining_sell -= take_sell
    # flush the final partial bucket
    if cur_buy + cur_sell > 0:
        buckets.append((cur_buy, cur_sell))
    return buckets


def vpin(
    volumes: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
    bucket_size: float | None = None,
    window: int = 50,
) -> "VpinResult":
    """Volume-synchronized VPIN.

    ``bucket_size`` defaults to ``total_volume / max(1, len(volumes))`` so the
    series yields roughly one bucket per bar. ``window`` is the rolling number
    of buckets used to compute the fraction of imbalanced volume.
    """
    n = len(volumes)
    if n != len(opens) or n != len(closes):
        raise ValueError("volumes, opens, closes must be equal-length")
    if n == 0:
        raise ValueError("need >=1 bar")
    total_v = sum(volumes)
    if total_v <= 0:
        raise ValueError("total volume must be > 0")
    if bucket_size is None:
        bucket_size = total_v / n
    buckets = _bucketize(volumes, opens, closes, bucket_size)
    if not buckets:
        raise ValueError("no buckets produced")
    # rolling VPIN over `window` buckets
    vpin_series: list[float] = []
    for i in range(len(buckets)):
        lo = max(0, i - window + 1)
        seg = buckets[lo : i + 1]
        if not seg:
            continue
        tot = sum(b + s for b, s in seg)
        imb = sum(abs(b - s) for b, s in seg)
        vpin_series.append(imb / tot if tot > 0 else 0.0)
    return VpinResult(
        buckets=buckets,
        vpin_series=vpin_series,
        latest_vpin=vpin_series[-1] if vpin_series else 0.0,
        bucket_size=bucket_size,
        n_buckets=len(buckets),
    )


@dataclass(frozen=True)
class VpinResult:
    buckets: list[tuple[float, float]]
    vpin_series: list[float]
    latest_vpin: float
    bucket_size: float
    n_buckets: int

    def to_dict(self) -> dict:
        return {
            "n_buckets": self.n_buckets,
            "bucket_size": self.bucket_size,
            "latest_vpin": self.latest_vpin,
            "vpin_series": self.vpin_series,
            "buckets": [{"buy": b, "sell": s} for b, s in self.buckets],
        }


def order_flow_imbalance(
    volumes: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
) -> list[float]:
    """Per-bar OFI proxy: signed volume / total volume, in [−1, 1]."""
    classified = classify_bar_volume(volumes, opens, closes)
    out: list[float] = []
    for (vb, vs), total in zip(classified, volumes):
        if total <= 0:
            out.append(0.0)
            continue
        out.append((vb - vs) / total)
    return out


def kyle_lambda(
    volumes: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
) -> float:
    """Kyle's price-impact coefficient: slope of mid-return on signed volume.

    Returns the OLS slope of ``ret_t = λ · ofi_t`` where ``ret_t = close/open − 1``
    and ``ofi_t`` is the normalized signed-volume imbalance. A larger slope
    means prices move more per unit of order flow ⇒ thinner liquidity.
    """
    n = len(volumes)
    if n != len(opens) or n != len(closes):
        raise ValueError("volumes, opens, closes must be equal-length")
    if n < 2:
        raise ValueError("need >=2 bars")
    ofi = order_flow_imbalance(volumes, opens, closes)
    rets: list[float] = []
    for o, c in zip(opens, closes):
        if o <= 0:
            rets.append(0.0)
        else:
            rets.append(c / o - 1.0)
    mu_x = sum(ofi) / n
    mu_y = sum(rets) / n
    sxx = sum((x - mu_x) ** 2 for x in ofi)
    if sxx == 0.0:
        return 0.0
    sxy = sum((x - mu_x) * (y - mu_y) for x, y in zip(ofi, rets))
    return sxy / sxx