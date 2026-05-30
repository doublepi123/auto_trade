from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.config import settings

logger = logging.getLogger("auto_trade.engine")


class EngineState(str, Enum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


@dataclass
class StrategyParams:
    symbol: str = ""
    market: str = "US"
    buy_low: float = 0.0
    sell_high: float = 0.0
    short_selling: bool = False
    min_profit_amount: float = 0.0
    auto_resume_minutes: int = 3
    fee_rate_us: float = 0.0005
    fee_rate_hk: float = 0.003
    min_repricing_pct: float = 0.003
    llm_action_cooldown_seconds: int = 60


@dataclass
class TriggerResult:
    triggered: bool
    action: str = ""
    description: str = ""


class StrategyEngine:
    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or StrategyParams()
        self.state: EngineState = EngineState.FLAT
        self.last_price: float = 0.0
        self.last_trigger_price: float = 0.0
        self.last_trigger_at: Optional[datetime] = None
        self._cooldown_seconds: int = settings.engine_cooldown_seconds
        self._lock = threading.Lock()

    def update_price(self, price: float) -> TriggerResult:
        with self._lock:
            return self._update_price_locked(price)

    def record_price(self, price: float) -> None:
        with self._lock:
            self.last_price = price

    def _update_price_locked(self, price: float) -> TriggerResult:
        self.last_price = price

        if not self.params.symbol or self.params.buy_low <= 0 or self.params.sell_high <= 0 or self.params.buy_low >= self.params.sell_high:
            return TriggerResult(triggered=False)

        if self._in_cooldown():
            return TriggerResult(triggered=False)

        if self.state == EngineState.FLAT:
            if price <= self.params.buy_low:
                self.state = EngineState.LONG
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, go LONG",
                )
            if self.params.short_selling and price >= self.params.sell_high:
                self.state = EngineState.SHORT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="SELL_SHORT",
                    description=f"Price {price} >= sell_high {self.params.sell_high}, go SHORT",
                )

        elif self.state == EngineState.LONG:
            if price >= self.params.sell_high:
                self.state = EngineState.FLAT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="SELL",
                    description=f"Price {price} >= sell_high {self.params.sell_high}, sell LONG",
                )

        elif self.state == EngineState.SHORT:
            if price <= self.params.buy_low:
                self.state = EngineState.FLAT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY_TO_COVER",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, cover SHORT",
                )

        return TriggerResult(triggered=False)

    def _mark_trigger(self, price: float) -> None:
        self.last_trigger_price = price
        self.last_trigger_at = datetime.now(timezone.utc)

    def _in_cooldown(self) -> bool:
        if self.last_trigger_at is None:
            return False
        now = datetime.now(timezone.utc)
        last = self.last_trigger_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        return elapsed < self._cooldown_seconds

    def sync_state(self, has_long_position: bool, has_short_position: bool) -> None:
        with self._lock:
            if has_long_position and has_short_position:
                logger.warning("both long and short positions detected; defaulting to LONG")
            if has_long_position:
                self.state = EngineState.LONG
            elif has_short_position:
                self.state = EngineState.SHORT
            else:
                self.state = EngineState.FLAT

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "state": self.state.value,
                "last_price": self.last_price,
                "last_trigger_price": self.last_trigger_price,
                "last_trigger_at": self.last_trigger_at.isoformat() if self.last_trigger_at else None,
                "symbol": self.params.symbol,
                "buy_low": self.params.buy_low,
                "sell_high": self.params.sell_high,
                "short_selling": self.params.short_selling,
                "min_profit_amount": self.params.min_profit_amount,
                "auto_resume_minutes": self.params.auto_resume_minutes,
            }
