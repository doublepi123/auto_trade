from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from app.platform.portfolio_allocator import PortfolioAllocator
from app.platform.portfolio_config import PortfolioConfig
from app.platform.runner import PlatformRunner
from app.platform.sdk import OrderIntent

__all__ = ["PortfolioRunner"]


class PortfolioRunner:
    """周期性组合再平衡驱动：用 PortfolioAllocator 生成再平衡 OrderIntent，提交给底层 PlatformRunner。

    Positions / cash 可由可注入的 provider 提供，便于实盘接线（默认从 runner 持仓与初始现金推导）。
    """

    def __init__(
        self,
        config: PortfolioConfig,
        runner: PlatformRunner,
        allocator: PortfolioAllocator | None = None,
        prices_provider: Callable[[], dict[str, Decimal]] | None = None,
        cash_provider: Callable[[], Decimal] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.allocator = allocator or PortfolioAllocator(config)
        self.prices_provider = prices_provider
        self.cash_provider = cash_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def rebalance(
        self,
        prices: dict[str, Decimal] | None = None,
        cash: Decimal | None = None,
    ) -> list[OrderIntent]:
        prices = prices if prices is not None else (self.prices_provider() if self.prices_provider else {})
        cash = cash if cash is not None else (self.cash_provider() if self.cash_provider else Decimal("0"))
        positions = {
            sym: {"quantity": int(pos.get("quantity", 0))}
            for sym, pos in self.runner._positions.items()
        }
        # ensure every configured symbol has an entry
        for sym in self.config.symbols:
            positions.setdefault(sym, {"quantity": 0})
        intents = self.allocator.rebalance(positions, prices, cash)
        ts = self._clock()
        for intent in intents:
            self.runner.submit_intent(intent, timestamp=ts)
        return intents
