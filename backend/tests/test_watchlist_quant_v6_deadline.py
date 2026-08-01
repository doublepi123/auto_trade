from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationCancelledError,
    QuantV6EvaluationDeadline,
    QuantV6EvaluationDeadlineExceededError,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_deadline_exposes_bounded_remaining_budget() -> None:
    clock = _Clock()
    deadline = QuantV6EvaluationDeadline(30, monotonic=clock)

    assert deadline.deadline_at == 130.0
    assert deadline.remaining_seconds() == 30.0
    assert deadline.bounded_timeout(10) == 10.0

    clock.value = 125.0
    assert deadline.remaining_seconds() == 5.0
    assert deadline.bounded_timeout(10) == 5.0


def test_deadline_distinguishes_cancel_from_timeout() -> None:
    clock = _Clock()
    cancelled = QuantV6EvaluationDeadline(30, monotonic=clock)
    cancelled.cancel()
    with pytest.raises(QuantV6EvaluationCancelledError):
        cancelled.checkpoint()

    expired = QuantV6EvaluationDeadline(30, monotonic=clock)
    clock.value = 130.0
    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        expired.checkpoint()


def test_outer_timer_can_force_deadline_branch() -> None:
    clock = _Clock()
    deadline = QuantV6EvaluationDeadline(30, monotonic=clock)

    deadline.expire()

    assert deadline.cancel_event.is_set()
    assert deadline.is_stopped() is True
    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        deadline.checkpoint()


def test_forced_timeout_takes_priority_over_shutdown_cancel() -> None:
    deadline = QuantV6EvaluationDeadline(30)
    deadline.cancel()

    with pytest.raises(QuantV6EvaluationCancelledError):
        deadline.checkpoint()

    deadline.expire()

    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        deadline.checkpoint()


def test_wait_does_not_sleep_beyond_remaining_budget() -> None:
    deadline = QuantV6EvaluationDeadline(0.02)
    began_at = time.monotonic()

    with pytest.raises(QuantV6EvaluationDeadlineExceededError):
        deadline.wait(1)

    assert time.monotonic() - began_at < 0.5


def test_existing_cancel_event_is_shared_with_worker() -> None:
    event = threading.Event()
    deadline = QuantV6EvaluationDeadline(30, cancel_event=event)

    event.set()

    with pytest.raises(QuantV6EvaluationCancelledError):
        deadline.remaining_seconds()


@pytest.mark.parametrize(
    "value",
    [True, 0, -1, float("inf"), float("nan"), "30"],
)
def test_deadline_rejects_invalid_timeout(value: Any) -> None:
    with pytest.raises(ValueError):
        QuantV6EvaluationDeadline(value)
