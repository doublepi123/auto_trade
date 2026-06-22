from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.events import FillEvent

__all__ = ["Portfolio", "Position"]


@dataclass
class Position:
    quantity: int = 0
    avg_cost: Decimal = field(default_factory=lambda: Decimal("0"))


class Portfolio:
    """中央账户状态（参考 Nautilus Portfolio/Cache）：cash / positions / realized PnL 的单一真相源。

    订阅 fill 即可维护；nav 由当前价计算。回测、归因、风控均从其读取。
    """

    def __init__(self, initial_cash: Decimal = Decimal("0")) -> None:
        self.cash: Decimal = initial_cash
        self.positions: dict[str, Position] = {}
        self.realized_pnl: Decimal = Decimal("0")

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position())

    def on_fill(self, fill: FillEvent) -> None:
        symbol = fill.symbol or ""
        pos = self.position(symbol)
        qty = fill.quantity
        price = fill.price
        if fill.side == "BUY":
            new_qty = pos.quantity + qty
            if new_qty > 0:
                pos.avg_cost = (Decimal(pos.quantity) * pos.avg_cost + Decimal(qty) * price) / Decimal(new_qty)
            pos.quantity = new_qty
            self.cash -= Decimal(qty) * price + fill.commission
        else:  # SELL
            realized = (price - pos.avg_cost) * Decimal(qty) - fill.commission
            self.realized_pnl += realized
            pos.quantity -= qty
            self.cash += Decimal(qty) * price - fill.commission
            if pos.quantity == 0:
                pos.avg_cost = Decimal("0")

    def quantities(self) -> dict[str, int]:
        return {sym: pos.quantity for sym, pos in self.positions.items() if pos.quantity != 0}

    def positions_view(self) -> dict[str, dict[str, Any]]:
        return {
            sym: {"quantity": pos.quantity, "avg_cost": pos.avg_cost}
            for sym, pos in self.positions.items()
            if pos.quantity != 0
        }

    def nav(self, prices: dict[str, Decimal]) -> Decimal:
        total = self.cash
        for sym, pos in self.positions.items():
            total += Decimal(pos.quantity) * prices.get(sym, Decimal("0"))
        return total
