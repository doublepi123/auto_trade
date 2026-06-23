from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.platform.cashbook import CashBook, currency_for
from app.platform.events import EventSource, FillEvent


def _fill(symbol, side, qty, price, commission="0"):
    return FillEvent(timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc), source=EventSource.BROKER, symbol=symbol, broker_order_id="o", side=side, quantity=qty, price=Decimal(price), commission=Decimal(commission))


def test_currency_for_symbol_suffix():
    assert currency_for("AAPL.US") == "USD"
    assert currency_for("0700.HK") == "HKD"


def test_deposit_withdraw_and_balance():
    cb = CashBook("USD")
    cb.deposit("USD", Decimal("10000"))
    assert cb.balance("USD") == Decimal("10000")
    cb.withdraw("USD", Decimal("3000"))
    assert cb.balance("USD") == Decimal("7000")


def test_withdraw_insufficient_raises():
    cb = CashBook("USD")
    with pytest.raises(ValueError):
        cb.withdraw("USD", Decimal("1"))


def test_nav_aggregates_via_fx():
    cb = CashBook("USD")
    cb.deposit("USD", Decimal("10000"))
    cb.deposit("HKD", Decimal("50000"))
    cb.set_fx_rate("HKD", Decimal("0.128"))
    assert cb.nav() == Decimal("10000") + Decimal("50000") * Decimal("0.128")


def test_fill_debits_correct_currency():
    cb = CashBook("USD")
    cb.deposit("USD", Decimal("10000"))
    cb.deposit("HKD", Decimal("100000"))
    cb.set_fx_rate("HKD", Decimal("0.128"))
    # US buy: 10 @ 150 = 1500 USD
    cb.on_fill(_fill("AAPL.US", "BUY", 10, "150"))
    assert cb.balance("USD") == Decimal("8500")
    # HK buy: 100 @ 50 = 5000 HKD
    cb.on_fill(_fill("0700.HK", "BUY", 100, "50"))
    assert cb.balance("HKD") == Decimal("95000")


def test_base_currency_rate_must_be_one():
    cb = CashBook("USD")
    with pytest.raises(ValueError):
        cb.set_fx_rate("USD", Decimal("2"))


def test_sell_fill_credits_currency():
    cb = CashBook("USD")
    cb.deposit("USD", Decimal("0"))
    cb.on_fill(_fill("AAPL.US", "SELL", 10, "150", commission="2"))
    assert cb.balance("USD") == Decimal("1498")
