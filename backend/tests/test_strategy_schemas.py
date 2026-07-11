from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.schemas import StrategyConfigSchema, StrategyMergedSchema


_VALID_STRATEGY = {
    "symbol": "AAPL.US",
    "market": "US",
    "buy_low": 100.0,
    "sell_high": 110.0,
}


@pytest.mark.parametrize("schema", [StrategyConfigSchema, StrategyMergedSchema])
@pytest.mark.parametrize(
    "field",
    [
        "buy_low",
        "sell_high",
        "min_profit_amount",
        "max_daily_loss",
        "fee_rate_us",
        "fee_rate_hk",
        "min_repricing_pct",
        "margin_safety_factor",
        "max_position_notional",
        "max_risk_per_trade",
        "stop_loss_pct",
    ],
)
@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf],
    ids=["nan", "positive_inf", "negative_inf"],
)
def test_strategy_schemas_reject_non_finite_numbers(
    schema: type[StrategyConfigSchema] | type[StrategyMergedSchema],
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate({**_VALID_STRATEGY, field: value})


@pytest.mark.parametrize("schema", [StrategyConfigSchema, StrategyMergedSchema])
@pytest.mark.parametrize(
    "field",
    ["short_selling", "allow_position_addons", "llm_order_execution_enabled"],
)
def test_strategy_schemas_permanently_reject_disabled_live_features(
    schema: type[StrategyConfigSchema] | type[StrategyMergedSchema],
    field: str,
) -> None:
    with pytest.raises(ValidationError, match="disabled by the P0 live safety policy"):
        schema.model_validate({**_VALID_STRATEGY, field: True})
