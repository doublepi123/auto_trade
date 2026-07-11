from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.exit_policy import (
    ExitPolicyConfig,
    ExitQuote,
    PositionExitContext,
    ReductionCause,
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
    combined_pnl: float = 0,
):
    return evaluate_exit_policy(
        config=ExitPolicyConfig(stop_loss_pct=1, max_holding_minutes=60),
        position=position or _position(),
        quote=quote or ExitQuote(last=100, bid=99.9, ask=100.1),
        now=now,
        in_flatten_window=flatten,
        combined_daily_pnl=combined_pnl,
        max_daily_loss=500,
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
        combined_pnl=-500,
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


def test_invalid_quote_cannot_trigger_exit() -> None:
    assert _evaluate(quote=ExitQuote(last=0, bid=0, ask=0), combined_pnl=-1000) is None
