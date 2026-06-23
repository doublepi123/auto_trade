"""P197: multi-strategy concurrent execution isolation.

Gives each strategy its own capital allocation and a private sub-portfolio so
that concurrent strategies do not share cash/positions/PnL. Mirrors Nautilus
Trader's strategy-level capital isolation and Lean's per-portfolio allocation.

The :class:`StrategyIsolationManager` owns one :class:`~app.platform.portfolio.Portfolio`
per strategy, plus a per-strategy cash budget. Fills are routed by an
order-id → strategy map (the runner populates it at submit time) so each
strategy only sees its own trades. NAV is computed both per-strategy (for
risk/PnL attribution) and aggregated (for a global account view).

Capital allocation supports both fixed (absolute) and fractional (weight of
total) modes; the manager validates that allocations do not exceed the total
budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.events import FillEvent
from app.platform.portfolio import Portfolio

__all__ = ["StrategyAllocation", "StrategyIsolationManager"]


@dataclass
class StrategyAllocation:
    """A strategy's capital budget descriptor."""

    strategy_id: str
    capital: Decimal  # absolute cash allocated to this strategy
    weight: Decimal = field(default_factory=lambda: Decimal("0"))  # fraction of total (informational)


class StrategyIsolationManager:
    """Owns per-strategy isolated sub-portfolios + an aggregate view.

    Capital is fully reserved up front: each strategy's ``Portfolio`` starts
    with ``capital`` cash. The aggregate NAV is the sum of per-strategy NAVs,
    so capital is never double-counted.
    """

    def __init__(self, total_capital: Decimal) -> None:
        self.total_capital = total_capital
        self._allocations: dict[str, StrategyAllocation] = {}
        self._portfolios: dict[str, Portfolio] = {}
        self._order_to_strategy: dict[str, str] = {}

    # ---- allocation ------------------------------------------------------

    def allocate(self, strategy_id: str, capital: Decimal) -> StrategyAllocation:
        """Reserve ``capital`` for ``strategy_id``.

        Raises ``ValueError`` if the new total would exceed ``total_capital``
        or if ``strategy_id`` is already allocated.
        """
        if strategy_id in self._allocations:
            raise ValueError(f"strategy '{strategy_id}' already allocated")
        if capital < 0:
            raise ValueError("capital must be non-negative")
        used = sum((a.capital for a in self._allocations.values()), Decimal("0"))
        if used + capital > self.total_capital:
            raise ValueError(
                f"allocation {used + capital} exceeds total capital {self.total_capital}"
            )
        weight = (capital / self.total_capital) if self.total_capital > 0 else Decimal("0")
        alloc = StrategyAllocation(strategy_id=strategy_id, capital=capital, weight=weight)
        self._allocations[strategy_id] = alloc
        self._portfolios[strategy_id] = Portfolio(initial_cash=capital)
        return alloc

    def allocate_weights(self, weights: dict[str, Decimal]) -> dict[str, StrategyAllocation]:
        """Allocate by fractional weights that must sum to <= 1.0."""
        total_weight = sum(weights.values(), Decimal("0"))
        if total_weight > Decimal("1") + Decimal("1e-9"):
            raise ValueError(f"weights sum {total_weight} exceeds 1.0")
        result: dict[str, StrategyAllocation] = {}
        for strategy_id, weight in weights.items():
            capital = (self.total_capital * weight).quantize(Decimal("0.01"))
            result[strategy_id] = self.allocate(strategy_id, capital)
        return result

    def deallocate(self, strategy_id: str) -> None:
        self._allocations.pop(strategy_id, None)
        self._portfolios.pop(strategy_id, None)

    # ---- fill routing ----------------------------------------------------

    def bind_order(self, broker_order_id: str, strategy_id: str) -> None:
        """Record that ``broker_order_id`` belongs to ``strategy_id``."""
        self._order_to_strategy[broker_order_id] = strategy_id

    def on_fill(self, fill: FillEvent) -> str | None:
        """Route a fill to its strategy's sub-portfolio.

        Returns the strategy_id that consumed the fill, or ``None`` if the
        order was not bound (the fill is dropped — defensive, should not happen
        in a wired runner).
        """
        strategy_id = self._order_to_strategy.get(fill.broker_order_id)
        if strategy_id is None or strategy_id not in self._portfolios:
            return None
        self._portfolios[strategy_id].on_fill(fill)
        return strategy_id

    # ---- views -----------------------------------------------------------

    def portfolio(self, strategy_id: str) -> Portfolio:
        if strategy_id not in self._portfolios:
            raise KeyError(f"strategy '{strategy_id}' not allocated")
        return self._portfolios[strategy_id]

    def strategy_nav(self, strategy_id: str, prices: dict[str, Decimal]) -> Decimal:
        return self.portfolio(strategy_id).nav(prices)

    def aggregate_nav(self, prices: dict[str, Decimal]) -> Decimal:
        return sum(
            (p.nav(prices) for p in self._portfolios.values()), Decimal("0")
        )

    def strategy_pnl(self, strategy_id: str, prices: dict[str, Decimal]) -> Decimal:
        """Unrealized + realized PnL for a strategy vs its allocated capital."""
        port = self.portfolio(strategy_id)
        return port.nav(prices) - self._allocations[strategy_id].capital

    def attribution(self, prices: dict[str, Decimal]) -> dict[str, Any]:
        """Per-strategy NAV + PnL snapshot for risk/ops attribution."""
        out: dict[str, Any] = {}
        for strategy_id, alloc in self._allocations.items():
            port = self._portfolios[strategy_id]
            nav = port.nav(prices)
            out[strategy_id] = {
                "capital": alloc.capital,
                "weight": alloc.weight,
                "nav": nav,
                "realized_pnl": port.realized_pnl,
                "total_pnl": nav - alloc.capital,
                "positions": port.positions_view(),
            }
        return out

    def allocated_strategies(self) -> list[str]:
        return list(self._allocations.keys())

    def total_allocated(self) -> Decimal:
        return sum((a.capital for a in self._allocations.values()), Decimal("0"))

    def unallocated_capital(self) -> Decimal:
        return self.total_capital - self.total_allocated()
