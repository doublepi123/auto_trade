from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Protocol, Self


class QuantV6StopEvent(Protocol):
    """Minimal event contract shared by thread and spawn-process deadlines."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


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
        cancel_event: QuantV6StopEvent | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        _deadline_at: float | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if _deadline_at is not None and (
            isinstance(_deadline_at, bool)
            or not isinstance(_deadline_at, (int, float))
            or not math.isfinite(float(_deadline_at))
        ):
            raise ValueError("deadline_at must be finite")
        self._monotonic = monotonic
        self._cancel_event = cancel_event or threading.Event()
        self._deadline_at = (
            monotonic() + float(timeout_seconds)
            if _deadline_at is None
            else float(_deadline_at)
        )
        self._forced_timeout = threading.Event()

    @classmethod
    def from_deadline_at(
        cls,
        deadline_at: float,
        *,
        cancel_event: QuantV6StopEvent,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Self:
        """Build a child view without granting fresh time after spawn/import."""
        if (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(float(deadline_at))
        ):
            raise ValueError("deadline_at must be finite")
        return cls(
            1.0,
            cancel_event=cancel_event,
            monotonic=monotonic,
            _deadline_at=float(deadline_at),
        )

    @property
    def cancel_event(self) -> QuantV6StopEvent:
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
    "QuantV6StopEvent",
]
