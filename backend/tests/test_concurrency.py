# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import threading
import time
from decimal import Decimal

from app.core.broker import OrderResult, Position, Quote
from app.core.engine import StrategyParams
from app.core.risk import RiskConfig, RiskController
from app.runner import AppRunner


class _NoopNotifier:
    def notify_order(self, *_args: object) -> bool:
        return True

    def notify_risk_event(self, *_args: object) -> bool:
        return True


class _ConcurrentBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.submitted: list[tuple[str, str, Decimal, Decimal]] = []

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol, 100.0, 99.9, 100.1, "")

    def estimate_margin_max_quantity(
        self,
        _symbol: str,
        _side: str,
        _price: Decimal,
        _currency: str | None = None,
    ) -> Decimal:
        return Decimal("10")

    def get_positions(self) -> list[Position]:
        return [Position("AAPL.US", "LONG", Decimal("10"), Decimal("99"), available_quantity=Decimal("10"))]

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        with self._lock:
            self.submitted.append((symbol, side, quantity, price))
        return OrderResult(f"order-{len(self.submitted)}", symbol, side, quantity, price, "FILLED")


def test_app_runner_lock_prevents_deadlock_in_concurrent_buy_sell_decisions() -> None:
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=99, sell_high=101)
    runner.broker = _ConcurrentBroker()
    runner.notifier = _NoopNotifier()
    runner._trade_svc._record_order = lambda *args: None
    runner._trade_svc._update_order_status = lambda *args, **kwargs: None
    runner._trade_svc._record_risk_event = lambda reason: None
    runner._trade_svc._record_order_skipped = lambda *args: None
    runner._trade_svc._persist_entry = lambda *args: None
    runner._record_order_skipped = lambda *args: None
    barrier = threading.Barrier(2)
    results: dict[str, list[dict[str, object]]] = {"buy": [], "sell": []}
    errors: list[BaseException] = []

    def run_decisions(name: str, decision: dict[str, object]) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(25):
                results[name].append(runner.execute_llm_order_decision(decision))
                time.sleep(0.001)
        except BaseException as exc:
            errors.append(exc)

    buy_thread = threading.Thread(target=run_decisions, args=("buy", {"order_action": "BUY_NOW", "order_price": 99.0}))
    sell_thread = threading.Thread(target=run_decisions, args=("sell", {"order_action": "SELL_NOW", "order_price": 101.0}))

    buy_thread.start()
    sell_thread.start()
    buy_thread.join(timeout=5)
    sell_thread.join(timeout=5)

    assert not buy_thread.is_alive()
    assert not sell_thread.is_alive()
    assert errors == []
    assert len(results["buy"]) == 25
    assert len(results["sell"]) == 25


def test_risk_controller_pause_is_thread_safe() -> None:
    risk = RiskController(RiskConfig(max_daily_loss=1_000_000, max_consecutive_losses=1000))
    barrier = threading.Barrier(5)
    errors: list[BaseException] = []

    def pause(reason: str) -> None:
        try:
            barrier.wait(timeout=5)
            risk.pause(reason=reason)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=pause, args=(f"reason-{index}",)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert risk.paused is True
    assert risk.pause_reason in {f"reason-{index}" for index in range(5)}
