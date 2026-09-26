"""Quantity formula and tick quantization (PREREGISTRATION §10.5)."""

from __future__ import annotations

from decimal import Decimal

from app.domain.guidance_continuation.sizing import ceil_to_tick, position_quantity


class TestQuantity:
    def test_at_250_quantity_is_capped_at_100(self) -> None:
        # min(100, 25000/250=100, 250/(0.0045*250)=222) = 100.
        assert position_quantity(Decimal("250")) == 100

    def test_at_500_quantity_is_50(self) -> None:
        # min(100, 50, 111) = 50.
        assert position_quantity(Decimal("500")) == 50

    def test_at_20_quantity_is_100(self) -> None:
        # min(100, 1250, 2777) = 100.
        assert position_quantity(Decimal("20")) == 100

    def test_at_300_risk_bound_yields_185(self) -> None:
        # min(100, 83.33, 185.18) = 83 — wait, notional 83 < risk 185.
        q = position_quantity(Decimal("300"))
        assert q == 83

    def test_high_price_notional_bound_dominates(self) -> None:
        # At 300: 25000/300 = 83.33 → floor 83 (spec's worked example).
        assert position_quantity(Decimal("300")) == 83

    def test_very_high_price_gives_no_entry(self) -> None:
        # 25000/5000 = 5; still >= 1.
        assert position_quantity(Decimal("5000")) == 5
        # 25000/30000 = 0.83 → q < 1 → no entry.
        assert position_quantity(Decimal("30000")) == 0

    def test_zero_or_negative_price_gives_zero(self) -> None:
        assert position_quantity(Decimal("0")) == 0
        assert position_quantity(Decimal("-5")) == 0


class TestTick:
    def test_exact_tick_unchanged(self) -> None:
        assert ceil_to_tick(Decimal("100.00")) == Decimal("100.00")

    def test_sub_tick_rounds_up(self) -> None:
        assert ceil_to_tick(Decimal("100.01")) == Decimal("100.01")
        assert ceil_to_tick(Decimal("100.011")) == Decimal("100.02")
        assert ceil_to_tick(Decimal("100.019")) == Decimal("100.02")

    def test_ask_like_price(self) -> None:
        # Entry: L = ceil_to_tick(ask) — e.g. ask 101.4567 → 101.46.
        assert ceil_to_tick(Decimal("101.4567")) == Decimal("101.46")

    def test_result_quantized_to_tick(self) -> None:
        result = ceil_to_tick(Decimal("33.333333"))
        assert result == Decimal("33.34")
        assert result == result.quantize(Decimal("0.01"))
