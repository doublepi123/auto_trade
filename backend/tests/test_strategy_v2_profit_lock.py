from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.strategy_v2 import (
    ProfitLockAction,
    ProfitLockConfig,
    StrategyBar,
    evaluate_profit_lock_bar,
)


_START = datetime(2026, 7, 24, 14, 30, tzinfo=timezone.utc)


def _bar(
    minute: int,
    *,
    open_price: float,
    high: float,
    low: float,
) -> StrategyBar:
    return StrategyBar(
        timestamp=_START + timedelta(minutes=minute),
        open=open_price,
        high=high,
        low=low,
        close=open_price,
        volume=1000,
        symbol="AAPL.US",
    )


def test_activation_bar_cannot_also_claim_an_intrabar_profit_lock() -> None:
    config = ProfitLockConfig(
        activation_pct=0.40,
        locked_profit_pct=0.20,
        slippage_bps=2.0,
    )

    decision = evaluate_profit_lock_bar(
        config=config,
        entry_price=100.0,
        bar=_bar(1, open_price=100.1, high=100.5, low=100.0),
        armed_before_bar=False,
    )

    assert decision.action == ProfitLockAction.ACTIVATE
    assert decision.effective_at == _START + timedelta(minutes=2)
    assert decision.exit_price is None
    assert decision.activation_price == pytest.approx(100.4)
    assert decision.floor_price == pytest.approx(100.2)


def test_armed_profit_lock_exits_at_floor_with_adverse_slippage() -> None:
    decision = evaluate_profit_lock_bar(
        config=ProfitLockConfig(0.40, 0.20, 2.0),
        entry_price=100.0,
        bar=_bar(2, open_price=100.3, high=100.35, low=100.1),
        armed_before_bar=True,
    )

    assert decision.action == ProfitLockAction.EXIT
    assert decision.reason == "PROFIT_LOCK"
    assert decision.exit_price == pytest.approx(100.2 * 0.9998)


def test_profit_lock_gap_uses_worse_open_instead_of_the_floor() -> None:
    decision = evaluate_profit_lock_bar(
        config=ProfitLockConfig(0.40, 0.20, 2.0),
        entry_price=100.0,
        bar=_bar(2, open_price=99.9, high=100.0, low=99.8),
        armed_before_bar=True,
    )

    assert decision.action == ProfitLockAction.EXIT
    assert decision.exit_price == pytest.approx(99.9 * 0.9998)


@pytest.mark.parametrize(
    ("activation", "floor", "slippage"),
    [
        (0.0, 0.1, 2.0),
        (0.4, 0.0, 2.0),
        (0.4, 0.4, 2.0),
        (0.4, 0.2, 50.1),
    ],
)
def test_profit_lock_config_rejects_invalid_thresholds(
    activation: float,
    floor: float,
    slippage: float,
) -> None:
    with pytest.raises(ValueError):
        ProfitLockConfig(activation, floor, slippage)
