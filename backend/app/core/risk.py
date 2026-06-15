from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional


@dataclass
class RiskConfig:
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 3

    def __post_init__(self) -> None:
        # ``is not None`` guard is required: runtime_state_service.load() may
        # pass None when the DB column is NULL — this is NOT dead code.
        if self.max_daily_loss is not None and self.max_daily_loss < 0:
            raise ValueError("max_daily_loss must be non-negative")


@dataclass
class RiskResult:
    approved: bool
    reason: str = ""


TradeDayProvider = Callable[[], date]


class RiskController:
    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        trade_day_provider: TradeDayProvider | None = None,
    ) -> None:
        self.config = config or RiskConfig()
        self._trade_day_provider: TradeDayProvider = trade_day_provider or _current_risk_day
        self.daily_pnl: float = 0.0
        self._today: date = self._trade_day_provider()
        self.consecutive_losses: int = 0
        self.kill_switch: bool = False
        self.paused: bool = False
        self._pause_reason: str = ""
        self._paused_at: datetime | None = None
        self._pause_auto_resumable: bool = False
        self._kill_switch_reason: str = ""
        self._lock = threading.Lock()

    def set_trade_day_provider(self, provider: TradeDayProvider) -> None:
        with self._lock:
            self._trade_day_provider = provider
            self._today = provider()

    def check(self) -> RiskResult:
        with self._lock:
            if self.kill_switch:
                return RiskResult(approved=False, reason="kill switch is active")
            if self.paused:
                reason = "trading is paused"
                if self._pause_reason:
                    reason = f"{reason}: {self._pause_reason}"
                return RiskResult(approved=False, reason=reason)
            # Day rollover is a mutating operation (resets daily_pnl and
            # consecutive_losses) so it is performed explicitly here rather
            # than hidden inside the read-only limit check.
            self._maybe_rollover_day()
            return self._check_limits()

    def reset_consecutive_losses(self) -> None:
        with self._lock:
            self.consecutive_losses = 0

    def _maybe_rollover_day(self) -> None:
        """Reset daily PnL and consecutive losses when the trade day changes.

        This is a **mutating** operation — it must be called explicitly so
        callers are aware that ``check()`` can reset state as a side effect.
        """
        today = self._trade_day_provider()
        if today != self._today:
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self._today = today

    def _check_limits(self) -> RiskResult:
        if self.config.max_daily_loss < 0:
            raise ValueError("max_daily_loss must be non-negative")

        # max_daily_loss == 0 means "no limit" (disabled); only enforce when > 0.
        # Without this guard, daily_pnl <= -0 is always True when daily_pnl == 0,
        # which would permanently block trading.
        if self.config.max_daily_loss > 0 and self.daily_pnl <= -self.config.max_daily_loss:
            return RiskResult(approved=False, reason=f"daily loss limit reached: {self.daily_pnl}")

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return RiskResult(approved=False, reason=f"max consecutive losses reached: {self.consecutive_losses}")

        return RiskResult(approved=True)

    def record_trade(self, pnl: float) -> None:
        with self._lock:
            today = self._trade_day_provider()
            if today != self._today:
                self.daily_pnl = 0.0
                self.consecutive_losses = 0
                self._today = today

            self.daily_pnl += pnl
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

    def replace_daily_pnl(self, daily_pnl: float, consecutive_losses: int, pnl_date: date | None = None) -> None:
        with self._lock:
            self.daily_pnl = daily_pnl
            self.consecutive_losses = max(0, consecutive_losses)
            self._today = pnl_date or self._trade_day_provider()

    def pause(
        self,
        reason: str = "manual",
        *,
        auto_resumable: bool = False,
        paused_at: datetime | None = None,
    ) -> None:
        with self._lock:
            self.paused = True
            self._pause_reason = reason
            self._paused_at = paused_at or datetime.now(timezone.utc)
            self._pause_auto_resumable = auto_resumable

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            self._pause_reason = ""
            self._paused_at = None
            self._pause_auto_resumable = False

    def restore_pause(
        self,
        paused: bool,
        reason: str = "",
        paused_at: datetime | None = None,
        auto_resumable: bool = False,
    ) -> None:
        with self._lock:
            self.paused = paused
            self._pause_reason = reason if paused else ""
            self._paused_at = paused_at if paused else None
            self._pause_auto_resumable = auto_resumable if paused else False

    def enable_kill_switch(self, reason: str = "manual") -> None:
        with self._lock:
            self.kill_switch = True
            self._kill_switch_reason = reason

    def disable_kill_switch(self) -> None:
        with self._lock:
            self.kill_switch = False
            self._kill_switch_reason = ""

    def begin_day(self, persisted_date: Optional[date] = None) -> None:
        """Reset daily P&L and consecutive losses if the day has changed.

        ``persisted_date`` is the last date the state was saved, used when
        loading from DB to detect a day boundary that occurred while the
        process was not running.
        """
        with self._lock:
            if persisted_date is not None:
                self._today = persisted_date
            today = self._trade_day_provider()
            if today != self._today:
                self.daily_pnl = 0.0
                self.consecutive_losses = 0
                self._today = today

    @property
    def daily_pnl_date(self) -> date:
        with self._lock:
            return self._today

    @property
    def pause_reason(self) -> str:
        with self._lock:
            return self._pause_reason

    @property
    def paused_at(self) -> datetime | None:
        with self._lock:
            return self._paused_at

    @property
    def pause_auto_resumable(self) -> bool:
        with self._lock:
            return self._pause_auto_resumable


def _current_risk_day() -> date:
    return datetime.now(timezone.utc).date()
