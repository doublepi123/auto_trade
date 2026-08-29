"""Pure FIFO round-trip pairing for PaperBroker fill streams.

Pairs long-only BUY/SELL ``FillEvent``s into per-trade ``RoundTrip`` records so
offline screening can feed the signal-edge gate. No I/O, no DB, no clock.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from app.platform.events import FillEvent


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    entry_notional: Decimal
    net_return_pct: float


@dataclass(frozen=True)
class OpenLot:
    symbol: str
    entry_at: datetime
    entry_price: Decimal
    quantity: int
    remaining_fees: Decimal


@dataclass
class _LedgerLot:
    """Mutable FIFO lot: entry fill facts plus its uncharged commission pool."""

    entry_at: datetime
    entry_price: Decimal
    remaining_quantity: int
    remaining_commission: Decimal


def _take_share(pool: Decimal, take: int, whole: int) -> Decimal:
    # Pro-rata share of a commission pool; when `take` consumes the whole
    # remainder the pool itself is charged, so successive shares sum back to
    # the original commission exactly (no fee leakage from rounding).
    if take == whole:
        return pool
    return pool * Decimal(take) / Decimal(whole)


def pair_round_trips(fills: Sequence[FillEvent]) -> tuple[list[RoundTrip], list[OpenLot]]:
    """Pair long-only fills into round trips via per-symbol FIFO ledgers.

    One SELL consuming multiple entry lots emits one ``RoundTrip`` per lot
    (FIFO per lot, never blended into a qty-weighted average entry) so the
    downstream edge gate counts trades, not closing fills. An unmatched or
    over-quantity SELL raises ``ValueError``: paper fill streams are complete,
    so such a fill is an input error, and silently dropping it would shrink
    the edge sample.
    """
    trips: list[RoundTrip] = []
    ledgers: dict[str, deque[_LedgerLot]] = defaultdict(deque)

    for fill in sorted(fills, key=lambda f: f.timestamp):
        symbol = fill.symbol
        if symbol is None:
            raise ValueError(f"Fill without symbol cannot be paired: {fill.broker_order_id}")

        if fill.side == "BUY":
            ledgers[symbol].append(
                _LedgerLot(
                    entry_at=fill.timestamp,
                    entry_price=fill.price,
                    remaining_quantity=fill.quantity,
                    remaining_commission=fill.commission,
                )
            )
            continue
        if fill.side != "SELL":
            raise ValueError(f"Unsupported fill side {fill.side!r} for {symbol}; long-only pairing")

        ledger = ledgers[symbol]
        if sum(lot.remaining_quantity for lot in ledger) < fill.quantity:
            raise ValueError(
                f"SELL {fill.quantity} of {symbol} at {fill.timestamp.isoformat()} "
                f"exceeds open lots; long-only paper fills must close opened positions"
            )

        remaining_quantity = fill.quantity
        remaining_commission = fill.commission
        while remaining_quantity > 0:
            lot = ledger[0]
            take = min(remaining_quantity, lot.remaining_quantity)
            entry_share = _take_share(lot.remaining_commission, take, lot.remaining_quantity)
            exit_share = _take_share(remaining_commission, take, remaining_quantity)

            gross_pnl = (fill.price - lot.entry_price) * take
            entry_notional = lot.entry_price * take
            fees = entry_share + exit_share
            net_pnl = gross_pnl - fees
            trips.append(
                RoundTrip(
                    symbol=symbol,
                    entry_at=lot.entry_at,
                    exit_at=fill.timestamp,
                    entry_price=lot.entry_price,
                    exit_price=fill.price,
                    quantity=take,
                    gross_pnl=gross_pnl,
                    fees=fees,
                    net_pnl=net_pnl,
                    entry_notional=entry_notional,
                    net_return_pct=float(net_pnl / entry_notional * 100),
                )
            )

            lot.remaining_quantity -= take
            lot.remaining_commission -= entry_share
            if lot.remaining_quantity == 0:
                ledger.popleft()
            remaining_quantity -= take
            remaining_commission -= exit_share

    open_lots = [
        OpenLot(
            symbol=symbol,
            entry_at=lot.entry_at,
            entry_price=lot.entry_price,
            quantity=lot.remaining_quantity,
            remaining_fees=lot.remaining_commission,
        )
        for symbol, ledger in ledgers.items()
        for lot in ledger
    ]
    return trips, open_lots
