from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class RiskConfig:
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 3


@dataclass
class RiskResult:
    approved: bool
    reason: str = ""


class RiskController:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.daily_pnl: float = 0.0
        self._today: date = date.today()
        self.consecutive_losses: int = 0
        self.kill_switch: bool = False
        self.paused: bool = False
        self._pause_reason: str = ""
        self._kill_switch_reason: str = ""

    def check(self) -> RiskResult:
        if self.kill_switch:
            return RiskResult(approved=False, reason="kill switch is active")
        if self.paused:
            return RiskResult(approved=False, reason="trading is paused")
        return self._check_limits()

    def reset_consecutive_losses(self) -> None:
        """Public method to reset the consecutive losses counter."""
        self.consecutive_losses = 0

    def _check_limits(self) -> RiskResult:
        today = date.today()
        if today != self._today:
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self._today = today

        if self.daily_pnl <= -abs(self.config.max_daily_loss):
            return RiskResult(approved=False, reason=f"daily loss limit reached: {self.daily_pnl}")

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return RiskResult(approved=False, reason=f"max consecutive losses reached: {self.consecutive_losses}")

        return RiskResult(approved=True)

    def record_trade(self, pnl: float) -> None:
        today = date.today()
        if today != self._today:
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self._today = today

        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def pause(self, reason: str = "manual") -> None:
        self.paused = True
        self._pause_reason = reason

    def resume(self) -> None:
        self.paused = False
        self._pause_reason = ""

    def enable_kill_switch(self, reason: str = "manual") -> None:
        self.kill_switch = True
        self._kill_switch_reason = reason

    def disable_kill_switch(self) -> None:
        self.kill_switch = False
        self._kill_switch_reason = ""
