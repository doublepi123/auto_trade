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
            old_qty = pos.quantity
            new_qty = old_qty + qty
            if old_qty < 0 and new_qty > 0:
                # Closing a short position — realize on the covered portion first,
                # then reset cost basis for the new long position.
                covered = min(qty, -old_qty)
                self.realized_pnl += (pos.avg_cost - price) * Decimal(covered) - fill.commission
                remaining = qty - covered
                if remaining > 0:
                    pos.quantity = remaining
                    pos.avg_cost = price
                else:
                    pos.quantity = 0
                    pos.avg_cost = Decimal("0")
            elif old_qty < 0 and new_qty <= 0:
                # Still short — only add to the short position cost basis.
                self.realized_pnl -= fill.commission
                if new_qty != 0:
                    pos.avg_cost = (
                        Decimal(old_qty) * pos.avg_cost + Decimal(qty) * price
                    ) / Decimal(new_qty)
                pos.quantity = new_qty
            else:
                # Old position long or zero — standard long cost averaging.
                if new_qty > 0:
                    pos.avg_cost = (
                        Decimal(old_qty) * pos.avg_cost + Decimal(qty) * price
                    ) / Decimal(new_qty)
                pos.quantity = new_qty
            self.cash -= Decimal(qty) * price + fill.commission
        else:  # SELL
            old_qty = pos.quantity
            new_qty = old_qty - qty
            if old_qty > 0 and new_qty < 0:
                # Closing a long position and flipping to short.
                covered = min(qty, old_qty)
                self.realized_pnl += (price - pos.avg_cost) * Decimal(covered) - fill.commission
                remaining = qty - covered
                if remaining > 0:
                    pos.quantity = -remaining
                    pos.avg_cost = price
                else:
                    pos.quantity = 0
                    pos.avg_cost = Decimal("0")
            elif old_qty > 0 and new_qty >= 0:
                # Still long — standard long realized PnL.
                self.realized_pnl += (price - pos.avg_cost) * Decimal(qty) - fill.commission
                pos.quantity = new_qty
                if new_qty == 0:
                    pos.avg_cost = Decimal("0")
            else:
                # Already short or zero — add to short position.
                self.realized_pnl -= fill.commission
                if new_qty != 0:
                    pos.avg_cost = (
                        Decimal(old_qty) * pos.avg_cost - Decimal(qty) * price
                    ) / Decimal(new_qty)
                pos.quantity = new_qty
            self.cash += Decimal(qty) * price - fill.commission

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
