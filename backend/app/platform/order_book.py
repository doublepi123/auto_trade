"""P194: L2 order book matching model.

A standalone price-level order book (FIFO queue per price level, bid/ask sides)
that matches incoming market and limit orders against resting liquidity —
mirroring the level-2 matching semantics in Nautilus Trader's ``OrderBook`` and
QuantConnect Lean's ``FillMatrix``.

This is a self-contained simulation primitive. The PaperBroker keeps its
bar-based matching as the default; an :class:`OrderBook` can be fed
:func:`apply_quote` snapshots to synthesize levels, then :meth:`match` incoming
:class:`~app.platform.sdk.OrderIntent` objects one at a time, producing
:class:`~app.platform.events.FillEvent` lists (including partial fills that walk
across multiple price levels). The book is deterministic and side-effect free
apart from its own internal queues.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from app.platform.events import EventSource, FillEvent
from app.platform.sdk import OrderIntent


@dataclass
class BookLevel:
    """A single price level: FIFO queue of resting (qty, order_id) entries."""

    price: Decimal
    queue: deque[tuple[int, str]] = field(default_factory=deque)

    def total(self) -> int:
        return sum(qty for qty, _ in self.queue)


@dataclass
class OrderBook:
    """A two-sided L2 order book keyed by price.

    Bids are stored descending (best bid = highest price first); asks are
    stored ascending (best ask = lowest price first). Levels with zero resting
    quantity are pruned lazily.
    """

    symbol: str
    tick_size: Decimal = Decimal("0.01")
    _bids: dict[Decimal, BookLevel] = field(default_factory=dict)
    _asks: dict[Decimal, BookLevel] = field(default_factory=dict)

    def _quantize(self, price: Decimal) -> Decimal:
        """Snap a price to the book's tick grid."""
        ticks = (price / self.tick_size).to_integral_value(rounding="ROUND_HALF_EVEN")
        return self.tick_size * ticks

    def apply_quote(self, bids: list[tuple[Decimal, int]], asks: list[tuple[Decimal, int]]) -> None:
        """Replace the book with the given bid/ask level snapshot.

        Each level is seeded with a single anonymous resting entry so that
        matching consumes real liquidity.
        """
        self._bids.clear()
        self._asks.clear()
        for price, qty in bids:
            if qty <= 0:
                continue
            p = self._quantize(price)
            self._bids[p] = BookLevel(price=p, queue=deque([(int(qty), "resting")]))
        for price, qty in asks:
            if qty <= 0:
                continue
            p = self._quantize(price)
            self._asks[p] = BookLevel(price=p, queue=deque([(int(qty), "resting")]))

    def add_resting(self, intent: OrderIntent, order_id: str) -> None:
        """Rest a LIMIT order on the book (does not cross).

        BUY rests on the bid side, SELL on the ask side.
        """
        if intent.order_type != "LIMIT" or intent.limit_price is None:
            return
        price = self._quantize(intent.limit_price)
        side = self._bids if intent.side == "BUY" else self._asks
        level = side.setdefault(price, BookLevel(price=price))
        level.queue.append((intent.quantity, order_id))

    def best_bid(self) -> Decimal | None:
        self._prune()
        prices = sorted(self._bids.keys(), reverse=True)
        return prices[0] if prices else None

    def best_ask(self) -> Decimal | None:
        self._prune()
        prices = sorted(self._asks.keys())
        return prices[0] if prices else None

    def spread(self) -> Decimal | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def depth(self, levels: int = 5) -> dict[str, list[tuple[Decimal, int]]]:
        """Return up to ``levels`` best bid/ask levels with aggregated qty."""
        self._prune()
        bids = sorted(self._bids.items(), key=lambda kv: kv[0], reverse=True)[:levels]
        asks = sorted(self._asks.items(), key=lambda kv: kv[0])[:levels]
        return {
            "bids": [(p, lvl.total()) for p, lvl in bids],
            "asks": [(p, lvl.total()) for p, lvl in asks],
        }

    def _prune(self) -> None:
        for side in (self._bids, self._asks):
            empty = [p for p, lvl in side.items() if lvl.total() <= 0]
            for p in empty:
                del side[p]

    def match(
        self,
        intent: OrderIntent,
        order_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        commission_rate: Decimal = Decimal("0"),
    ) -> list[FillEvent]:
        """Match ``intent`` against resting liquidity.

        MARKET orders cross the spread immediately. LIMIT orders cross only if
        their limit price reaches the opposing best level; otherwise they rest.
        Produces one :class:`FillEvent` per consumed price level (so a single
        intent may yield several partial fills). Returns ``[]`` if nothing
        matched.
        """
        order_id = order_id or f"book-{uuid4().hex[:8]}"
        now = clock() if clock is not None else datetime.now(timezone.utc)
        symbol = intent.symbol

        if intent.order_type == "LIMIT" and intent.limit_price is not None:
            cross = self._crosses(intent.side, intent.limit_price)
            if not cross:
                # Rest on the book; no fill this round.
                self.add_resting(intent, order_id)
                return []

        fills: list[FillEvent] = []
        remaining = intent.quantity
        opposing = self._asks if intent.side == "BUY" else self._bids

        while remaining > 0:
            self._prune()
            best = self._best_level(opposing)
            if best is None:
                break
            level_price, level = best
            # For LIMIT orders, stop once we can no longer reach the level.
            if intent.order_type == "LIMIT" and intent.limit_price is not None:
                if intent.side == "BUY" and level_price > intent.limit_price:
                    break
                if intent.side == "SELL" and level_price < intent.limit_price:
                    break
            level_qty = level.total()
            take = min(remaining, level_qty)
            # Drain from the FIFO queue.
            to_take = take
            while to_take > 0 and level.queue:
                q_qty, q_id = level.queue[0]
                if q_qty <= to_take:
                    level.queue.popleft()
                    to_take -= q_qty
                else:
                    level.queue[0] = (q_qty - to_take, q_id)
                    to_take = 0
            commission = level_price * Decimal(take) * commission_rate
            fills.append(
                FillEvent(
                    timestamp=now,
                    source=EventSource.BROKER,
                    symbol=symbol,
                    broker_order_id=order_id,
                    side=intent.side,
                    quantity=take,
                    price=level_price,
                    commission=commission,
                    partial=(take < remaining),
                )
            )
            remaining -= take

        # A LIMIT order with leftover qty after crossing rests the remainder.
        if remaining > 0 and intent.order_type == "LIMIT" and intent.limit_price is not None:
            leftover = OrderIntent(
                symbol=intent.symbol,
                side=intent.side,
                quantity=remaining,
                order_type="LIMIT",
                limit_price=intent.limit_price,
                reason=intent.reason,
            )
            self.add_resting(leftover, order_id)

        return fills

    def _best_level(self, side: dict[Decimal, BookLevel]) -> tuple[Decimal, BookLevel] | None:
        self._prune()
        if not side:
            return None
        if side is self._bids:
            key_fn = lambda p: p  # type: ignore[arg-type]
            best_price = max(side.keys())
        else:
            best_price = min(side.keys())
        return best_price, side[best_price]

    def _crosses(self, side: str, limit_price: Decimal) -> bool:
        """Whether a LIMIT order's price reaches the opposing best level."""
        if side == "BUY":
            ask = self.best_ask()
            return ask is not None and limit_price >= ask
        bid = self.best_bid()
        return bid is not None and limit_price <= bid


def match_intent(
    book: OrderBook,
    intent: OrderIntent,
    order_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
    commission_rate: Decimal = Decimal("0"),
) -> list[FillEvent]:
    """Functional convenience wrapper around :meth:`OrderBook.match`."""
    return book.match(intent, order_id=order_id, clock=clock, commission_rate=commission_rate)
