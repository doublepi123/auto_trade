from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.strategy_v2 import (
    BracketAction,
    BracketConfig,
    StrategyBar,
    evaluate_bracket_bar,
)


_ENTRY_AT = datetime(2026, 7, 27, 14, 31, tzinfo=timezone.utc)


def _config() -> BracketConfig:
    return BracketConfig(
        stop_loss_pct=0.40,
        profit_target_pct=0.70,
        slippage_bps=2.0,
        flatten_minutes_before_close=15,
    )


def _bar(
    minute: int,
    *,
    open_price: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
) -> StrategyBar:
    return StrategyBar(
        timestamp=_ENTRY_AT + timedelta(minutes=minute),
        open=open_price,
        high=high,
        low=low,
        close=open_price,
        volume=1_000,
        symbol="AAPL.US",
    )


def _evaluate(
    bar: StrategyBar,
    *,
    signal_vwap: float = 100.2,
    holding_deadline: datetime | None = None,
):
    return evaluate_bracket_bar(
        config=_config(),
        market="US",
        entry_price=100.0,
        signal_vwap=signal_vwap,
        holding_deadline=(
            holding_deadline
            or _ENTRY_AT + timedelta(minutes=60)
        ),
        bar=bar,
    )


def test_bracket_config_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="stop_loss_pct"):
        BracketConfig(0.0, 0.7, 2.0, 15)
    with pytest.raises(ValueError, match="profit_target_pct"):
        BracketConfig(0.4, 0.0, 2.0, 15)
    with pytest.raises(ValueError, match="slippage_bps"):
        BracketConfig(0.4, 0.7, 51.0, 15)
    with pytest.raises(ValueError, match="flatten_minutes"):
        BracketConfig(0.4, 0.7, 2.0, 0)


def test_bracket_holds_inside_thresholds() -> None:
    decision = _evaluate(_bar(1))

    assert decision.action == BracketAction.HOLD
    assert decision.exit_price is None
    assert decision.stop_price == pytest.approx(99.6)
    assert decision.target_price == pytest.approx(100.7)


def test_ambiguous_stop_and_target_bar_chooses_stop() -> None:
    decision = _evaluate(
        _bar(1, open_price=100.0, high=101.0, low=99.0),
    )

    assert decision.action == BracketAction.EXIT
    assert decision.reason == "PRICE_STOP"
    assert decision.exit_price == pytest.approx(99.6 * 0.9998)


def test_gap_through_stop_uses_adverse_open() -> None:
    decision = _evaluate(
        _bar(1, open_price=99.3, high=99.5, low=99.1),
    )

    assert decision.reason == "PRICE_STOP"
    assert decision.exit_price == pytest.approx(99.3 * 0.9998)


def test_signal_vwap_remains_the_target_floor() -> None:
    hold = _evaluate(
        _bar(1, high=100.8),
        signal_vwap=101.0,
    )
    exit_decision = _evaluate(
        _bar(2, open_price=100.8, high=101.1, low=100.7),
        signal_vwap=101.0,
    )

    assert hold.action == BracketAction.HOLD
    assert hold.target_price == pytest.approx(101.0)
    assert exit_decision.reason == "PROFIT_TARGET"
    assert exit_decision.exit_price == pytest.approx(101.0 * 0.9998)


def test_eod_flatten_precedes_target_but_not_stop() -> None:
    eod_bar = StrategyBar(
        timestamp=datetime(2026, 7, 27, 19, 45, tzinfo=timezone.utc),
        open=100.5,
        high=101.0,
        low=100.4,
        close=100.8,
        volume=1_000,
        symbol="AAPL.US",
    )
    stopped_bar = StrategyBar(
        timestamp=eod_bar.timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1_000,
        symbol="AAPL.US",
    )

    assert _evaluate(eod_bar).reason == "EOD_FLATTEN"
    assert _evaluate(stopped_bar).reason == "PRICE_STOP"


def test_target_precedes_max_hold() -> None:
    deadline = _ENTRY_AT + timedelta(minutes=1)
    decision = _evaluate(
        _bar(1, open_price=100.5, high=100.8, low=100.4),
        holding_deadline=deadline,
    )

    assert decision.reason == "PROFIT_TARGET"


def test_max_hold_exits_at_adverse_open() -> None:
    deadline = _ENTRY_AT + timedelta(minutes=1)
    decision = _evaluate(
        _bar(1, open_price=100.2, high=100.3, low=100.0),
        holding_deadline=deadline,
    )

    assert decision.reason == "MAX_HOLD"
    assert decision.exit_price == pytest.approx(100.2 * 0.9998)
