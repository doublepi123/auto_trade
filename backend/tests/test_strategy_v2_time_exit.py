from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.strategy_v2 import (
    StrategyBar,
    TimeExitAction,
    TimeExitConfig,
    evaluate_time_exit_bar,
)


_ENTRY_AT = datetime(2026, 7, 24, 14, 31, tzinfo=timezone.utc)


def _bar(minute: int, *, open_price: float = 100.0) -> StrategyBar:
    return StrategyBar(
        timestamp=_ENTRY_AT + timedelta(minutes=minute),
        open=open_price,
        high=open_price + 0.1,
        low=open_price - 0.1,
        close=open_price,
        volume=1000,
        symbol="AAPL.US",
    )


def test_time_exit_holds_before_deadline_and_exits_at_deadline_open() -> None:
    config = TimeExitConfig(max_holding_minutes=15, slippage_bps=2.0)

    before = evaluate_time_exit_bar(
        config=config,
        entry_at=_ENTRY_AT,
        bar=_bar(14, open_price=100.2),
    )
    deadline = evaluate_time_exit_bar(
        config=config,
        entry_at=_ENTRY_AT,
        bar=_bar(15, open_price=99.8),
    )

    assert before.action == TimeExitAction.HOLD
    assert before.exit_price is None
    assert deadline.action == TimeExitAction.EXIT
    assert deadline.reason == "TIME_STOP"
    assert deadline.event_at == _ENTRY_AT + timedelta(minutes=15)
    assert deadline.exit_price == pytest.approx(99.8 * 0.9998)


def test_time_exit_uses_first_observed_open_after_a_bar_gap() -> None:
    decision = evaluate_time_exit_bar(
        config=TimeExitConfig(max_holding_minutes=15, slippage_bps=0.0),
        entry_at=_ENTRY_AT,
        bar=_bar(18, open_price=99.5),
    )

    assert decision.action == TimeExitAction.EXIT
    assert decision.event_at == _ENTRY_AT + timedelta(minutes=18)
    assert decision.exit_price == pytest.approx(99.5)
    assert decision.holding_deadline == _ENTRY_AT + timedelta(minutes=15)


@pytest.mark.parametrize(
    ("max_holding_minutes", "slippage_bps"),
    [
        (0, 2.0),
        (1_441, 2.0),
        (15, -0.1),
        (15, 50.1),
        (15, float("nan")),
    ],
)
def test_time_exit_config_rejects_invalid_values(
    max_holding_minutes: int,
    slippage_bps: float,
) -> None:
    with pytest.raises(ValueError):
        TimeExitConfig(
            max_holding_minutes=max_holding_minutes,
            slippage_bps=slippage_bps,
        )


def test_time_exit_requires_timezone_aware_entry() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_time_exit_bar(
            config=TimeExitConfig(max_holding_minutes=15, slippage_bps=2.0),
            entry_at=_ENTRY_AT.replace(tzinfo=None),
            bar=_bar(15),
        )
