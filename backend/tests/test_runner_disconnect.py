# pyright: reportPrivateUsage=false
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from app.core.engine import StrategyEngine, StrategyParams
from app.runner import AppRunner


def _runner_for_disconnect_tests() -> tuple[AppRunner, MagicMock, MagicMock]:
    runner = AppRunner.__new__(AppRunner)
    broker_mock = MagicMock()
    runner.broker = broker_mock
    runner.engine = StrategyEngine()
    runner.engine.params = StrategyParams(symbol="AAPL.US")
    runner._state_lock = threading.RLock()
    runner._symbol_runtimes = {}
    runner._quotes_subscribed = True
    runner._last_push_quote_at = 1.0
    record_mock = MagicMock()
    runner._audit = MagicMock(record=record_mock)
    runner._disconnect_retry_count = 0
    runner._trigger_in_flight = False
    return runner, record_mock, broker_mock


def test_on_disconnect_writes_audit_and_marks_unsubscribed() -> None:
    runner, record_mock, _broker_mock = _runner_for_disconnect_tests()

    runner._on_disconnect("network_drop")

    record_mock.assert_called_once()
    assert record_mock.call_args.args[0] == "BROKER_DISCONNECT"
    assert runner._quotes_subscribed is False
    assert runner._disconnect_retry_count == 1


def test_on_disconnect_increments_retry_count_each_call() -> None:
    runner, _record_mock, _broker_mock = _runner_for_disconnect_tests()

    runner._on_disconnect("a")
    runner._on_disconnect("b")
    runner._on_disconnect("c")
    runner._on_disconnect("d")

    assert runner._disconnect_retry_count == 4


def test_on_disconnect_writes_retry_exhausted_audit_at_threshold() -> None:
    runner, record_mock, _broker_mock = _runner_for_disconnect_tests()
    runner._disconnect_retry_count = 2

    runner._on_disconnect("c")

    actions = [call.args[0] for call in record_mock.call_args_list]
    assert "BROKER_RETRY_EXHAUSTED" in actions


def test_on_resubscribe_if_needed_resubscribes_when_unsubscribed() -> None:
    runner, _record_mock, broker_mock = _runner_for_disconnect_tests()
    runner._quotes_subscribed = False
    runner._disconnect_retry_count = 1
    runner._last_push_quote_at = 0.0

    runner._on_resubscribe_if_needed()

    broker_mock.unsubscribe_quotes.assert_called_once()
    broker_mock.subscribe_quotes_batch.assert_called_once_with(
        ["AAPL.US"],
        runner._on_quote,
    )
    assert runner._quotes_subscribed is True
    assert runner._disconnect_retry_count == 0
    assert runner._last_push_quote_at > 0
