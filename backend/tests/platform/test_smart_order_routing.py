"""Tests for P251 smart order routing."""

from __future__ import annotations

import pytest

from app.platform.smart_order_routing import VenueQuote, route_order


def _venues():
    return [
        {"venue": "A", "bid": 99.8, "bid_size": 200, "ask": 100.1, "ask_size": 100, "fee_per_share": 0.0},
        {"venue": "B", "bid": 99.9, "bid_size": 150, "ask": 100.2, "ask_size": 300, "fee_per_share": 0.0},
        {"venue": "C", "bid": 99.7, "bid_size": 500, "ask": 100.05, "ask_size": 200, "fee_per_share": 0.0},
    ]


def test_buy_routes_to_cheapest_ask():
    res = route_order("buy", 100, _venues())
    # Venue C has the lowest ask (100.05).
    assert res.child_orders[0]["venue"] == "C"
    assert res.child_orders[0]["quantity"] == 100
    assert res.filled_quantity == 100
    assert res.unfilled_quantity == 0


def test_buy_splits_across_venues_when_depth_exhausted():
    res = route_order("buy", 350, _venues())
    # C (200) then A (100) then B (50).
    venues_filled = [(c["venue"], c["quantity"]) for c in res.child_orders]
    assert venues_filled[0] == ("C", 200)
    assert venues_filled[1] == ("A", 100)
    assert venues_filled[2] == ("B", 50)
    assert res.filled_quantity == 350


def test_sell_routes_to_highest_bid():
    res = route_order("sell", 100, _venues())
    # Venue B has the highest bid (99.9).
    assert res.child_orders[0]["venue"] == "B"
    assert res.child_orders[0]["quantity"] == 100


def test_unfilled_when_liquidity_insufficient():
    res = route_order("buy", 10000, _venues())
    assert res.filled_quantity == 600  # 100 + 300 + 200
    assert res.unfilled_quantity == 9400
    assert res.weighted_avg_price > 0.0


def test_fees_added_to_effective_price_for_buy():
    venues = [
        {"venue": "A", "bid": 99.0, "bid_size": 100, "ask": 100.0, "ask_size": 100, "fee_per_share": 0.05},
        {"venue": "B", "bid": 99.0, "bid_size": 100, "ask": 100.02, "ask_size": 100, "fee_per_share": 0.0},
    ]
    res = route_order("buy", 50, venues)
    # Effective: A=100.05, B=100.02 -> B wins.
    assert res.child_orders[0]["venue"] == "B"
    res_a = route_order("buy", 50, list(reversed(venues)))
    # With A's fee making it 100.05 vs B 100.02, B still wins regardless of order.
    assert res_a.child_orders[0]["venue"] == "B"


def test_tick_quantisation_buy_rounds_down():
    venues = [{"venue": "X", "bid": 100.0, "bid_size": 100, "ask": 100.123, "ask_size": 100,
               "fee_per_share": 0.0, "tick_size": 0.05}]
    res = route_order("buy", 10, venues)
    # 100.123 quantised down to 0.05 tick -> 100.10
    assert abs(res.child_orders[0]["price"] - 100.10) < 1e-9


def test_tick_quantisation_sell_rounds_up():
    venues = [{"venue": "X", "bid": 99.877, "bid_size": 100, "ask": 100.0, "ask_size": 100,
               "fee_per_share": 0.0, "tick_size": 0.05}]
    res = route_order("sell", 10, venues)
    # 99.877 quantised up to 0.05 tick -> 99.90
    assert abs(res.child_orders[0]["price"] - 99.90) < 1e-9


def test_weighted_avg_price_correct():
    res = route_order("buy", 350, _venues())
    expected = (200 * 100.05 + 100 * 100.1 + 50 * 100.2) / 350
    assert abs(res.weighted_avg_price - expected) < 1e-9


def test_venue_quote_dataclass_accepted():
    v = VenueQuote(venue="A", bid=99.0, bid_size=100, ask=100.0, ask_size=100)
    res = route_order("buy", 50, [v])
    assert res.filled_quantity == 50


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        route_order("hold", 100, _venues())


def test_nonpositive_quantity_raises():
    with pytest.raises(ValueError):
        route_order("buy", 0, _venues())


def test_empty_venues_raises():
    with pytest.raises(ValueError):
        route_order("buy", 100, [])


def test_to_dict_roundtrip():
    res = route_order("buy", 100, _venues())
    d = res.to_dict()
    assert d["side"] == "buy"
    assert d["filled_quantity"] == 100
    assert "child_orders" in d