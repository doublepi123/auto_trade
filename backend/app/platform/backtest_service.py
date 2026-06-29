from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.analytics import PerformanceAnalytics
from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource, FillEvent
from app.platform.portfolio import Portfolio
from app.platform.registry import get_default_registry
from app.platform.runner import PlatformRunner
from app.platform.sdk import Strategy


class _BacktestCollector:
    """Subscribe to fills; track cash, positions (qty), last close per symbol, fills, equity curve.

    Cash / positions / realized PnL are delegated to a central :class:`Portfolio` instance.
    """

    def __init__(self, initial_cash: Decimal, symbols: list[str]) -> None:
        self.portfolio = Portfolio(initial_cash=initial_cash)
        self.symbols = symbols
        self.last_close: dict[str, Decimal] = {}
        self.fills: list[FillEvent] = []
        self.equity_curve: list[dict[str, Any]] = []

    def on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        self.portfolio.on_fill(event)
        self.fills.append(event)

    def update_price(self, symbol: str, close: Decimal) -> None:
        self.last_close[symbol] = close

    def nav(self) -> Decimal:
        # Fill missing prices with avg_cost to avoid silently valuing
        # positions at zero.
        prices = dict(self.last_close)
        for sym, pos in self.portfolio.positions.items():
            if pos.quantity != 0 and sym not in prices:
                prices[sym] = pos.avg_cost
        return self.portfolio.nav(prices)

    def snapshot(self, timestamp: Any) -> dict[str, Any]:
        nav = self.nav()
        self.equity_curve.append({"timestamp": timestamp, "nav": float(nav)})
        return {"timestamp": timestamp, "nav": float(nav)}


class PlatformBacktestService:
    def run(
        self,
        strategy_name: str,
        params: dict[str, Any],
        symbols: list[str],
        bars: list[dict[str, Any]],
        initial_cash: Decimal = Decimal("100000"),
    ) -> dict[str, Any]:
        registry = get_default_registry()
        strategy_cls = registry.get(strategy_name)
        strategy: Strategy = strategy_cls(params=params)  # type: ignore[call-arg]

        bus = EventBus()
        collector = _BacktestCollector(initial_cash, symbols)
        bus.subscribe("fill", collector.on_fill)

        runner = PlatformRunner(
            symbols=symbols,
            strategy=strategy,
            mode="paper",
            bus=bus,
        )

        symbol_set = frozenset(symbols)
        for raw in bars:
            bar_symbol = raw.get("symbol", "")
            if bar_symbol and bar_symbol not in symbol_set:
                raise ValueError(f"Bar symbol '{bar_symbol}' not in strategy symbols {symbols}")
            bar = BarEvent(
                timestamp=raw["timestamp"],
                source=EventSource.MARKET,
                symbol=bar_symbol,
                open=Decimal(str(raw["open"])),
                high=Decimal(str(raw["high"])),
                low=Decimal(str(raw["low"])),
                close=Decimal(str(raw["close"])),
                volume=int(raw.get("volume", 0)),
            )
            symbol = bar.symbol or ""
            collector.update_price(symbol, bar.close)
            runner.on_bar(bar)
            collector.snapshot(bar.timestamp)

        final_nav = collector.nav()
        equity = [pt["nav"] for pt in collector.equity_curve]
        analytics = PerformanceAnalytics().analyze(equity, collector.fills)
        return {
            "equity_curve": collector.equity_curve,
            "fills": [f.to_dict() for f in collector.fills],
            "final_positions": {sym: collector.portfolio.quantities().get(sym, 0) for sym in symbols},
            "stats": {
                "initial_cash": float(initial_cash),
                "final_nav": float(final_nav),
                "pnl": float(final_nav - initial_cash),
                "realized_pnl": float(collector.portfolio.realized_pnl),
                "num_fills": len(collector.fills),
                "num_bars": len(bars),
            },
            "analytics": analytics,
        }
