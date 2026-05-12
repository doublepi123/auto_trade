from decimal import Decimal

from app.core.broker import Position, Quote
from app.core.engine import EngineState, StrategyParams
from app.runner import AppRunner, get_runner


class _NoopNotifier:
    def notify_order(self, *args: object) -> bool:
        return True

    def notify_risk_event(self, *args: object) -> bool:
        return True


class TestAppRunner:
    def test_runner_init_defaults(self) -> None:
        runner = AppRunner()
        assert runner._running is False
        assert runner._thread is None
        assert runner.engine.state == "flat"
        assert runner.risk.kill_switch is False

    def test_get_runner_singleton(self) -> None:
        r1 = get_runner()
        r2 = get_runner()
        assert r1 is r2

    def test_runner_stop_when_not_running(self) -> None:
        runner = AppRunner()
        runner.stop()
        assert runner._running is False

    def test_runner_start_stop_cycle(self) -> None:
        runner = AppRunner()
        runner.start()
        assert runner._running is True
        runner.stop()
        assert runner._running is False

    def test_runner_double_start(self) -> None:
        runner = AppRunner()
        runner.start()
        first_thread = runner._thread
        runner.start()
        assert runner._thread is first_thread
        runner.stop()

    def test_broadcast_status_no_connections(self) -> None:
        runner = AppRunner()
        runner._broadcast_status()

    def test_risk_rejection_rolls_back_triggered_engine_state(self) -> None:
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.risk.pause("testing")
        runner.notifier = _NoopNotifier()
        runner._record_risk_event = lambda reason: None

        runner._on_quote(Quote(symbol="AAPL.US", last_price=99.0, bid=98.5, ask=99.5, timestamp=""))

        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_trigger_at is None
        assert runner.engine.last_trigger_price == 0.0

    def test_missing_position_rolls_back_sell_trigger(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._running = True
        runner.broker = Broker()
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.notifier = _NoopNotifier()
        runner._record_risk_event = lambda reason: None

        runner._on_quote(Quote(symbol="AAPL.US", last_price=201.0, bid=200.5, ask=201.5, timestamp=""))

        assert runner.engine.state == EngineState.LONG
        assert runner.engine.last_trigger_at is None

    def test_sell_uses_matching_symbol_position(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted_quantity: Decimal | None = None

            def get_positions(self) -> list[Position]:
                return [
                    Position(symbol="MSFT.US", side="LONG", quantity=Decimal("2"), avg_price=Decimal("300")),
                    Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150")),
                ]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal):
                self.submitted_quantity = quantity

                class Result:
                    broker_order_id = "order-1"

                return Result()

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        runner._record_order = lambda *args: None

        executed = runner._execute_sell("AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, ""))

        assert executed is True
        assert broker.submitted_quantity == Decimal("5")
