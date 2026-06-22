from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

from app.platform.events import Event, FillEvent
from app.platform.portfolio_config import PortfolioConfig
from app.platform.store import EventStore


class AttributionService:
    """Per-symbol realized (FIFO) + unrealized PnL attribution from stored fills."""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    def _load_fills(self, symbol: str) -> list[FillEvent]:
        events: list[Event] = self.store.load(symbol=symbol, limit=100000)
        return [e for e in events if isinstance(e, FillEvent)]

    def attribute(
        self,
        config: PortfolioConfig,
        prices: dict[str, Decimal] | None = None,
    ) -> dict[str, Any]:
        prices = prices or {}
        per_symbol: dict[str, dict[str, Any]] = {}
        total_realized = Decimal("0")
        total_unrealized = Decimal("0")
        for symbol in config.symbols:
            fills = self._load_fills(symbol)
            lots: deque[tuple[int, Decimal]] = deque()  # (qty, price) FIFO for BUY
            realized = Decimal("0")
            commissions = Decimal("0")
            for fill in fills:
                commissions += fill.commission
                if fill.side == "BUY":
                    lots.append((fill.quantity, fill.price))
                else:  # SELL — match FIFO
                    remaining = fill.quantity
                    while remaining > 0 and lots:
                        lot_qty, lot_price = lots[0]
                        matched = min(remaining, lot_qty)
                        realized += (fill.price - lot_price) * Decimal(matched)
                        remaining -= matched
                        if matched == lot_qty:
                            lots.popleft()
                        else:
                            lots[0] = (lot_qty - matched, lot_price)
            remaining_qty = sum(q for q, _ in lots)
            if remaining_qty > 0:
                total_cost = sum(Decimal(q) * p for q, p in lots)
                avg_cost = total_cost / Decimal(remaining_qty)
            else:
                avg_cost = Decimal("0")
            realized -= commissions
            current_price = prices.get(symbol)
            if current_price is not None and remaining_qty > 0:
                unrealized = (current_price - avg_cost) * Decimal(remaining_qty)
            else:
                unrealized = Decimal("0")
            per_symbol[symbol] = {
                "quantity": remaining_qty,
                "avg_cost": float(avg_cost),
                "realized_pnl": float(realized),
                "unrealized_pnl": float(unrealized),
                "num_fills": len(fills),
            }
            total_realized += realized
            total_unrealized += unrealized
        return {
            "per_symbol": per_symbol,
            "totals": {
                "realized_pnl": float(total_realized),
                "unrealized_pnl": float(total_unrealized),
            },
        }
