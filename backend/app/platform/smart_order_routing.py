"""P251: Smart Order Routing (SOR) — multi-venue best-execution planner.

A pure-computation layer that, given a parent order and a snapshot of L1
quotes across multiple venues, produces an execution plan that maximises
filled value (best price) subject to venue fees, tick quantisation, and per-
venue depth caps. Mirrors the *abstraction shape* of Nautilus's
``OrderRouting`` / FIX best-execution logic, but performs **no broker I/O**.

Conventions
-----------
* A **venue** quote is ``{"venue", "bid", "bid_size", "ask", "ask_size",
  "fee_per_share", "tick_size"}``. Sizes are in shares; fees are per share.
* A parent order is ``{"side": "buy"|"sell", "quantity": Q}``.
* For a **buy**, route to the lowest ask (cheapest to buy); for a **sell**,
  route to the highest bid (best to sell). Greedy by **effective price**
  (price + fee for buys, price − fee for sells), respecting each venue's
  displayed size and quantising each child to the venue tick.
* Returns :class:`SorResult` with the per-venue child orders, the
  weighted-average fill price, total fees, and any unfilled remainder.

Pure Python, deterministic (no RNG, no broker calls). Raises ``ValueError``
on invalid quotes / non-positive quantity / unknown side.

Reference: Nautilus ``OrderRouting`` / ``ExecutionEngine``; FIX tag 18
"ExecutionReport"; Kissell "The Science of Algorithmic Trading and Risk
Management" (best-execution routing). Pure Python, no external deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "SorResult",
    "VenueQuote",
    "route_order",
]

Side = str  # "buy" | "sell"


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    bid: float
    bid_size: int
    ask: float
    ask_size: int
    fee_per_share: float = 0.0
    tick_size: float = 0.01

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "bid": self.bid,
            "bid_size": self.bid_size,
            "ask": self.ask,
            "ask_size": self.ask_size,
            "fee_per_share": self.fee_per_share,
            "tick_size": self.tick_size,
        }


def _quantize_down(qty: int, tick: float, price: float) -> tuple[int, float]:
    """Quantise a child's notional/size to the venue tick; return (size, price)."""
    if tick <= 0.0:
        return qty, price
    # Round price down to the nearest tick for buys (we pay), up for sells handled by caller.
    n_ticks = math.floor(price / tick + 1e-9)
    px = round(n_ticks * tick, 10)
    return qty, px


@dataclass(frozen=True)
class SorResult:
    side: str
    quantity: int
    child_orders: list[dict]
    weighted_avg_price: float
    total_fees: float
    filled_quantity: int
    unfilled_quantity: int
    n_venues: int

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "quantity": self.quantity,
            "child_orders": self.child_orders,
            "weighted_avg_price": self.weighted_avg_price,
            "total_fees": self.total_fees,
            "filled_quantity": self.filled_quantity,
            "unfilled_quantity": self.unfilled_quantity,
            "n_venues": self.n_venues,
        }


def route_order(
    side: Side,
    quantity: int,
    venues: Sequence[VenueQuote | dict],
) -> SorResult:
    """Plan a smart-order-routing execution across venues.

    For ``side="buy"`` we sort venues by ascending effective ask
    (``ask + fee_per_share``) and fill greedily up to each ``ask_size``;
    for ``side="sell"`` we sort by descending effective bid
    (``bid − fee_per_share``) and fill up to each ``bid_size``. Each child
    order's price is tick-quantised. Raises ``ValueError`` on empty venues,
    non-positive quantity, or unknown side.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not venues:
        raise ValueError("venues must be non-empty")

    # Normalise dict -> VenueQuote.
    norm: list[VenueQuote] = []
    for v in venues:
        if isinstance(v, VenueQuote):
            norm.append(v)
        elif isinstance(v, dict):
            norm.append(VenueQuote(
                venue=str(v["venue"]),
                bid=float(v["bid"]), bid_size=int(v["bid_size"]),
                ask=float(v["ask"]), ask_size=int(v["ask_size"]),
                fee_per_share=float(v.get("fee_per_share", 0.0)),
                tick_size=float(v.get("tick_size", 0.01)),
            ))
        else:
            raise ValueError("each venue must be a VenueQuote or dict")

    if side == "buy":
        # ascending effective ask
        ordered = sorted(norm, key=lambda q: q.ask + q.fee_per_share)
    else:
        # descending effective bid
        ordered = sorted(norm, key=lambda q: -(q.bid - q.fee_per_share))

    remaining = quantity
    child_orders: list[dict] = []
    total_notional = 0.0
    total_fees = 0.0
    filled = 0
    for v in ordered:
        if remaining <= 0:
            break
        if side == "buy":
            depth = v.ask_size
            raw_price = v.ask
            # Quantise price DOWN to tick for the buyer (we pay no more than quoted).
            n_ticks = math.floor(raw_price / v.tick_size + 1e-9) if v.tick_size > 0 else 0
            price = round(n_ticks * v.tick_size, 10) if v.tick_size > 0 else raw_price
        else:
            depth = v.bid_size
            raw_price = v.bid
            # Quantise price UP to tick for the seller (we receive no less than quoted).
            n_ticks = math.ceil(raw_price / v.tick_size - 1e-9) if v.tick_size > 0 else 0
            price = round(n_ticks * v.tick_size, 10) if v.tick_size > 0 else raw_price
        if depth <= 0:
            continue
        fill = min(remaining, depth)
        fee = fill * v.fee_per_share
        child_orders.append({
            "venue": v.venue,
            "side": side,
            "quantity": fill,
            "price": price,
            "fee": fee,
            "effective_price": (price + v.fee_per_share) if side == "buy" else (price - v.fee_per_share),
        })
        total_notional += fill * price
        total_fees += fee
        filled += fill
        remaining -= fill

    wap = total_notional / filled if filled > 0 else 0.0
    return SorResult(
        side=side,
        quantity=quantity,
        child_orders=child_orders,
        weighted_avg_price=wap,
        total_fees=total_fees,
        filled_quantity=filled,
        unfilled_quantity=remaining,
        n_venues=len(norm),
    )