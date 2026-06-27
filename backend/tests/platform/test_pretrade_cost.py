from __future__ import annotations

import pytest

from app.platform.pretrade_cost import pretrade_cost_report


def test_pretrade_cost_increases_with_participation():
    low = pretrade_cost_report(order_qty=100, adv=10000, price=10, spread_bps=5, volatility=0.2).to_dict()
    high = pretrade_cost_report(order_qty=2000, adv=10000, price=10, spread_bps=5, volatility=0.2).to_dict()
    assert high["total_cost_bps"] > low["total_cost_bps"]
    assert high["notional"] == 20000


def test_pretrade_cost_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        pretrade_cost_report(order_qty=0, adv=10000, price=10)
    with pytest.raises(ValueError):
        pretrade_cost_report(order_qty=100, adv=0, price=10)
