from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.domain.strategy_v2.features import StrategyBar


class TimeExitAction(str, Enum):
    HOLD = "HOLD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class TimeExitConfig:
    max_holding_minutes: int
    slippage_bps: float

    def __post_init__(self) -> None:
        if not 1 <= self.max_holding_minutes <= 1_440:
            raise ValueError("max_holding_minutes must be in [1, 1440]")
        if not math.isfinite(self.slippage_bps):
            raise ValueError("slippage_bps must be finite")
        if not 0 <= self.slippage_bps <= 50:
            raise ValueError("slippage_bps must be in [0, 50]")


@dataclass(frozen=True)
class TimeExitDecision:
    action: TimeExitAction
    reason: str
    event_at: datetime
    exit_price: float | None
    holding_deadline: datetime


def evaluate_time_exit_bar(
    *,
    config: TimeExitConfig,
    entry_at: datetime,
    bar: StrategyBar,
) -> TimeExitDecision:
    """Exit a long position at the first eligible bar open after its TTL."""
    if entry_at.tzinfo is None:
        raise ValueError("entry_at must be timezone-aware")
    normalized_entry = entry_at.astimezone(timezone.utc)
    deadline = normalized_entry + timedelta(
        minutes=config.max_holding_minutes
    )
    if bar.timestamp < deadline:
        return TimeExitDecision(
            action=TimeExitAction.HOLD,
            reason="TIME_EXIT_OPEN",
            event_at=bar.timestamp,
            exit_price=None,
            holding_deadline=deadline,
        )
    return TimeExitDecision(
        action=TimeExitAction.EXIT,
        reason="TIME_STOP",
        event_at=bar.timestamp,
        exit_price=bar.open * (1.0 - config.slippage_bps / 10_000.0),
        holding_deadline=deadline,
    )
