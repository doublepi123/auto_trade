import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import runner as runner_module
from app.core.broker import OrderResult, Position, Quote
from app.core.engine import EngineState, StrategyParams
from app.runner import AppRunner, get_runner


class _NoopNotifier:
    def notify_order(self, *args: object) -> bool:
        return True

    def notify_risk_event(self, *args: object) -> bool:
        return True


class TestAppRunner:
    def _stub_trade_callbacks(self, runner: AppRunner) -> None:
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None

    def _execute_buy(self, runner: AppRunner, symbol: str, quote: Quote):
        return runner._trade_svc._execute_buy(
            symbol,
            quote,
            runner.broker,
            runner.risk,
            runner.notifier,
            runner._cash_currency(),
        )

    def _execute_sell(self, runner: AppRunner, symbol: str, quote: Quote):
        return runner._trade_svc._execute_sell(
            symbol,
            quote,
            runner.broker,
            runner.risk,
            runner.notifier,
        )

    def test_runner_init_defaults(self) -> None:
        runner = AppRunner()
        assert runner._running is False
        assert runner._thread is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.risk.kill_switch is False

    def test_execute_llm_order_decision_submits_buy_now(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []

            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol, 222.0, 221.9, 222.1, "")

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 221.75,
            "order_reason": "strong signal",
        })

        assert result == {"executed": True, "status": "FILLED", "order_id": "order-llm-buy", "action": "BUY"}
        assert runner.broker.submitted == [("NVDA.US", "BUY", Decimal("9"), Decimal("221.75"))]
        assert runner.engine.state == EngineState.LONG

    def test_execute_llm_order_decision_cancel_replace(self) -> None:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled = []
                self.submitted = []

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol, 225.0, 224.9, 225.1, "")

            def get_positions(self) -> list[Position]:
                return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("220"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-llm-sell", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-pending", "NVDA.US", "BUY", Decimal("5"), Decimal("221"), "SUBMITTED"),
            broker,
            runner._engine_snapshot(),
        )

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "SELL_NOW",
            "replacement_price": 225.25,
            "order_reason": "replace stale buy with exit",
        })

        assert result == {"executed": True, "status": "FILLED", "order_id": "order-llm-sell", "action": "SELL"}
        assert broker.cancelled == ["order-pending"]
        assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), Decimal("225.25"))]
        assert runner._trade_svc.has_pending_order is False
        assert runner.engine.state == EngineState.FLAT

    def test_execute_llm_order_decision_replaces_pending_order_for_new_action(self) -> None:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled = []
                self.submitted = []

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol, 222.0, 221.9, 222.1, "")

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("12")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-llm-new-buy", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-old-buy", "NVDA.US", "BUY", Decimal("10"), Decimal("221.0"), "SUBMITTED"),
            broker,
            (EngineState.FLAT, 0.0, None),
        )

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 221.88,
            "order_reason": "US price moved, refresh the resting order",
        })

        assert result == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-llm-new-buy",
            "action": "BUY",
            "replaced_order_id": "order-old-buy",
        }
        assert broker.cancelled == ["order-old-buy"]
        assert broker.submitted == [("NVDA.US", "BUY", Decimal("10"), Decimal("221.88"))]
        assert runner._trade_svc.has_pending_order is False
        assert runner.engine.state == EngineState.LONG

    def test_execute_llm_order_decision_stop_loss_sell_bypasses_profit_guard(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []

            def get_quote(self, symbol: str) -> Quote:
                return Quote(symbol, 215.0, 214.9, 215.1, "")

            def get_positions(self) -> list[Position]:
                return [Position("NVDA.US", "LONG", Decimal("8"), Decimal("220"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-stop-loss", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225, min_profit_amount=50)
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        result = runner.execute_llm_order_decision({
            "order_action": "STOP_LOSS_SELL_NOW",
            "order_price": 215.0,
            "order_reason": "支撑失效并开始崩盘",
        })

        assert result == {"executed": True, "status": "FILLED", "order_id": "order-stop-loss", "action": "SELL"}
        assert runner.broker.submitted == [("NVDA.US", "SELL", Decimal("8"), Decimal("215.00"))]
        assert runner.engine.state == EngineState.FLAT

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
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        assert runner._running is True
        runner.stop()
        assert runner._running is False

    def test_runner_double_start(self) -> None:
        runner = AppRunner()
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        first_thread = runner._thread
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        assert runner._thread is first_thread
        runner.stop()

    def test_initialize_runner_preserves_existing_loop_when_no_running_loop(self) -> None:
        runner = AppRunner()
        existing_loop = asyncio.new_event_loop()
        runner._loop = existing_loop

        class FakeService:
            def __init__(self, _db) -> None:
                pass

            def get_config(self):
                class Config:
                    symbol = ""
                    market = "US"
                    buy_low = 0.0
                    sell_high = 0.0
                    short_selling = False
                    max_daily_loss = 5000.0
                    max_consecutive_losses = 3

                return Config()

            def get_runtime_state(self):
                class State:
                    engine_state = "flat"
                    last_price = 0.0
                    last_trigger_price = 0.0
                    last_trigger_at = None
                    daily_pnl = 0.0
                    consecutive_losses = 0
                    kill_switch = False
                    paused = False

                return State()

        class FakeDb:
            def query(self, _model):
                class Query:
                    def filter(self, *_args):
                        return self

                    def order_by(self, *_args):
                        return self

                    def first(self):
                        return None

                return Query()

            def close(self) -> None:
                pass

        with (
            patch("app.runner.SessionLocal", lambda: FakeDb()),
            patch.object(runner._state_svc, "load") as load_state,
            patch.object(runner, "_load_credentials") as load_credentials,
            patch.object(runner, "_apply_credentials") as apply_credentials,
        ):
            load_credentials.return_value = object()
            runner._initialize_runner()

        assert runner._loop is existing_loop
        existing_loop.close()

    def test_initialize_runner_pauses_when_unresolved_live_order_exists(self) -> None:
        runner = AppRunner()

        class FakeService:
            def __init__(self, _db) -> None:
                pass

            def get_config(self):
                class Config:
                    symbol = "AAPL.US"
                    market = "US"
                    buy_low = 100.0
                    sell_high = 200.0
                    short_selling = False
                    max_daily_loss = 5000.0
                    max_consecutive_losses = 3

                return Config()

            def get_runtime_state(self):
                class State:
                    engine_state = "flat"
                    last_price = 0.0
                    last_trigger_price = 0.0
                    last_trigger_at = None
                    daily_pnl = 0.0
                    consecutive_losses = 0
                    kill_switch = False
                    paused = False

                return State()

        class FakeQuery:
            def filter(self, *_args):
                return self

            def order_by(self, *_args):
                return self

            def first(self):
                return SimpleNamespace(broker_order_id="order-live", status="SUBMITTED")

        class FakeDb:
            def query(self, _model):
                return FakeQuery()

            def close(self) -> None:
                pass

        with (
            patch("app.runner.SessionLocal", lambda: FakeDb()),
            patch.object(runner._state_svc, "load") as load_state,
            patch.object(runner, "_load_credentials") as load_credentials,
            patch.object(runner, "_apply_credentials") as apply_credentials,
        ):
            load_credentials.return_value = object()
            load_state.side_effect = lambda _db, engine, _risk: setattr(
                engine,
                "params",
                StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0),
            )
            runner._initialize_runner()

        assert runner.risk.paused is True

    def test_broadcast_status_no_connections(self) -> None:
        runner = AppRunner()
        runner._broadcast_status()

    def test_broadcast_status_includes_runner_running(self, monkeypatch) -> None:
        messages = []

        async def broadcast(message):
            messages.append(message)

        def run_coroutine_threadsafe(coro, _loop):
            asyncio.run(coro)
            return None

        class RunningLoop:
            def is_running(self) -> bool:
                return True

        monkeypatch.setattr(runner_module.manager, "broadcast", broadcast)
        monkeypatch.setattr(runner_module.asyncio, "run_coroutine_threadsafe", run_coroutine_threadsafe)
        runner = AppRunner()
        runner._running = True
        runner._thread = SimpleNamespace(is_alive=lambda: True)
        runner._loop = RunningLoop()

        runner._broadcast_status()

        assert messages[0]["runner_running"] is True

    def test_active_quote_refresh_fetches_quote_when_push_is_stale(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_quote(self, symbol: str) -> Quote:
                self.calls.append(symbol)
                return Quote(symbol=symbol, last_price=123.45, bid=123.4, ask=123.5, timestamp="")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner._active_quote_refresh_interval_seconds = 0.0

        runner._refresh_quote_if_stale()

        assert broker.calls == ["AAPL.US"]
        assert runner.engine.last_price == 123.45
        assert runner._last_quote_at > 0

    def test_active_quote_refresh_skips_when_push_is_fresh(self) -> None:
        class Broker:
            def get_quote(self, _symbol: str) -> Quote:
                raise AssertionError("fresh quote should not trigger active refresh")

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner._active_quote_refresh_interval_seconds = 60.0
        runner._last_quote_at = time.monotonic()

        runner._refresh_quote_if_stale()

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

    def test_on_quote_does_not_submit_add_on_buy_when_long_below_buy_low(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions: list[tuple[str, Decimal]] = []

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions.append((side, quantity))
                return OrderResult(f"order-{len(self.submissions)}", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.engine.last_trigger_at = None
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, ""))

        assert broker.submissions == []
        assert runner.engine.state == EngineState.LONG
        assert runner.engine.last_price == 99.0

    def test_submit_exception_pauses_runner_instead_of_immediate_retry(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                raise RuntimeError("broker rejected invalid price")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, "")
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.risk.paused is True
        assert runner.engine.state == EngineState.FLAT

    def test_rate_limit_submit_exception_marks_pause_auto_resumable(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                raise RuntimeError("429 too many requests")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, ""))

        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is True
        assert "429" in runner.risk.pause_reason

    def test_auto_resume_transient_pause_after_configured_delay(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        paused_at = now - timedelta(minutes=3, seconds=1)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=3,
        )
        runner.risk.pause("429 too many requests", auto_resumable=True, paused_at=paused_at)

        resumed = runner._auto_resume_pause_if_due(now=now)

        assert resumed is True
        assert runner.risk.paused is False

    def test_auto_resume_does_not_resume_manual_pause(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        paused_at = now - timedelta(minutes=10)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=3,
        )
        runner.risk.pause("manual", auto_resumable=False, paused_at=paused_at)

        resumed = runner._auto_resume_pause_if_due(now=now)

        assert resumed is False
        assert runner.risk.paused is True

    def test_paused_runner_updates_price_without_repeated_risk_notification(self) -> None:
        class Notifier:
            def __init__(self) -> None:
                self.risk_events: list[tuple[object, ...]] = []

            def notify_order(self, *args: object) -> bool:
                return True

            def notify_risk_event(self, *args: object) -> bool:
                self.risk_events.append(args)
                return True

        runner = AppRunner()
        notifier = Notifier()
        risk_events: list[str] = []
        runner._running = True
        runner.notifier = notifier
        runner._record_risk_event = lambda reason: risk_events.append(reason)
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.risk.pause("manual")

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, ""))
        runner._on_quote(Quote("AAPL.US", 98.5, 98.0, 99.0, ""))

        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 98.5
        assert notifier.risk_events == []
        assert risk_events == []

    def test_unprofitable_sell_skip_preserves_cooldown_to_avoid_position_polling_loop(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)

        class Broker:
            def __init__(self) -> None:
                self.position_checks = 0

            def get_positions(self) -> list[Position]:
                self.position_checks += 1
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("220"))]

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=220.15)
        runner.engine.state = EngineState.LONG
        runner.engine._cooldown_seconds = 60
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        quote = Quote("AAPL.US", 220.16, 220.15, 220.17, "")
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.position_checks == 1
        assert runner.engine.state == EngineState.LONG

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

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="FILLED",
                    executed_quantity=self.submitted_quantity,
                    executed_price=Decimal("201"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        updates: list[tuple[str, str, object]] = []
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, ""))

        assert order_status is not None
        assert order_status.status == "FILLED"
        assert broker.submitted_quantity == Decimal("5")
        assert runner.risk.daily_pnl == 255.0
        assert updates[-1][0] == "order-1"
        assert updates[-1][1] == "FILLED"
        assert updates[-1][2] is not None

    def test_execute_buy_returns_false_for_rejected_order(self) -> None:
        class Broker:
            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-rejected",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="REJECTED",
                )

        runner = AppRunner()
        runner.broker = Broker()
        runner.engine.params.market = "US"
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        order_status = self._execute_buy(runner, "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""))

        assert order_status is not None
        assert order_status.status == "REJECTED"

    @pytest.mark.parametrize("terminal_status", ["REJECTED", "CANCELLED"])
    def test_execute_sell_records_terminal_status_timestamp(self, terminal_status: str) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                assert order_id == "order-1"
                assert runner.risk.daily_pnl == 0.0
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status=terminal_status,
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 1

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, ""))

        assert order_status is not None
        assert order_status.status == terminal_status
        assert runner.risk.daily_pnl == 0.0
        assert updates
        assert updates[-1][0] == "order-1"
        assert updates[-1][1] == terminal_status
        assert updates[-1][2] is not None

    def test_execute_sell_without_fill_tracks_pending_without_pnl_or_filled_status(self) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, ""))

        assert order_status is not None
        assert order_status.status == "SUBMITTED"
        assert runner.risk.daily_pnl == 0.0
        assert runner._trade_svc._pending_order is not None
        assert runner._trade_svc._pending_order.broker_order_id == "order-1"
        assert all(status != "FILLED" for _order_id, status, _filled_at in updates)

    def test_live_submitted_timeout_keeps_trigger_state_and_skips_duplicate_order(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, "")
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is not None

    def test_partial_filled_timeout_keeps_pending_and_skips_duplicate_order(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="PARTIAL_FILLED",
                    executed_quantity=Decimal("1"),
                    executed_price=Decimal("99"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, "")
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is not None

    def test_pending_order_rejection_restores_snapshot_and_later_quote_can_retrigger(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0
                self.statuses = ["SUBMITTED", "REJECTED", "REJECTED"]

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id=f"order-{self.submissions}",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                status = self.statuses.pop(0)
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status=status,
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, "")
        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG

        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.engine.state == EngineState.FLAT
        assert runner._trade_svc._pending_order is None

        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.risk.paused is True

    def test_trigger_uses_symbol_snapshot_if_strategy_changes_before_submit(self) -> None:
        entered_first_broadcast = threading.Event()
        release_first_broadcast = threading.Event()
        broadcast_calls = 0

        class Broker:
            def __init__(self) -> None:
                self.submitted_symbols: list[str] = []

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted_symbols.append(symbol)
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="REJECTED",
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        def broadcast_status() -> None:
            nonlocal broadcast_calls
            broadcast_calls += 1
            if broadcast_calls == 1:
                entered_first_broadcast.set()
                release_first_broadcast.wait(timeout=2)

        runner._broadcast_status = broadcast_status
        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, ""),))
        thread.start()
        try:
            assert entered_first_broadcast.wait(timeout=1)
            runner.engine.params = StrategyParams(symbol="MSFT.US", buy_low=50.0, sell_high=80.0)
        finally:
            release_first_broadcast.set()
            thread.join(timeout=2)

        assert thread.is_alive() is False
        assert broker.submitted_symbols == ["AAPL.US"]

    def test_stop_does_not_close_broker_while_order_status_poll_is_running(self) -> None:
        entered_status_poll = threading.Event()
        release_status_poll = threading.Event()

        class Broker:
            def __init__(self) -> None:
                self.closed = False

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                entered_status_poll.set()
                release_status_poll.wait(timeout=2)
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="FILLED",
                    executed_quantity=Decimal("9"),
                    executed_price=Decimal("99"),
                )

            def close(self) -> None:
                self.closed = True

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 2

        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, ""),))
        thread.start()
        try:
            assert entered_status_poll.wait(timeout=1)
            runner.stop()
            assert broker.closed is False
        finally:
            release_status_poll.set()
            thread.join(timeout=2)

        assert thread.is_alive() is False
        assert broker.closed is True

    def test_pending_partial_cancel_accounts_fill_and_keeps_residual_position_state(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.statuses = [
                    SimpleNamespace(
                        broker_order_id="order-1",
                        status="PARTIAL_FILLED",
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("205"),
                    ),
                    SimpleNamespace(
                        broker_order_id="order-1",
                        status="CANCELLED",
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("205"),
                    ),
                ]

            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return self.statuses.pop(0)

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 201.0, 200.5, 201.5, "")
        runner._on_quote(quote)
        assert runner.engine.state == EngineState.FLAT
        assert runner._trade_svc._pending_order is not None

        runner._on_quote(quote)

        assert runner.risk.daily_pnl == 110.0
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is None

    def test_pending_order_reconcile_is_throttled_between_poll_intervals(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.status_checks = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                self.status_checks += 1
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 60
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, "")
        runner._on_quote(quote)
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.status_checks == 1

    def test_on_quote_does_not_hold_state_lock_while_polling_order_status(self) -> None:
        entered_status_poll = threading.Event()
        release_status_poll = threading.Event()

        class Broker:
            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                entered_status_poll.set()
                release_status_poll.wait(timeout=2)
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="FILLED",
                    executed_quantity=Decimal("9"),
                    executed_price=Decimal("99"),
                )

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 2

        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, ""),))
        thread.start()
        try:
            assert entered_status_poll.wait(timeout=1)
            acquired = runner._state_lock.acquire(timeout=0.1)
            if acquired:
                runner._state_lock.release()
            assert acquired is True
        finally:
            release_status_poll.set()
            thread.join(timeout=2)

        assert thread.is_alive() is False

    def test_execute_sell_rejected_after_submit_updates_status_without_pnl(self) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="REJECTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 1

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, ""))

        assert order_status is not None
        assert order_status.status == "REJECTED"
        assert runner.risk.daily_pnl == 0.0
        assert updates[-1][1] == "REJECTED"
