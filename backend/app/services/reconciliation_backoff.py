from __future__ import annotations

import hashlib
from typing import Final

_MIN_JITTER_FACTOR: Final = 0.8
_JITTER_FACTOR_RANGE: Final = 0.4


class ReconciliationBackoff:
    def __init__(self, base_seconds: float, cap_seconds: float) -> None:
        self._base_seconds = base_seconds
        self._cap_seconds = cap_seconds
        self._failure_count = 0
        self._next_attempt_at = 0.0

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def next_attempt_at(self) -> float:
        return self._next_attempt_at

    def allows_attempt(self, now: float) -> bool:
        return now >= self._next_attempt_at

    def record_failure(self, now: float, failure_key: str) -> float:
        self._failure_count += 1
        exponential_delay = min(
            self._cap_seconds,
            self._base_seconds * (2 ** (self._failure_count - 1)),
        )
        digest = hashlib.sha256(
            f"{failure_key}:{self._failure_count}".encode("utf-8")
        ).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
        jitter_factor = _MIN_JITTER_FACTOR + _JITTER_FACTOR_RANGE * fraction
        delay = min(self._cap_seconds, exponential_delay * jitter_factor)
        self._next_attempt_at = now + delay
        return delay

    def record_success(self, now: float) -> None:
        self._failure_count = 0
        self._next_attempt_at = now + self._base_seconds
