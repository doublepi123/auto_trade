from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class StrategyContext:
    symbol: str
    positions: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default_factory=lambda: __import__("datetime").datetime.utcnow)
