from __future__ import annotations

from decimal import Decimal

import pytest

from app.platform.sizers import (
    ATRSizer,
    FixedFractionalSizer,
    FullEquitySizer,
    SizerRegistry,
    get_default_registry,
    intent_from_signal,
)


def test_fixed_fractional_sizing():
    s = FixedFractionalSizer(fraction=Decimal("0.25"))
    assert s.size("BUY", Decimal("50"), Decimal("10000")) == 50  # 10000*0.25/50 = 50


def test_full_equity_sizing():
    s = FullEquitySizer()
    assert s.size("BUY", Decimal("100"), Decimal("10000")) == 100


def test_atr_sizing():
    s = ATRSizer(risk_fraction=Decimal("0.02"), atr_multiplier=Decimal("1"))
    # nav 10000, risk 200, atr 4 -> stop 4 -> qty 50
    assert s.size("BUY", Decimal("100"), Decimal("10000"), atr=Decimal("4")) == 50


def test_atr_sizer_returns_zero_without_atr():
    s = ATRSizer()
    assert s.size("BUY", Decimal("100"), Decimal("10000")) == 0


def test_zero_size_when_price_or_nav_nonpositive():
    s = FixedFractionalSizer()
    assert s.size("BUY", Decimal("0"), Decimal("10000")) == 0
    assert s.size("BUY", Decimal("100"), Decimal("0")) == 0


def test_registry_lists_default_sizers():
    reg = get_default_registry()
    names = {s["name"] for s in reg.list()}
    assert names == {"fixed_fractional", "full_equity", "atr"}
    assert reg.get("fixed_fractional").size("BUY", Decimal("100"), Decimal("10000")) == 10


def test_registry_register_duplicate_raises():
    reg = SizerRegistry()
    reg.register(FixedFractionalSizer())
    with pytest.raises(ValueError):
        reg.register(FixedFractionalSizer())


def test_intent_from_signal_returns_none_for_zero_size():
    sizer = FixedFractionalSizer(fraction=Decimal("0"))  # zero fraction -> 0 qty
    intent = intent_from_signal("AAPL.US", "BUY", Decimal("100"), sizer, Decimal("10000"))
    assert intent is None


def test_intent_from_signal_builds_intent():
    sizer = FixedFractionalSizer(fraction=Decimal("0.25"))
    intent = intent_from_signal("AAPL.US", "BUY", Decimal("50"), sizer, Decimal("10000"))
    assert intent is not None
    assert intent.quantity == 50
    assert intent.side == "BUY"
