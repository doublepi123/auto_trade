from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass
class StrategyContext:
    symbol: str
    positions: dict[str, dict[str, Any]]
    params: dict[str, Any]
    clock: Callable[[], datetime]
