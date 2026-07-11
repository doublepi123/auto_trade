from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

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
    allow_position_addons: bool = True
    stop_loss_pct: float = 0.0
    max_holding_minutes: int = 0
    entry_cutoff_minutes_before_close: int = 0
    flatten_minutes_before_close: int = 0


@dataclass
class TriggerResult:
    triggered: bool
    action: str = ""
    description: str = ""


@dataclass(frozen=True)
class EngineSnapshot:
    """Immutable snapshot of engine state for save/restore.

    Note: ``_last_trigger_monotonic`` is intentionally excluded to prevent
    restoring a stale cooldown timer, which would either suppress a
    legitimate re-trigger or, worse, create a tight retry loop when an
    unprofitable exit is skipped (see
    ``test_unprofitable_sell_skip_preserves_cooldown_to_avoid_position_polling_loop``).
    """

    state: EngineState
    last_trigger_price: float
    last_trigger_at: datetime | None


class StrategyEngine:
    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or StrategyParams()
        self.state: EngineState = EngineState.FLAT
        self.last_price: float = 0.0
        self.last_trigger_price: float = 0.0
        self.last_trigger_at: datetime | None = None
        self._last_trigger_monotonic: float = 0.0
        self._cooldown_seconds: int = settings.engine_cooldown_seconds
        self._lock = threading.Lock()

    def update_price(self, price: float) -> TriggerResult:
        with self._lock:
            return self._update_price_locked(price)

    def record_price(self, price: float) -> None:
        with self._lock:
            self.last_price = price

    def _update_price_locked(self, price: float) -> TriggerResult:
        if price <= 0:
            logger.warning("engine received non-positive price %s for %s", price, self.params.symbol)
            return TriggerResult(triggered=False)
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
            if self.params.allow_position_addons and price <= self.params.buy_low:
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, add-on buy LONG",
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
            # NOTE: SHORT 状态不追加做空 — 有意限制空头敞口,与 LONG 的 add-on 不对称

        return TriggerResult(triggered=False)

    def _mark_trigger(self, price: float) -> None:
        self.last_trigger_price = price
        self.last_trigger_at = datetime.now(timezone.utc)
        self._last_trigger_monotonic = time.monotonic()

    def _in_cooldown(self) -> bool:
        """Return True when the engine is still inside its post-trigger cooldown window.

        ``cooldown_seconds`` is the minimum gap between successive triggers.
        Setting it to ``0`` (or any non-positive value) disables the cooldown
        check entirely — triggers can fire back-to-back. This is the
        intended opt-out for high-frequency / test scenarios.
        """
        if self._cooldown_seconds <= 0:
            return False
        if self._last_trigger_monotonic <= 0:
            return False
        elapsed = time.monotonic() - self._last_trigger_monotonic
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

    # ------------------------------------------------------------------
    # Snapshot / restore / state-machine transitions (owned by engine)
    # ------------------------------------------------------------------

    def snapshot(self) -> EngineSnapshot:
        """Capture current mutable state as an immutable snapshot."""
        with self._lock:
            return EngineSnapshot(
                state=self.state,
                last_trigger_price=self.last_trigger_price,
                last_trigger_at=self.last_trigger_at,
            )

    def restore(self, snap: EngineSnapshot) -> None:
        """Restore engine state from a snapshot (full restore)."""
        with self._lock:
            self.state = snap.state
            self.last_trigger_price = snap.last_trigger_price
            self.last_trigger_at = snap.last_trigger_at

    def restore_preserving_trigger(self, snap: EngineSnapshot) -> None:
        """Restore only the ``state`` field, keeping trigger info unchanged.

        Used when an unprofitable exit is skipped — the cooldown must remain
        active to prevent a tight position-polling loop.
        """
        with self._lock:
            self.state = snap.state

    def transition_for_action(self, action: str) -> str:
        """Attempt a state-machine transition for *action*.

        Returns ``"OK"`` on success or an error status string:
        ``"INCOMPATIBLE_STATE"``, ``"SHORT_SELLING_DISABLED"``,
        ``"UNKNOWN_ACTION"``.
        """
        with self._lock:
            current = self.state
            if action == "BUY":
                if current == EngineState.LONG:
                    return "OK"
                if current != EngineState.FLAT:
                    return "INCOMPATIBLE_STATE"
                self.state = EngineState.LONG
                return "OK"
            if action == "SELL":
                if current != EngineState.LONG:
                    return "INCOMPATIBLE_STATE"
                self.state = EngineState.FLAT
                return "OK"
            if action == "SELL_SHORT":
                if not self.params.short_selling:
                    return "SHORT_SELLING_DISABLED"
                if current != EngineState.FLAT:
                    return "INCOMPATIBLE_STATE"
                self.state = EngineState.SHORT
                return "OK"
            if action == "BUY_TO_COVER":
                if current != EngineState.SHORT:
                    return "INCOMPATIBLE_STATE"
                self.state = EngineState.FLAT
                return "OK"
            return "UNKNOWN_ACTION"

    def to_dict(self) -> dict[str, Any]:
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
                "allow_position_addons": self.params.allow_position_addons,
                "stop_loss_pct": self.params.stop_loss_pct,
                "max_holding_minutes": self.params.max_holding_minutes,
                "entry_cutoff_minutes_before_close": self.params.entry_cutoff_minutes_before_close,
                "flatten_minutes_before_close": self.params.flatten_minutes_before_close,
            }
