from __future__ import annotations

from app.models import StrategyConfig
from app.services.strategy_service import validate_strategy_consistency


def _config(**overrides):
    base = dict(
        buy_low=100.0,
        sell_high=110.0,
        market="US",
        min_profit_amount=10.0,
        max_daily_loss=500.0,
        fee_rate_us=0.0005,
        fee_rate_hk=0.003,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def test_consistency_clean_returns_no_issues():
    issues = validate_strategy_consistency(_config())
    assert issues == []


def test_consistency_flags_min_profit_below_fees():
    # Per-share fee = 0.0005 * 2 = 0.001; min_profit = 0.0005 < 0.001
    config = _config(min_profit_amount=0.0005, fee_rate_us=0.0005)
    issues = validate_strategy_consistency(config)
    assert any(i["field"] == "min_profit_amount" for i in issues)


def test_consistency_hk_uses_hk_fee():
    # HK fee is 10x US, so a reasonable US setup trips the HK warning.
    config = _config(market="HK", min_profit_amount=0.001, fee_rate_hk=0.003)
    issues = validate_strategy_consistency(config)
    assert any(i["field"] == "min_profit_amount" for i in issues)


def test_consistency_flags_daily_loss_below_min_profit():
    config = _config(min_profit_amount=200.0, max_daily_loss=100.0)
    issues = validate_strategy_consistency(config)
    assert any(i["field"] == "max_daily_loss" for i in issues)


def test_consistency_flags_sell_high_below_buy_low():
    config = _config(buy_low=100.0, sell_high=99.0)
    issues = validate_strategy_consistency(config)
    fields = {i["field"] for i in issues}
    assert "sell_high" in fields


def test_consistency_no_warning_when_min_profit_is_zero():
    config = _config(min_profit_amount=0.0, fee_rate_us=0.005)
    issues = validate_strategy_consistency(config)
    assert issues == []
