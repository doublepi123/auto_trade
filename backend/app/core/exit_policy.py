from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class ReductionCause(str, Enum):
    DAILY_LOSS = "DAILY_LOSS"
    PRICE_STOP = "PRICE_STOP"
    EOD_FLATTEN = "EOD_FLATTEN"
    TIME_STOP = "TIME_STOP"


@dataclass(frozen=True)
class ExitPolicyConfig:
    stop_loss_pct: float
    max_holding_minutes: int


@dataclass(frozen=True)
class PositionExitContext:
    symbol: str
    side: str
    quantity: float
    avg_entry_price: float
    opened_at: datetime | None


@dataclass(frozen=True)
class ExitQuote:
    last: float
    bid: float
    ask: float


@dataclass(frozen=True)
class ReductionDecision:
    action: str
    cause: ReductionCause
    reason: str
    trigger_price: float
    threshold_price: float | None


def evaluate_exit_policy(
    *,
    config: ExitPolicyConfig,
    position: PositionExitContext,
    quote: ExitQuote,
    now: datetime,
    in_flatten_window: bool,
    combined_daily_pnl: float,
    max_daily_loss: float,
) -> ReductionDecision | None:
    """Return the highest-priority deterministic reduction for a position."""
    side = position.side.upper()
    if side not in {"LONG", "SHORT"} or position.quantity <= 0:
        return None
    executable_price = _executable_price(side, quote)
    if executable_price <= 0:
        return None
    action = "SELL" if side == "LONG" else "BUY_TO_COVER"

    if (
        math.isfinite(combined_daily_pnl)
        and math.isfinite(max_daily_loss)
        and max_daily_loss > 0
        and combined_daily_pnl <= -max_daily_loss
    ):
        return ReductionDecision(
            action=action,
            cause=ReductionCause.DAILY_LOSS,
            reason=(
                f"daily loss limit reached: combined={combined_daily_pnl:.2f}, "
                f"limit={max_daily_loss:.2f}"
            ),
            trigger_price=executable_price,
            threshold_price=None,
        )

    if config.stop_loss_pct > 0 and position.avg_entry_price > 0:
        stop_fraction = config.stop_loss_pct / 100
        if side == "LONG":
            stop_price = position.avg_entry_price * (1 - stop_fraction)
            stop_hit = executable_price <= stop_price
        else:
            stop_price = position.avg_entry_price * (1 + stop_fraction)
            stop_hit = executable_price >= stop_price
        if stop_hit:
            return ReductionDecision(
                action=action,
                cause=ReductionCause.PRICE_STOP,
                reason=(
                    f"{side.lower()} hard stop reached: executable={executable_price:.4f}, "
                    f"stop={stop_price:.4f}"
                ),
                trigger_price=executable_price,
                threshold_price=stop_price,
            )

    if in_flatten_window:
        return ReductionDecision(
            action=action,
            cause=ReductionCause.EOD_FLATTEN,
            reason="end-of-day flatten window reached",
            trigger_price=executable_price,
            threshold_price=None,
        )

    opened_at = _as_utc(position.opened_at)
    current = _as_utc(now)
    if (
        config.max_holding_minutes > 0
        and opened_at is not None
        and current is not None
        and current >= opened_at + timedelta(minutes=config.max_holding_minutes)
    ):
        return ReductionDecision(
            action=action,
            cause=ReductionCause.TIME_STOP,
            reason=f"maximum holding time reached: {config.max_holding_minutes} minutes",
            trigger_price=executable_price,
            threshold_price=None,
        )
    return None


def _executable_price(side: str, quote: ExitQuote) -> float:
    candidate = quote.bid if side == "LONG" else quote.ask
    if math.isfinite(candidate) and candidate > 0:
        return candidate
    if math.isfinite(quote.last) and quote.last > 0:
        return quote.last
    return 0.0


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
