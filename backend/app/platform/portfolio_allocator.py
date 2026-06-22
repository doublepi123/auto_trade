from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.portfolio_config import PortfolioConfig
from app.platform.sdk import OrderIntent


class PortfolioAllocator:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config

    def rebalance(
        self,
        positions: dict[str, dict[str, Any]],
        prices: dict[str, Decimal],
        cash: Decimal,
    ) -> list[OrderIntent]:
        total_value = cash
        for symbol, pos in positions.items():
            qty = int(pos.get("quantity", 0))
            price = prices.get(symbol, Decimal("0"))
            total_value += Decimal(qty) * price

        intents: list[OrderIntent] = []
        for symbol in self.config.symbols:
            target_weight = self.config.allocations.get(symbol, Decimal("0"))
            target_value = total_value * target_weight
            price = prices.get(symbol, Decimal("0"))
            if price <= 0:
                continue
            target_qty = int((target_value / price).to_integral_value())
            current_qty = int(positions.get(symbol, {}).get("quantity", 0))
            delta = target_qty - current_qty
            if delta == 0:
                continue
            side = "BUY" if delta > 0 else "SELL"
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side=side,
                    quantity=abs(delta),
                    order_type="LIMIT",
                    limit_price=price,
                    reason="portfolio_rebalance",
                )
            )
        return intents
