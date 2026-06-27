from __future__ import annotations

import pytest

from app.platform.market_impact_model import market_impact_model_report


def test_market_impact_increases_with_participation():
    low = market_impact_model_report(order_qty=100, adv=10000, volatility=0.2, participation=0.05).to_dict()
    high = market_impact_model_report(order_qty=100, adv=10000, volatility=0.2, participation=0.20).to_dict()
    assert high["total_impact_bps"] > low["total_impact_bps"]
    assert "temporary_impact_bps" in high
    assert "permanent_impact_bps" in high


def test_market_impact_notional_equals_qty_times_price():
    body = market_impact_model_report(order_qty=100, adv=10000, volatility=0.2, participation=0.1, price=10.0).to_dict()
    assert body["notional"] == 1000


def test_market_impact_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        market_impact_model_report(order_qty=0, adv=10000, volatility=0.2)
    with pytest.raises(ValueError):
        market_impact_model_report(order_qty=100, adv=10000, volatility=-0.1)
