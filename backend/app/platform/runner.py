from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.platform.bus import EventBus
from app.platform.context import StrategyContext
from app.platform.events import (
    BarEvent,
    Event,
    EventSource,
    FillEvent,
    OrderIntentEvent,
    QuoteEvent,
    RiskEvent,
)
from app.platform.execution import ExecutionClient, LiveExecutionClient
from app.platform.indicators import IndicatorService
from app.platform.paper_broker import PaperBroker
from app.platform.risk_engine import RiskEngine
from app.platform.scheduler import Scheduler
from app.platform.sdk import OrderIntent, Strategy
from app.platform.session_filter import MarketSessionFilter
from app.platform.store import EventStore
from app.platform.universe import Universe


class PlatformRunner:
    """平台级运行器：用统一事件流驱动策略插件。

    支持三种模式：
    - backtest / paper: 使用 PaperBroker 撮合（partial fill、滑点、费用），从外部喂入 bar/quote。
    - live: 策略产生 OrderIntent，通过回调交给现有 TradeExecutionService。
    """

    def __init__(
        self,
        symbol: str = "",
        strategy: Strategy = None,  # type: ignore[assignment]
        mode: str = "",
        bus: EventBus | None = None,
        store: EventStore | None = None,
        clock: Callable[[], datetime] | None = None,
        live_order_handler: Callable[[OrderIntent], None] | None = None,
        symbols: list[str] | None = None,
        broker: ExecutionClient | None = None,
        risk_engine: RiskEngine | None = None,
        indicators: IndicatorService | None = None,
        universe: Universe | None = None,
        scheduler: Scheduler | None = None,
        session_filter: MarketSessionFilter | None = None,
        allowed_sessions: tuple[str, ...] = ("rth", "pre", "post"),
    ) -> None:
        self.symbols = list(symbols) if symbols else [symbol]
        self.strategy = strategy
        self.mode = mode
        self.bus = bus or EventBus()
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.live_order_handler = live_order_handler
        self._positions: dict[str, dict[str, Any]] = {}
        self._broker: ExecutionClient | None = broker
        if mode in ("backtest", "paper"):
            self._broker = broker or PaperBroker(clock=self.clock)
            self.bus.subscribe("fill", self._on_fill)
        elif mode == "live" and self.live_order_handler is not None and self._broker is None:
            # Live runner with a handler but no explicit broker: adapt the handler
            # into the ExecutionClient shape so _execute_intent can go through a
            # uniform code path and also emit an OrderEvent for live submissions.
            self._broker = LiveExecutionClient(self.live_order_handler, clock=self.clock)
        self.risk_engine = risk_engine or RiskEngine()
        self.bus.subscribe("fill", self._on_risk_fill)
        self.indicators = indicators
        self.universe = universe
        self.scheduler = scheduler
        self.session_filter = session_filter
        self.allowed_sessions = allowed_sessions
        self._warming_up: bool = False

    @property
    def symbol(self) -> str | None:
        return self.symbols[0] if self.symbols else None

    def _context(self, symbol: str | None = None) -> StrategyContext:
        return StrategyContext(
            symbol=symbol or (self.symbols[0] if self.symbols else ""),
            positions=self._positions,
            params=self.strategy.params,
            clock=self.clock,
            indicators=self.indicators,
        )

    def _emit(self, event: Event) -> None:
        self.bus.publish(event)
        if self.store is not None:
            self.store.append(event)

    def _execute_intent(self, intent: OrderIntent, timestamp: datetime | None = None) -> None:
        if self._warming_up:
            return
        ts = timestamp or self.clock()
        self._emit(
            OrderIntentEvent(
                timestamp=ts,
                source=EventSource.STRATEGY,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                reason=intent.reason,
            )
        )
        if self._broker is not None:
            order_event = self._broker.submit(intent, timestamp=ts)
            self._emit(order_event)
        elif self.mode == "live" and self.live_order_handler is not None:
            # Backward-compat fallback: live runner constructed without a broker
            # (e.g. main.py lifespan). Real broker fills arrive async via broker.
            self.live_order_handler(intent)

    def submit_intent(self, intent: OrderIntent, timestamp: datetime | None = None) -> None:
        """Public entry point for external drivers (e.g. PortfolioRunner) to submit an OrderIntent."""
        self._execute_intent(intent, timestamp=timestamp)

    def on_bar(self, bar: BarEvent) -> None:
        self._emit(bar)
        if self.indicators is not None:
            self.indicators.on_bar(bar)
        if self.scheduler is not None:
            self.scheduler.on_bar(bar)
        symbol = bar.symbol or ""
        route = (not self.symbols or symbol in self.symbols)
        if self.universe is not None:
            route = route and self.universe.contains(symbol, bar)
        if self.session_filter is not None:
            route = route and self.session_filter.allows(bar.timestamp, self.allowed_sessions)
        if route:
            intents = self.strategy.on_bar(self._context(symbol), bar)
            for intent in intents:
                self._execute_intent(intent, timestamp=bar.timestamp)
        if self.mode in ("backtest", "paper") and self._broker is not None:
            fills = self._broker.on_bar(bar)
            for fill in fills:
                self._emit(fill)
        if self.risk_engine is not None and self.mode in ("backtest", "paper"):
            risk_symbol = bar.symbol or ""
            risk_events = self.risk_engine.evaluate({risk_symbol: bar.close}, timestamp=bar.timestamp)
            for evt in risk_events:
                self._emit(evt)

    def warmup(self, bars: list[BarEvent]) -> None:
        """参考 Lean SetWarmup：喂入历史 bar 预热指标/策略状态，不产生真实订单。"""
        self._warming_up = True
        try:
            for bar in bars:
                self._emit(bar)
                if self.indicators is not None:
                    self.indicators.on_bar(bar)
                symbol = bar.symbol or ""
                route = (not self.symbols or symbol in self.symbols)
                if self.universe is not None:
                    route = route and self.universe.contains(symbol, bar)
                if route:
                    # 调用策略以预热其内部状态；产生的 intent 由 _warming_up 守卫丢弃。
                    self.strategy.on_bar(self._context(symbol), bar)
        finally:
            self._warming_up = False

    def on_quote(self, quote: QuoteEvent) -> None:
        self._emit(quote)
        symbol = quote.symbol or ""
        route = (not self.symbols or symbol in self.symbols)
        if self.universe is not None:
            route = route and self.universe.contains(symbol, None)
        if route:
            intents = self.strategy.on_quote(self._context(symbol), quote)
            for intent in intents:
                self._execute_intent(intent, timestamp=quote.timestamp)

    def _on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        fill = event
        symbol = fill.symbol or (self.symbols[0] if self.symbols else "")
        pos = self._positions.get(symbol, {"quantity": 0})
        qty = pos["quantity"]
        if fill.side == "BUY":
            qty += fill.quantity
        else:
            qty -= fill.quantity
        self._positions[symbol] = {"quantity": qty}
        follow_up = self.strategy.on_fill(self._context(symbol), fill)
        for intent in follow_up:
            self._execute_intent(intent, timestamp=fill.timestamp)

    def _on_risk_fill(self, event: Event) -> None:
        """Adapter: 转发 fill 给 RiskEngine 更新持仓（返回值忽略）。"""
        if not isinstance(event, FillEvent):
            return
        self.risk_engine.on_fill(event)
