from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from app.platform.events import BarEvent, FillEvent, RiskEvent
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_risk import PortfolioRiskController

__all__ = ["RiskEngine"]


class RiskEngine:
    """事件驱动的组合风控引擎：从成交更新持仓，从 bar 评估敞口/回撤。"""

    def __init__(
        self,
        config: PortfolioConfig | None = None,
        controller: PortfolioRiskController | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.controller = controller or (PortfolioRiskController(config, clock=clock) if config else None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._positions: dict[str, dict[str, Any]] = {}
        self._cash: Decimal = Decimal("0")

    def set_cash(self, cash: Decimal) -> None:
        self._cash = cash

    def on_fill(self, fill: FillEvent) -> list[RiskEvent]:
        symbol = fill.symbol or ""
        pos = self._positions.get(symbol, {"quantity": 0})
        qty = pos["quantity"]
        if fill.side == "BUY":
            qty += fill.quantity
        else:
            qty -= fill.quantity
        self._positions[symbol] = {"quantity": qty}
        return []

    def evaluate(self, prices: dict[str, Decimal], timestamp: datetime | None = None) -> list[RiskEvent]:
        if self.controller is None:
            return []
        ts = timestamp or self._clock()
        nav = self._cash
        for symbol, pos in self._positions.items():
            price = prices.get(symbol, Decimal("0"))
            nav += Decimal(int(pos.get("quantity", 0))) * price
        events = self.controller.check(prices, self._positions, nav, timestamp=ts)
        events.extend(self.controller.drawdown(nav, timestamp=ts))
        return events
