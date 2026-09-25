from __future__ import annotations

import json
from decimal import Decimal

from app.core.accounting_fees import (
    ACCOUNTING_FEE_MODEL_US_SEC98,
    SEC98_FIXED_USD,
    allocated_entry_fee,
    model_applies,
    model_from_config_snapshot,
    order_fee,
)

_LEGACY_RATE = Decimal("0.0005")
_HK_RATE = Decimal("0.003")


class TestModelApplies:
    def test_only_the_sec98_constant_on_us_applies(self) -> None:
        assert model_applies(ACCOUNTING_FEE_MODEL_US_SEC98, "US") is True
        assert model_applies(ACCOUNTING_FEE_MODEL_US_SEC98, "us") is True

    def test_hk_never_applies_even_with_the_marker(self) -> None:
        assert model_applies(ACCOUNTING_FEE_MODEL_US_SEC98, "HK") is False

    def test_missing_or_foreign_models_do_not_apply(self) -> None:
        assert model_applies(None, "US") is False
        assert model_applies("", "US") is False
        assert model_applies("us-sec98-v2", "US") is False


class TestOrderFee:
    def test_us_sec98_charges_fixed_plus_notional_rate(self) -> None:
        # 1.568 + 0.0000641 * 250 * 100 = 3.1705
        assert order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            price=Decimal("250"),
            quantity=Decimal("100"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("3.1705")

    def test_zero_quantity_charges_nothing(self) -> None:
        assert order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            price=Decimal("250"),
            quantity=Decimal("0"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("0")

    def test_hk_with_the_marker_keeps_the_legacy_formula(self) -> None:
        assert order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="HK",
            price=Decimal("250"),
            quantity=Decimal("100"),
            legacy_rate=_HK_RATE,
        ) == Decimal("250") * Decimal("100") * _HK_RATE

    def test_without_the_marker_the_legacy_formula_applies(self) -> None:
        assert order_fee(
            model=None,
            market="US",
            price=Decimal("250"),
            quantity=Decimal("100"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("250") * Decimal("100") * _LEGACY_RATE


class TestAllocatedEntryFee:
    def test_us_sec98_partial_reduction_is_proportional(self) -> None:
        # PROPORTIONAL allocation from the remaining position: the 40-share
        # exit takes 40% of the entry order's fee:
        # (1.568 + 0.0000641*250*100) * 40 / 100 = 3.1705 * 0.4 = 1.2682.
        assert allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("100"),
            fill_quantity=Decimal("40"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("1.2682")

    def test_us_sec98_closing_exit_carries_fixed_plus_variable(self) -> None:
        # The exit closing the remaining 60 shares:
        # (1.568 + 0.0000641*250*100) * 60 / 100 = 3.1705 * 0.6 = 2.5295.
        assert allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("60"),
            fill_quantity=Decimal("60"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("2.5295")

    def test_us_sec98_local_partials_over_recognise_by_at_most_one_fixed_fee(self) -> None:
        # Buy 100 @ 250, sell 40 then 60. Proportional allocation from the
        # REMAINING position over-recognises the fixed commission (each local
        # partial carries a fresh fixed share): 1.2682 + 2.5295 = 3.7977,
        # which is above order_fee(250,100)=3.1705 but never by more than one
        # extra fixed commission (<= 3.1705 + 1.568). That is conservative
        # for risk and never under-charges.
        first = allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("100"),
            fill_quantity=Decimal("40"),
            legacy_rate=_LEGACY_RATE,
        )
        second = allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("60"),
            fill_quantity=Decimal("60"),
            legacy_rate=_LEGACY_RATE,
        )
        whole = order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            price=Decimal("250"),
            quantity=Decimal("100"),
            legacy_rate=_LEGACY_RATE,
        )
        assert first == Decimal("1.2682")
        assert second == Decimal("2.5295")
        assert first + second == Decimal("3.7977")
        assert first + second >= whole
        assert first + second <= whole + SEC98_FIXED_USD

    def test_us_sec98_single_full_close_equals_the_order_fee(self) -> None:
        assert allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("100"),
            fill_quantity=Decimal("100"),
            legacy_rate=_LEGACY_RATE,
        ) == order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            price=Decimal("250"),
            quantity=Decimal("100"),
            legacy_rate=_LEGACY_RATE,
        )

    def test_without_the_marker_today_s_formula_applies(self) -> None:
        assert allocated_entry_fee(
            model=None,
            market="US",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("100"),
            fill_quantity=Decimal("40"),
            legacy_rate=_LEGACY_RATE,
        ) == Decimal("250") * Decimal("40") * _LEGACY_RATE

    def test_hk_with_the_marker_keeps_the_legacy_formula(self) -> None:
        assert allocated_entry_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="HK",
            cost_basis_price=Decimal("250"),
            position_quantity_before=Decimal("100"),
            fill_quantity=Decimal("40"),
            legacy_rate=_HK_RATE,
        ) == Decimal("250") * Decimal("40") * _HK_RATE


class TestModelFromConfigSnapshot:
    def test_valid_snapshot_returns_the_constant(self) -> None:
        raw = json.dumps({"accounting_fee_model": ACCOUNTING_FEE_MODEL_US_SEC98})
        assert model_from_config_snapshot(raw) == ACCOUNTING_FEE_MODEL_US_SEC98

    def test_empty_object_returns_none(self) -> None:
        assert model_from_config_snapshot("{}") is None

    def test_none_returns_none(self) -> None:
        assert model_from_config_snapshot(None) is None

    def test_invalid_json_returns_none(self) -> None:
        assert model_from_config_snapshot("{not json") is None

    def test_foreign_value_returns_none(self) -> None:
        assert model_from_config_snapshot(
            json.dumps({"accounting_fee_model": "other-model"})
        ) is None

    def test_non_dict_payload_returns_none(self) -> None:
        assert model_from_config_snapshot(
            json.dumps([ACCOUNTING_FEE_MODEL_US_SEC98])
        ) is None
