from __future__ import annotations

from decimal import Decimal

from app.core.broker import OrderResult, Position, Quote
from app.core.risk import RiskController
from app.services.trade_execution_service import TradeExecutionService


class FakeBroker:
    def __init__(self) -> None:
        self.cash = Decimal("1000")
        self.positions: list[Position] = []
        self.orders: list[tuple[str, str, Decimal, Decimal]] = []

    def get_cash(self) -> Decimal:
        return self.cash

    def get_positions(self) -> list[Position]:
        return self.positions

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        self.orders.append((symbol, side, quantity, price))
        return OrderResult("order-1", symbol, side, quantity, price, "SUBMITTED")


class FakeNotifier:
    def __init__(self) -> None:
        self.orders: list[tuple[str, str, str, str, str]] = []
        self.risks: list[tuple[str, str]] = []

    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool:
        self.orders.append((side, symbol, quantity, price, order_id))
        return True

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        self.risks.append((event_type, reason))
        return True


def test_buy_uses_available_cash_and_records_order() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    recorded: list[tuple[str, str, str, float, float]] = []
    service = TradeExecutionService(record_order=lambda *args: recorded.append(args), record_risk_event=lambda reason: None)

    executed = service.execute("BUY", "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""), broker, RiskController(), notifier)

    assert executed is True
    assert broker.orders == [("AAPL.US", "BUY", Decimal("9"), Decimal("100.0"))]
    assert recorded == [("order-1", "AAPL.US", "BUY", 9.0, 100.0)]
    assert notifier.orders[0][0] == "BUY"


def test_sell_requires_matching_long_position() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=lambda reason: None)

    executed = service.execute("SELL", "AAPL.US", Quote("AAPL.US", 200.0, 199.5, 200.5, ""), broker, RiskController(), notifier)

    assert executed is False
    assert broker.orders == []


def test_sell_records_realized_pnl_for_matching_position() -> None:
    broker = FakeBroker()
    broker.positions = [Position("AAPL.US", "LONG", Decimal("3"), Decimal("150"))]
    risk = RiskController()
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=lambda reason: None)

    executed = service.execute("SELL", "AAPL.US", Quote("AAPL.US", 200.0, 199.5, 200.5, ""), broker, risk, FakeNotifier())

    assert executed is True
    assert broker.orders == [("AAPL.US", "SELL", Decimal("3"), Decimal("200.0"))]
    assert risk.daily_pnl == 150.0


def test_sell_short_uses_available_cash_and_records_order() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    recorded: list[tuple[str, str, str, float, float]] = []
    service = TradeExecutionService(record_order=lambda *args: recorded.append(args), record_risk_event=lambda reason: None)

    executed = service.execute(
        "SELL_SHORT",
        "AAPL.US",
        Quote("AAPL.US", 100.0, 99.5, 100.5, ""),
        broker,
        RiskController(),
        notifier,
    )

    assert executed is True
    assert broker.orders == [("AAPL.US", "SELL", Decimal("9"), Decimal("100.0"))]
    assert recorded == [("order-1", "AAPL.US", "SELL_SHORT", 9.0, 100.0)]
    assert notifier.orders[0][0] == "SELL_SHORT"


def test_buy_to_cover_records_realized_pnl_for_matching_short_position() -> None:
    broker = FakeBroker()
    broker.positions = [Position("AAPL.US", "SHORT", Decimal("4"), Decimal("250"))]
    notifier = FakeNotifier()
    risk = RiskController()
    recorded: list[tuple[str, str, str, float, float]] = []
    service = TradeExecutionService(record_order=lambda *args: recorded.append(args), record_risk_event=lambda reason: None)

    executed = service.execute(
        "BUY_TO_COVER",
        "AAPL.US",
        Quote("AAPL.US", 200.0, 199.5, 200.5, ""),
        broker,
        risk,
        notifier,
    )

    assert executed is True
    assert broker.orders == [("AAPL.US", "BUY", Decimal("4"), Decimal("200.0"))]
    assert recorded == [("order-1", "AAPL.US", "BUY_TO_COVER", 4.0, 200.0)]
    assert notifier.orders[0][0] == "BUY_TO_COVER"
    assert risk.daily_pnl == 200.0


def test_risk_rejection_records_event_without_order() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    risk = RiskController()
    risk.pause("maintenance")
    reasons: list[str] = []
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=reasons.append)

    executed = service.execute("BUY", "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""), broker, risk, notifier)

    assert executed is False
    assert reasons == ["trading is paused"]
    assert notifier.risks == [("REJECTED", "trading is paused")]
    assert broker.orders == []


def test_unknown_action_returns_false_without_risk_event_when_paused() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    risk = RiskController()
    risk.pause("maintenance")
    reasons: list[str] = []
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=reasons.append)

    executed = service.execute("HOLD", "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""), broker, risk, notifier)

    assert executed is False
    assert reasons == []
    assert notifier.risks == []
    assert broker.orders == []
