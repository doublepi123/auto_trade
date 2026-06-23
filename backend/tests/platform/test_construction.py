from __future__ import annotations

from decimal import Decimal

from app.platform.construction import EqualWeightModel, RiskParityModel, weights_to_intents


def test_equal_weight_among_active_signals():
    model = EqualWeightModel()
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("0.5"), "C": Decimal("0")})
    assert set(w.keys()) == {"A", "B"}
    assert w["A"] == Decimal("0.5")
    assert w["B"] == Decimal("0.5")


def test_equal_weight_empty_when_all_zero():
    model = EqualWeightModel()
    assert model.target_weights({"A": Decimal("0")}) == {}


def test_risk_parity_inverse_vol():
    model = RiskParityModel()
    w = model.target_weights(
        {"A": Decimal("1"), "B": Decimal("1")},
        volatilities={"A": Decimal("0.1"), "B": Decimal("0.2")},
    )
    # inverse: A=10, B=5 -> total 15 -> A=2/3, B=1/3
    assert w["A"] == Decimal("10") / Decimal("15")
    assert w["B"] == Decimal("5") / Decimal("15")


def test_risk_parity_zero_vol_defaults_to_equal():
    model = RiskParityModel()
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1")})  # no vols
    assert w["A"] == Decimal("0.5")
    assert w["B"] == Decimal("0.5")


def test_weights_to_intents_buys_and_sells():
    weights = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    current = {"A": 0, "B": 20}
    prices = {"A": Decimal("100"), "B": Decimal("50")}
    nav = Decimal("10000")
    intents = weights_to_intents(weights, current, prices, nav)
    by_sym = {i.symbol: i for i in intents}
    assert by_sym["A"].side == "BUY" and by_sym["A"].quantity == 50  # 5000/100
    # B target = 5000/50 = 100, current 20 -> BUY 80
    assert by_sym["B"].side == "BUY" and by_sym["B"].quantity == 80


def test_weights_to_intents_sells_when_overweight():
    weights = {"A": Decimal("0")}
    current = {"A": 30}
    prices = {"A": Decimal("100")}
    nav = Decimal("10000")
    intents = weights_to_intents(weights, current, prices, nav)
    assert len(intents) == 1
    assert intents[0].side == "SELL"
    assert intents[0].quantity == 30
