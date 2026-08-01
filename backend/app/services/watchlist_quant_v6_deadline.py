from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class QuantV6EvaluationStoppedError(RuntimeError):
    """Base error for cooperative quant-v6 evaluation termination."""


class QuantV6EvaluationCancelledError(QuantV6EvaluationStoppedError):
    """Raised when shutdown or an operator cancels one evaluation tick."""


class QuantV6EvaluationDeadlineExceededError(
    QuantV6EvaluationStoppedError
):
    """Raised when one evaluation tick exhausts its wall-clock budget."""


class QuantV6EvaluationDeadline:
    """Per-tick cooperative cancellation and absolute monotonic deadline."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        cancel_event: threading.Event | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self._monotonic = monotonic
        self._cancel_event = cancel_event or threading.Event()
        self._deadline_at = monotonic() + float(timeout_seconds)
        self._forced_timeout = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def deadline_at(self) -> float:
        return self._deadline_at

    def cancel(self) -> None:
        self._cancel_event.set()

    def expire(self) -> None:
        """Force the timeout branch when an outer async timer wins."""
        self._forced_timeout.set()
        self._cancel_event.set()

    def is_stopped(self) -> bool:
        return (
            self._forced_timeout.is_set()
            or self._cancel_event.is_set()
            or self._monotonic() >= self._deadline_at
        )

    def checkpoint(self) -> None:
        now = self._monotonic()
        if self._forced_timeout.is_set() or now >= self._deadline_at:
            raise QuantV6EvaluationDeadlineExceededError(
                "quant-v6 evaluation deadline exceeded"
            )
        if self._cancel_event.is_set():
            raise QuantV6EvaluationCancelledError(
                "quant-v6 evaluation was cancelled"
            )

    def remaining_seconds(self) -> float:
        self.checkpoint()
        remaining = self._deadline_at - self._monotonic()
        if remaining <= 0:
            raise QuantV6EvaluationDeadlineExceededError(
                "quant-v6 evaluation deadline exceeded"
            )
        return remaining

    def bounded_timeout(self, maximum_seconds: float) -> float:
        if (
            isinstance(maximum_seconds, bool)
            or not isinstance(maximum_seconds, (int, float))
            or not math.isfinite(float(maximum_seconds))
            or float(maximum_seconds) <= 0
        ):
            raise ValueError("maximum_seconds must be finite and positive")
        return min(float(maximum_seconds), self.remaining_seconds())

    def wait(self, delay_seconds: float) -> None:
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, (int, float))
            or not math.isfinite(float(delay_seconds))
            or float(delay_seconds) < 0
        ):
            raise ValueError("delay_seconds must be finite and non-negative")
        remaining = self.remaining_seconds()
        wait_seconds = min(float(delay_seconds), remaining)
        if self._cancel_event.wait(wait_seconds):
            self.checkpoint()
        self.checkpoint()


__all__ = [
    "QuantV6EvaluationCancelledError",
    "QuantV6EvaluationDeadline",
    "QuantV6EvaluationDeadlineExceededError",
    "QuantV6EvaluationStoppedError",
]
