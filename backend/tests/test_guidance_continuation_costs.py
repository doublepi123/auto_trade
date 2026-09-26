"""Cost model columns (PREREGISTRATION §10.7)."""

from __future__ import annotations

from decimal import Decimal

from app.domain.guidance_continuation.costs import (
    breakdown,
    commission,
    cost_columns,
)


class TestCommission:
    def test_formula_per_side(self) -> None:
        # 1.568 + 0.0000641 × N.
        assert commission(Decimal("25000")) == Decimal("1.568") + Decimal(
            "0.0000641"
        ) * Decimal("25000")

    def test_zero_notional_still_pays_fixed_fee(self) -> None:
        assert commission(Decimal("0")) == Decimal("1.568")


class TestColumns:
    def test_columns_at_25k_round_trip(self) -> None:
        baseline, confirmatory = cost_columns(
            entry_notional=Decimal("25000"),
            exit_notional=Decimal("25000"),
        )
        # commissions both sides: 2 × (1.568 + 0.0000641×25000) = 2×3.1705
        # = 6.341; confirmatory deduction 2 sides × 2.031 bps × 25000
        # = 10.155 → total 16.496 ≈ 6.598 bps of 25000.
        assert baseline == Decimal("6.341") + Decimal("0.031") / Decimal(
            "10000"
        ) * Decimal("50000")
        assert confirmatory == Decimal("6.341") + Decimal("2.031") / Decimal(
            "10000"
        ) * Decimal("50000")
        assert (confirmatory / Decimal("25000") * Decimal("10000")).quantize(
            Decimal("0.001")
        ) == Decimal("6.598")

    def test_confirmatory_minus_baseline_is_2bps_both_sides(self) -> None:
        baseline, confirmatory = cost_columns(
            entry_notional=Decimal("25000"),
            exit_notional=Decimal("25000"),
        )
        assert (
            confirmatory - baseline
            == Decimal("2.0") / Decimal("10000") * Decimal("50000")
        )

    def test_actual_notionals_used_not_fixed_25k(self) -> None:
        # Unequal entry/exit notionals must be priced per side.
        baseline, confirmatory = cost_columns(
            entry_notional=Decimal("10000"),
            exit_notional=Decimal("12000"),
        )
        commissions = 2 * Decimal("1.568") + Decimal("0.0000641") * Decimal(
            "22000"
        )
        assert baseline == commissions + Decimal("0.031") / Decimal(
            "10000"
        ) * Decimal("22000")
        assert confirmatory == commissions + Decimal("2.031") / Decimal(
            "10000"
        ) * Decimal("22000")

    def test_breakdown_exposes_all_fields(self) -> None:
        b = breakdown(
            entry_notional=Decimal("25000"),
            exit_notional=Decimal("25000"),
        )
        assert b.entry_commission == commission(Decimal("25000"))
        assert b.exit_commission == commission(Decimal("25000"))
        assert b.commissions == b.entry_commission + b.exit_commission
        assert b.extra_confirmatory_execution_bps == Decimal("2.000")
        assert b.confirmatory_total > b.baseline_total
