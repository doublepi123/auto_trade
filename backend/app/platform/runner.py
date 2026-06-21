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
    OrderEvent,
    OrderIntentEvent,
    QuoteEvent,
)
from app.platform.sdk import OrderIntent, Strategy
from app.platform.simbroker import SimBroker
from app.platform.store import EventStore


class PlatformRunner:
    """平台级运行器：用统一事件流驱动策略插件。

    Phase 1 支持两种模式：
    - backtest: 使用 SimBroker 撮合，从外部喂入 bar/quote 事件。
    - live: 策略产生 OrderIntent，通过回调交给现有 TradeExecutionService。
    """

    def __init__(
        self,
        symbol: str,
        strategy: Strategy,
        mode: str,
        bus: EventBus | None = None,
        store: EventStore | None = None,
        clock: Callable[[], datetime] | None = None,
        live_order_handler: Callable[[OrderIntent], None] | None = None,
    ) -> None:
        self.symbol = symbol
        self.strategy = strategy
        self.mode = mode
        self.bus = bus or EventBus()
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.live_order_handler = live_order_handler
        self._positions: dict[str, dict[str, Any]] = {}
        self._sim_broker: SimBroker | None = None
        if mode == "backtest":
            self._sim_broker = SimBroker(clock=self.clock)
            self.bus.subscribe("fill", self._on_fill)

    def _context(self) -> StrategyContext:
        return StrategyContext(
            symbol=self.symbol,
            positions=self._positions,
            params=self.strategy.params,
            clock=self.clock,
        )

    def _emit(self, event: Event) -> None:
        self.bus.publish(event)
        if self.store is not None:
            self.store.append(event)

    def _execute_intent(self, intent: OrderIntent, timestamp: datetime | None = None) -> None:
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
        if self.mode == "backtest" and self._sim_broker is not None:
            order_event = self._sim_broker.submit(intent, timestamp=ts)
            self._emit(order_event)
        elif self.mode == "live" and self.live_order_handler is not None:
            self.live_order_handler(intent)

    def on_bar(self, bar: BarEvent) -> None:
        self._emit(bar)
        intents = self.strategy.on_bar(self._context(), bar)
        for intent in intents:
            self._execute_intent(intent, timestamp=bar.timestamp)
        if self.mode == "backtest" and self._sim_broker is not None:
            fills = self._sim_broker.on_bar(bar)
            for fill in fills:
                self._emit(fill)

    def on_quote(self, quote: QuoteEvent) -> None:
        self._emit(quote)
        intents = self.strategy.on_quote(self._context(), quote)
        for intent in intents:
            self._execute_intent(intent, timestamp=quote.timestamp)

    def _on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        fill = event
        symbol = fill.symbol or self.symbol
        pos = self._positions.get(symbol, {"quantity": 0})
        qty = pos["quantity"]
        if fill.side == "BUY":
            qty += fill.quantity
        else:
            qty -= fill.quantity
        self._positions[symbol] = {"quantity": qty}
        follow_up = self.strategy.on_fill(self._context(), fill)
        for intent in follow_up:
            self._execute_intent(intent, timestamp=fill.timestamp)
