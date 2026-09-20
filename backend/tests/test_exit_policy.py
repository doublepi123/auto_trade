from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exit_policy import (
    ExitPolicyConfig,
    ExitQuote,
    PositionExitContext,
    ReductionCause,
    ReductionDecision,
    evaluate_exit_policy,
)


NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def _position(*, side: str = "LONG", opened_at: datetime | None = None) -> PositionExitContext:
    return PositionExitContext(
        symbol="NVDA.US",
        side=side,
        quantity=10,
        avg_entry_price=100,
        opened_at=opened_at or NOW,
    )


def _evaluate(
    *,
    position: PositionExitContext | None = None,
    quote: ExitQuote | None = None,
    now: datetime = NOW,
    flatten: bool = False,
    realized_daily_pnl: float = 0,
    unrealized_pnl: float | None = 0,
    price_evidence_trusted: bool = True,
) -> ReductionDecision | None:
    return evaluate_exit_policy(
        config=ExitPolicyConfig(stop_loss_pct=1, max_holding_minutes=60),
        position=position or _position(),
        quote=quote or ExitQuote(last=100, bid=99.9, ask=100.1),
        now=now,
        in_flatten_window=flatten,
        realized_daily_pnl=realized_daily_pnl,
        unrealized_pnl=unrealized_pnl,
        max_daily_loss=500,
        price_evidence_trusted=price_evidence_trusted,
    )


def test_long_price_stop_uses_executable_bid_at_boundary() -> None:
    decision = _evaluate(quote=ExitQuote(last=99.5, bid=99.0, ask=99.1))
    assert decision is not None
    assert decision.action == "SELL"
    assert decision.cause == ReductionCause.PRICE_STOP
    assert decision.trigger_price == 99.0


def test_short_price_stop_uses_executable_ask() -> None:
    decision = _evaluate(
        position=_position(side="SHORT"),
        quote=ExitQuote(last=100.5, bid=100.9, ask=101.0),
    )
    assert decision is not None
    assert decision.action == "BUY_TO_COVER"
    assert decision.cause == ReductionCause.PRICE_STOP


def test_daily_loss_has_priority_over_price_stop() -> None:
    decision = _evaluate(
        quote=ExitQuote(last=98, bid=98, ask=98.1),
        unrealized_pnl=-500,
    )
    assert decision is not None
    assert decision.cause == ReductionCause.DAILY_LOSS


def test_eod_flatten_precedes_time_stop() -> None:
    decision = _evaluate(now=NOW + timedelta(hours=2), flatten=True)
    assert decision is not None
    assert decision.cause == ReductionCause.EOD_FLATTEN


def test_time_stop_triggers_at_exact_boundary() -> None:
    decision = _evaluate(now=NOW + timedelta(minutes=60))
    assert decision is not None
    assert decision.cause == ReductionCause.TIME_STOP


def test_time_stop_does_not_trigger_before_boundary() -> None:
    assert _evaluate(now=NOW + timedelta(minutes=59, seconds=59)) is None


def test_missing_opened_at_disables_only_time_stop() -> None:
    position = PositionExitContext("NVDA.US", "LONG", 10, 100, None)
    assert _evaluate(position=position, now=NOW + timedelta(days=1)) is None


@pytest.mark.parametrize("realized_daily_pnl, unrealized_pnl, expected", [
    (0, -1000, None),
    (-1000, None, ReductionCause.DAILY_LOSS),
])
def test_invalid_quote_cannot_trigger_exit(
    realized_daily_pnl: float, unrealized_pnl: float | None,
    expected: ReductionCause | None,
) -> None:
    """Invalid quotes cannot establish unrealized loss; realized breaches need no quote."""
    decision = _evaluate(
        quote=ExitQuote(last=0, bid=0, ask=0),
        realized_daily_pnl=realized_daily_pnl, unrealized_pnl=unrealized_pnl,
    )
    if expected is None:
        assert decision is None
    else:
        assert decision is not None
        assert decision.cause == expected


def test_time_stop_fires_without_executable_price() -> None:
    decision = _evaluate(quote=ExitQuote(0, 0, 0), now=NOW + timedelta(hours=2))
    assert decision is not None
    assert decision.cause == ReductionCause.TIME_STOP
    assert decision.trigger_price == 0.0


def test_eod_flatten_fires_without_executable_price() -> None:
    decision = _evaluate(quote=ExitQuote(0, 0, 0), flatten=True)
    assert decision is not None
    assert decision.cause == ReductionCause.EOD_FLATTEN
    assert decision.trigger_price == 0.0


def test_realized_daily_loss_breach_fires_without_quote() -> None:
    decision = _evaluate(
        quote=ExitQuote(0, 0, 0), realized_daily_pnl=-1000, unrealized_pnl=None,
        price_evidence_trusted=False,
    )
    assert decision is not None
    assert decision.cause == ReductionCause.DAILY_LOSS
    assert decision.trigger_price == 0.0


def test_unrealized_daily_loss_requires_trusted_valuation() -> None:
    decision = _evaluate(
        realized_daily_pnl=-100, unrealized_pnl=-900, price_evidence_trusted=False,
    )
    assert decision is None


def test_price_stop_suppressed_when_price_evidence_untrusted() -> None:
    decision = _evaluate(quote=ExitQuote(98, 98, 98.1), price_evidence_trusted=False)
    assert decision is None


def test_untrusted_price_evidence_still_allows_time_stop() -> None:
    decision = _evaluate(
        quote=ExitQuote(98, 98, 98.1), price_evidence_trusted=False,
        now=NOW + timedelta(hours=2),
    )
    assert decision is not None
    assert decision.cause == ReductionCause.TIME_STOP


def test_nan_bid_never_falls_back_to_last_for_price_stop() -> None:
    decision = _evaluate(
        quote=ExitQuote(last=98, bid=float("nan"), ask=float("nan")),
        price_evidence_trusted=False,
    )
    assert decision is None


def _evaluate_with_profit_lock(
    *,
    position: PositionExitContext | None = None,
    quote: ExitQuote,
    peak: float | None,
    activation_pct: float = 0.4,
    lock_pct: float = 0.2,
    combined_pnl: float = 0,
    price_evidence_trusted: bool = True,
) -> ReductionDecision | None:
    return evaluate_exit_policy(
        config=ExitPolicyConfig(
            stop_loss_pct=1,
            max_holding_minutes=60,
            profit_lock_activation_pct=activation_pct,
            profit_lock_lock_pct=lock_pct,
        ),
        position=position or _position(),
        quote=quote,
        now=NOW,
        in_flatten_window=False,
        realized_daily_pnl=0,
        unrealized_pnl=combined_pnl,
        max_daily_loss=500,
        price_evidence_trusted=price_evidence_trusted,
        peak_executable_price=peak,
    )


def test_profit_lock_triggers_after_activation_and_pullback_to_lock() -> None:
    """Measured pattern: losers reached +0.44%/+0.69% MFE, then took the full
    -1% stop or timed out at a loss. Once the peak executable price clears the
    activation excursion, a pullback to breakeven-plus-lock must exit."""
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.19, bid=100.19, ask=100.21),
        peak=100.41,  # +0.41% >= activation 0.4%
    )
    assert decision is not None
    assert decision.action == "SELL"
    assert decision.cause == ReductionCause.PROFIT_LOCK
    assert decision.trigger_price == 100.19
    assert decision.threshold_price == 100.2  # 100 entry * (1 + 0.2%)


def test_profit_lock_inactive_before_activation() -> None:
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.19, bid=100.19, ask=100.21),
        peak=100.39,  # below activation 0.4%
    )
    assert decision is None


def test_profit_lock_does_not_fire_above_lock_price() -> None:
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.25, bid=100.25, ask=100.27),
        peak=100.41,
    )
    assert decision is None


def test_profit_lock_requires_peak_evidence() -> None:
    """After a restart the peak is unknown; without it the lock must stay
    inactive (the hard stop, flatten and time stop still protect the
    position)."""
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.19, bid=100.19, ask=100.21),
        peak=None,
    )
    assert decision is None


def test_profit_lock_disabled_when_unconfigured() -> None:
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.19, bid=100.19, ask=100.21),
        peak=100.41,
        activation_pct=0.0,
        lock_pct=0.0,
    )
    assert decision is None


def test_daily_loss_has_priority_over_profit_lock() -> None:
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(last=100.19, bid=100.19, ask=100.21),
        peak=100.41,
        combined_pnl=-500,
    )
    assert decision is not None
    assert decision.cause == ReductionCause.DAILY_LOSS


def test_short_profit_lock_symmetric() -> None:
    decision = _evaluate_with_profit_lock(
        position=_position(side="SHORT"),
        quote=ExitQuote(last=99.81, bid=99.79, ask=99.81),
        peak=99.59,  # -0.41% favourable excursion for a short
    )
    assert decision is not None
    assert decision.action == "BUY_TO_COVER"
    assert decision.cause == ReductionCause.PROFIT_LOCK
    assert decision.threshold_price == 99.8  # 100 entry * (1 - 0.2%)


def test_profit_lock_suppressed_when_price_evidence_untrusted() -> None:
    decision = _evaluate_with_profit_lock(
        quote=ExitQuote(100.19, 100.19, 100.21), peak=100.41,
        price_evidence_trusted=False,
    )
    assert decision is None
