from __future__ import annotations

from decimal import Decimal

from app.platform.margin import FixedMarginModel, LeverageGuard


def test_fixed_margin_initial_and_maintenance():
    m = FixedMarginModel(margin_rate=Decimal("0.5"), maint_rate=Decimal("0.25"))
    assert m.initial_margin(100, Decimal("100")) == Decimal("5000")  # 0.5 * 100 * 100
    assert m.maintenance_margin(100, Decimal("100"), Decimal("100")) == Decimal("2500")


def test_leverage_guard_allows_within_cap():
    model = FixedMarginModel(margin_rate=Decimal("0.5"))
    # equity 10000, exposure 0, cap 2x -> can open up to 20000 notional
    guard = LeverageGuard(max_leverage=Decimal("2"), equity_provider=lambda: Decimal("10000"), exposure_provider=lambda: Decimal("0"), margin_model=model)
    assert guard.can_open(100, Decimal("100")) is True  # adds 10000 -> leverage 1.0


def test_leverage_guard_blocks_over_cap():
    model = FixedMarginModel(margin_rate=Decimal("0.5"))
    guard = LeverageGuard(max_leverage=Decimal("1"), equity_provider=lambda: Decimal("10000"), exposure_provider=lambda: Decimal("0"), margin_model=model)
    # adding 100*200 = 20000 on equity 10000 -> leverage 2.0 > 1.0 cap
    assert guard.can_open(100, Decimal("200")) is False


def test_available_capacity_accounts_for_existing_exposure():
    model = FixedMarginModel(margin_rate=Decimal("0.5"))
    guard = LeverageGuard(max_leverage=Decimal("2"), equity_provider=lambda: Decimal("10000"), exposure_provider=lambda: Decimal("5000"), margin_model=model)
    # cap exposure = 2*10000 = 20000; existing 5000 -> remaining 15000
    assert guard.available_capacity() == Decimal("15000")


def test_zero_equity_blocks_opening():
    model = FixedMarginModel()
    guard = LeverageGuard(max_leverage=Decimal("2"), equity_provider=lambda: Decimal("0"), exposure_provider=lambda: Decimal("0"), margin_model=model)
    assert guard.can_open(1, Decimal("100")) is False
    assert guard.available_capacity() == Decimal("0")
