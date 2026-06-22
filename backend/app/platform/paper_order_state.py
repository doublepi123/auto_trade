from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.sdk import OrderIntent


@dataclass
class PaperOrderState:
    order_id: str
    intent: OrderIntent
    status: str = "SUBMITTED"
    filled_quantity: int = 0
    fills: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity

    def fill(self, qty: int, price: Decimal, slippage: Decimal = Decimal("0"), commission: Decimal = Decimal("0")) -> None:
        self.filled_quantity += qty
        self.fills.append({"quantity": qty, "price": price, "slippage": slippage, "commission": commission})
        if self.filled_quantity >= self.intent.quantity:
            self.status = "FILLED"
        else:
            self.status = "PARTIAL_FILLED"
