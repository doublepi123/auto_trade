from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from app.platform.portfolio_allocator import PortfolioAllocator
from app.platform.portfolio_config import PortfolioConfig
from app.platform.runner import PlatformRunner
from app.platform.sdk import OrderIntent

if TYPE_CHECKING:
    from app.platform.risk_engine import RiskEngine

__all__ = ["PortfolioRunner"]


# Module-level portfolio kill-switch. When armed, rebalance() returns [] and
# does not submit any OrderIntent. Controlled via arm/disarm/is_armed helpers
# and exposed through the /api/portfolio/kill-switch endpoints.
_KILL_SWITCH_ARMED: bool = False


def arm_kill_switch() -> None:
    """Arm the portfolio kill-switch; subsequent rebalance() calls return []."""
    global _KILL_SWITCH_ARMED
    _KILL_SWITCH_ARMED = True


def disarm_kill_switch() -> None:
    """Disarm the portfolio kill-switch, resuming normal rebalance behavior."""
    global _KILL_SWITCH_ARMED
    _KILL_SWITCH_ARMED = False


def is_kill_switch_armed() -> bool:
    """Return True iff the portfolio kill-switch is currently armed."""
    return _KILL_SWITCH_ARMED


def reset_kill_switch_for_tests() -> None:
    """Test-only helper to reset module state between tests."""
    global _KILL_SWITCH_ARMED
    _KILL_SWITCH_ARMED = False


class PortfolioRunner:
    """周期性组合再平衡驱动：用 PortfolioAllocator 生成再平衡 OrderIntent，提交给底层 PlatformRunner。

    Positions / cash 可由可注入的 provider 提供，便于实盘接线（默认从 runner 持仓与初始现金推导）。
    可选注入 ``risk_engine``：若其 controller 在当前持仓/价格下会产出 CRITICAL 级别 RiskEvent，
    则跳过本次再平衡（不发任何 intent）。
    """

    def __init__(
        self,
        config: PortfolioConfig,
        runner: PlatformRunner,
        allocator: PortfolioAllocator | None = None,
        prices_provider: Callable[[], dict[str, Decimal]] | None = None,
        cash_provider: Callable[[], Decimal] | None = None,
        clock: Callable[[], datetime] | None = None,
        risk_engine: "RiskEngine | None" = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.allocator = allocator or PortfolioAllocator(config)
        self.prices_provider = prices_provider
        self.cash_provider = cash_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.risk_engine = risk_engine

    def rebalance(
        self,
        prices: dict[str, Decimal] | None = None,
        cash: Decimal | None = None,
    ) -> list[OrderIntent]:
        # Module-level kill-switch takes precedence over everything else.
        if is_kill_switch_armed():
            return []
        prices = prices if prices is not None else (self.prices_provider() if self.prices_provider else {})
        cash = cash if cash is not None else (self.cash_provider() if self.cash_provider else Decimal("0"))
        positions = {
            sym: {"quantity": int(pos.get("quantity", 0))}
            for sym, pos in self.runner._positions.items()
        }
        # ensure every configured symbol has an entry
        for sym in self.config.symbols:
            positions.setdefault(sym, {"quantity": 0})
        # Risk gate: if a CRITICAL breach would occur at current NAV, skip rebalance.
        if self._would_breach_critical(prices, positions, cash):
            return []
        intents = self.allocator.rebalance(positions, prices, cash)
        ts = self._clock()
        for intent in intents:
            self.runner.submit_intent(intent, timestamp=ts)
        return intents

    def _would_breach_critical(
        self,
        prices: dict[str, Decimal],
        positions: dict[str, dict[str, Any]],
        cash: Decimal,
    ) -> bool:
        """Return True iff the injected risk engine's controller emits any CRITICAL RiskEvent at current NAV."""
        if self.risk_engine is None or self.risk_engine.controller is None:
            return False
        nav = cash
        for sym, pos in positions.items():
            nav += Decimal(int(pos.get("quantity", 0))) * prices.get(sym, Decimal("0"))
        events = self.risk_engine.controller.check(prices, positions, nav, timestamp=self._clock())
        return any(e.severity == "CRITICAL" for e in events)
