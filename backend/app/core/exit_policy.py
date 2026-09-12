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
    PROFIT_LOCK = "PROFIT_LOCK"


@dataclass(frozen=True)
class ExitPolicyConfig:
    stop_loss_pct: float
    max_holding_minutes: int
    profit_lock_activation_pct: float = 0.0
    profit_lock_lock_pct: float = 0.0


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
    peak_executable_price: float | None = None,
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

    profit_lock = _profit_lock_decision(
        config=config,
        position=position,
        side=side,
        action=action,
        executable_price=executable_price,
        peak_executable_price=peak_executable_price,
    )
    if profit_lock is not None:
        return profit_lock

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


def _profit_lock_decision(
    *,
    config: ExitPolicyConfig,
    position: PositionExitContext,
    side: str,
    action: str,
    executable_price: float,
    peak_executable_price: float | None,
) -> ReductionDecision | None:
    """Lock a small profit once a winning trade gives it all back.

    Measured on the live ledger: losing exits averaged +0.44% (TIME_STOP) and
    +0.69% (PRICE_STOP) peak favourable excursion before closing at the full
    loss — the strategy had no give-back protection, so every pop that
    reversed paid the whole stop. Once the peak executable price clears the
    activation excursion, a pullback to breakeven-plus-lock exits instead.

    Sits after PRICE_STOP (the hard stop always wins) and before EOD_FLATTEN /
    TIME_STOP. Disabled unless both pcts are positive and a peak observation
    exists; the peak is process-local, so after a restart the lock stays
    inactive until fresh evidence accumulates — the other three exits still
    protect the position.
    """
    activation_pct = config.profit_lock_activation_pct
    lock_pct = config.profit_lock_lock_pct
    if (
        activation_pct <= 0
        or lock_pct <= 0
        or position.avg_entry_price <= 0
        or peak_executable_price is None
        or not math.isfinite(peak_executable_price)
        or peak_executable_price <= 0
    ):
        return None
    activation_fraction = activation_pct / 100
    lock_fraction = lock_pct / 100
    if side == "LONG":
        activation_price = position.avg_entry_price * (1 + activation_fraction)
        lock_price = position.avg_entry_price * (1 + lock_fraction)
        armed = peak_executable_price >= activation_price
        lock_hit = executable_price <= lock_price
    else:
        activation_price = position.avg_entry_price * (1 - activation_fraction)
        lock_price = position.avg_entry_price * (1 - lock_fraction)
        armed = peak_executable_price <= activation_price
        lock_hit = executable_price >= lock_price
    if not armed or not lock_hit:
        return None
    return ReductionDecision(
        action=action,
        cause=ReductionCause.PROFIT_LOCK,
        reason=(
            f"{side.lower()} profit lock: peak={peak_executable_price:.4f} "
            f"cleared activation {activation_price:.4f}, "
            f"executable={executable_price:.4f} fell back to lock "
            f"{lock_price:.4f}"
        ),
        trigger_price=executable_price,
        threshold_price=lock_price,
    )


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
