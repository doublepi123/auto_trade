from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.events import Event, FillEvent

__all__ = ["Position", "PositionEngine"]


@dataclass
class Position:
    symbol: str
    side: str = "FLAT"  # FLAT / LONG / SHORT
    quantity: int = 0  # magnitude, always >= 0
    avg_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def is_open(self) -> bool:
        return self.side != "FLAT" and self.quantity > 0


class PositionEngine:
    """持仓引擎（参考 Nautilus PositionEngine）：净额模式，支持多空翻转与每仓 realized PnL。

    - FLAT + BUY -> LONG；FLAT + SELL -> SHORT
    - 同向加仓 -> 加权均价
    - 反向：先平现有仓（实现 PnL），剩余即翻转开反向新仓
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        symbol = event.symbol or ""
        pos = self._positions.setdefault(symbol, Position(symbol=symbol))
        qty = event.quantity
        price = event.price
        commission = event.commission

        if pos.side == "FLAT":
            pos.quantity = qty
            pos.avg_cost = price
            pos.side = "LONG" if event.side == "BUY" else "SHORT"
            return

        same_direction = (pos.side == "LONG" and event.side == "BUY") or (
            pos.side == "SHORT" and event.side == "SELL"
        )
        if same_direction:
            new_qty = pos.quantity + qty
            pos.avg_cost = (Decimal(pos.quantity) * pos.avg_cost + Decimal(qty) * price) / Decimal(new_qty)
            pos.quantity = new_qty
            return

        # opposite direction: reduce then maybe flip
        reduce_qty = min(qty, pos.quantity)
        if pos.side == "LONG":
            pos.realized_pnl += (price - pos.avg_cost) * Decimal(reduce_qty)
        else:  # SHORT
            pos.realized_pnl += (pos.avg_cost - price) * Decimal(reduce_qty)
        pos.realized_pnl -= commission
        pos.quantity -= reduce_qty
        remaining = qty - reduce_qty
        if pos.quantity == 0 and remaining == 0:
            pos.side = "FLAT"
            pos.avg_cost = Decimal("0")
        elif pos.quantity == 0:
            # flip into the opposite side
            pos.quantity = remaining
            pos.avg_cost = price
            pos.side = "LONG" if event.side == "BUY" else "SHORT"

    def subscribe(self, bus: Any) -> None:
        bus.subscribe("fill", self.on_fill)

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol=symbol))

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.is_open]
