from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.platform.indicators import IndicatorService


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StrategyContext:
    symbol: str
    positions: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default_factory=lambda: _utc_clock)
    indicators: IndicatorService | None = None
