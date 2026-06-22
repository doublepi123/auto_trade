from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent, QuoteEvent
from app.platform.sdk import OrderIntent

__all__ = ["ExecutionClient", "LiveExecutionClient"]


@runtime_checkable
class ExecutionClient(Protocol):
    """执行客户端抽象（参考 Nautilus ExecutionClient）。

    Paper/Live 各自实现；PaperBroker 结构上已满足该协议。"""

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent: ...

    def cancel(self, order_id: str, timestamp: datetime | None = None) -> OrderEvent: ...

    def modify(self, order_id: str, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent: ...

    def on_bar(self, bar: BarEvent) -> list[FillEvent]: ...

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]: ...


class LiveExecutionClient:
    """Live 执行客户端：submit 转发到注入的 live_order_handler；on_bar/on_quote 不仿真成交
    （实盘成交由真实券商异步回报，平台层在 live 模式不模拟）。
    """

    def __init__(
        self,
        live_order_handler: Callable[[OrderIntent], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._handler = live_order_handler
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._counter = 0

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        self._counter += 1
        order_id = f"live-{self._counter}"
        self._handler(intent)
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            broker_order_id=order_id,
            status="SUBMITTED",
        )

    def cancel(self, order_id: str, timestamp: datetime | None = None) -> OrderEvent:
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=None,
            broker_order_id=order_id,
            status="CANCELLED",
        )

    def modify(self, order_id: str, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            broker_order_id=order_id,
            status="MODIFIED",
        )

    def on_bar(self, bar: BarEvent) -> list[FillEvent]:
        return []

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
