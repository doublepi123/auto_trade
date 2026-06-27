"""P281: sample uniqueness and event-overlap weights."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean


@dataclass(frozen=True)
class SampleUniquenessResult:
    n_bars: int
    concurrency: list[int]
    events: list[dict[str, Any]]
    average_uniqueness: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def sample_uniqueness_report(events: list[dict[str, Any]], *, time_decay: float = 1.0) -> SampleUniquenessResult:
    if not isinstance(events, list) or not events:
        raise ValueError("events must be non-empty")
    decay = _finite(time_decay, "time_decay")
    if decay < 0:
        raise ValueError("time_decay must be non-negative")
    parsed: list[tuple[str, int, int, float]] = []
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("events must contain dicts")
        start = event.get("start")
        end = event.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise ValueError("event ranges must satisfy 0 <= start <= end")
        if end > 10000:
            raise ValueError("event end must be <= 10000")
        parsed.append((str(event.get("id", i)), start, end, _finite(event.get("return", 0.0), "event return")))
    n_bars = max(end for _, _, end, _ in parsed) + 1
    concurrency = [0] * n_bars
    for _, start, end, _ in parsed:
        for idx in range(start, end + 1):
            concurrency[idx] += 1
    rows: list[dict[str, Any]] = []
    uniques: list[float] = []
    for event_id, start, end, ret in parsed:
        conc = [concurrency[idx] for idx in range(start, end + 1)]
        uniqueness = mean([1.0 / c for c in conc if c > 0])
        uniques.append(uniqueness)
        rows.append({"id": event_id, "uniqueness": uniqueness, "avg_concurrency": mean([float(c) for c in conc]), "weight": abs(ret) * (uniqueness ** decay)})
    return SampleUniquenessResult(n_bars, concurrency, rows, mean(uniques))


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


__all__ = ["SampleUniquenessResult", "sample_uniqueness_report"]
