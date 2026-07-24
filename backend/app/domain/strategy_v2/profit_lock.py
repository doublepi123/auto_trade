from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.domain.strategy_v2.features import StrategyBar


class ProfitLockAction(str, Enum):
    HOLD = "HOLD"
    ACTIVATE = "ACTIVATE"
    EXIT = "EXIT"


@dataclass(frozen=True)
class ProfitLockConfig:
    activation_pct: float
    locked_profit_pct: float
    slippage_bps: float

    def __post_init__(self) -> None:
        values = (
            self.activation_pct,
            self.locked_profit_pct,
            self.slippage_bps,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("profit-lock thresholds must be finite")
        if self.activation_pct <= 0:
            raise ValueError("activation_pct must be positive")
        if self.locked_profit_pct <= 0:
            raise ValueError("locked_profit_pct must be positive")
        if self.locked_profit_pct >= self.activation_pct:
            raise ValueError("locked_profit_pct must be below activation_pct")
        if not 0 <= self.slippage_bps <= 50:
            raise ValueError("slippage_bps must be in [0, 50]")

    def activation_price(self, entry_price: float) -> float:
        return entry_price * (1.0 + self.activation_pct / 100.0)

    def floor_price(self, entry_price: float) -> float:
        return entry_price * (1.0 + self.locked_profit_pct / 100.0)


@dataclass(frozen=True)
class ProfitLockDecision:
    action: ProfitLockAction
    reason: str
    event_at: datetime
    effective_at: datetime | None = None
    exit_price: float | None = None
    activation_price: float | None = None
    floor_price: float | None = None


def evaluate_profit_lock_bar(
    *,
    config: ProfitLockConfig,
    entry_price: float,
    bar: StrategyBar,
    armed_before_bar: bool,
) -> ProfitLockDecision:
    """Evaluate one completed long-position bar without assuming intrabar order.

    A lock first observed on this bar becomes effective only on the next bar.
    This prevents a bar that traded both above the activation level and below
    the floor from being scored with an unknowable favorable path.
    """
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError("entry_price must be finite and positive")
    activation_price = config.activation_price(entry_price)
    floor_price = config.floor_price(entry_price)

    if armed_before_bar and bar.low <= floor_price:
        raw_exit_price = min(bar.open, floor_price)
        exit_price = raw_exit_price * (1.0 - config.slippage_bps / 10_000.0)
        return ProfitLockDecision(
            action=ProfitLockAction.EXIT,
            reason="PROFIT_LOCK",
            event_at=bar.timestamp,
            exit_price=exit_price,
            activation_price=activation_price,
            floor_price=floor_price,
        )

    if not armed_before_bar and bar.high >= activation_price:
        return ProfitLockDecision(
            action=ProfitLockAction.ACTIVATE,
            reason="PROFIT_LOCK_ACTIVATED",
            event_at=bar.timestamp,
            effective_at=bar.end_at,
            activation_price=activation_price,
            floor_price=floor_price,
        )

    return ProfitLockDecision(
        action=ProfitLockAction.HOLD,
        reason="PROFIT_LOCK_ARMED" if armed_before_bar else "PROFIT_LOCK_INACTIVE",
        event_at=bar.timestamp,
        activation_price=activation_price,
        floor_price=floor_price,
    )
