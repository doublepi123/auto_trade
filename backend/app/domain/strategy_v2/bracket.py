from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.core.market_calendar import is_closing_window
from app.domain.strategy_v2.features import StrategyBar


class BracketAction(str, Enum):
    HOLD = "HOLD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class BracketConfig:
    stop_loss_pct: float
    profit_target_pct: float
    slippage_bps: float
    flatten_minutes_before_close: int
    vwap_target_cap_bps: float | None = None

    def __post_init__(self) -> None:
        values = (
            self.stop_loss_pct,
            self.profit_target_pct,
            self.slippage_bps,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("bracket thresholds must be finite")
        if not 0 < self.stop_loss_pct <= 0.75:
            raise ValueError("stop_loss_pct must be in (0, 0.75]")
        if not 0 < self.profit_target_pct <= 5.0:
            raise ValueError("profit_target_pct must be in (0, 5]")
        if not 0 <= self.slippage_bps <= 50:
            raise ValueError("slippage_bps must be in [0, 50]")
        if not 1 <= self.flatten_minutes_before_close <= 180:
            raise ValueError(
                "flatten_minutes_before_close must be in [1, 180]"
            )
        if self.vwap_target_cap_bps is not None:
            if (
                not math.isfinite(self.vwap_target_cap_bps)
                or not 0 < self.vwap_target_cap_bps <= 500
            ):
                raise ValueError("vwap_target_cap_bps must be in (0, 500]")
            if self.vwap_target_cap_bps + 1e-12 < (
                self.profit_target_pct * 100.0
            ):
                raise ValueError(
                    "vwap_target_cap_bps must not be below the profit target"
                )

    def stop_price(self, entry_price: float) -> float:
        return entry_price * (1.0 - self.stop_loss_pct / 100.0)

    def target_price(
        self,
        entry_price: float,
        signal_vwap: float,
    ) -> float:
        vwap_target = signal_vwap
        if self.vwap_target_cap_bps is not None:
            vwap_target = min(
                vwap_target,
                entry_price
                * (1.0 + self.vwap_target_cap_bps / 10_000.0),
            )
        return max(
            entry_price * (1.0 + self.profit_target_pct / 100.0),
            vwap_target,
        )


@dataclass(frozen=True)
class BracketDecision:
    action: BracketAction
    reason: str
    event_at: datetime
    exit_price: float | None
    stop_price: float
    target_price: float


def evaluate_bracket_bar(
    *,
    config: BracketConfig,
    market: str,
    entry_price: float,
    signal_vwap: float,
    holding_deadline: datetime,
    bar: StrategyBar,
) -> BracketDecision:
    """Evaluate one completed long-position bar with conservative OHLC order."""
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError("entry_price must be finite and positive")
    if not math.isfinite(signal_vwap) or signal_vwap <= 0:
        raise ValueError("signal_vwap must be finite and positive")
    if holding_deadline.tzinfo is None:
        raise ValueError("holding_deadline must be timezone-aware")

    stop_price = config.stop_price(entry_price)
    target_price = config.target_price(entry_price, signal_vwap)
    raw_exit_price: float | None = None
    reason = "BRACKET_OPEN"

    if bar.low <= stop_price:
        reason = "PRICE_STOP"
        raw_exit_price = min(bar.open, stop_price)
    elif is_closing_window(
        market,
        config.flatten_minutes_before_close,
        bar.timestamp,
    ):
        reason = "EOD_FLATTEN"
        raw_exit_price = bar.open
    elif bar.high >= target_price:
        reason = "PROFIT_TARGET"
        raw_exit_price = max(bar.open, target_price)
    elif bar.timestamp >= holding_deadline:
        reason = "MAX_HOLD"
        raw_exit_price = bar.open

    if raw_exit_price is None:
        return BracketDecision(
            action=BracketAction.HOLD,
            reason=reason,
            event_at=bar.timestamp,
            exit_price=None,
            stop_price=stop_price,
            target_price=target_price,
        )
    return BracketDecision(
        action=BracketAction.EXIT,
        reason=reason,
        event_at=bar.timestamp,
        exit_price=raw_exit_price * (
            1.0 - config.slippage_bps / 10_000.0
        ),
        stop_price=stop_price,
        target_price=target_price,
    )
