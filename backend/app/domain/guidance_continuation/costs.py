"""Cost model (PREREGISTRATION §10.7).

``commission_usd(N) = 1.568 + 0.0000641 × N`` per side; two cost columns
are computed on the ACTUAL entry/exit notionals: baseline (0.031 bps per
side) and confirmatory (2.031 bps per side).  The spread is NOT deducted
again — it is already inside the simulated fill prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)

_BPS: Final[Decimal] = Decimal("10000")


def commission(
    notional: Decimal,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> Decimal:
    """Per-side commission on the actual side notional ``N``."""
    return config.commission_fixed_usd + config.commission_rate * notional


def cost_columns(
    *,
    entry_notional: Decimal,
    exit_notional: Decimal,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> tuple[Decimal, Decimal]:
    """(baseline_total_usd, confirmatory_total_usd) for one round trip.

    Each column = both sides' commissions + its per-side execution
    deduction applied to that side's actual notional.
    """
    commissions = commission(entry_notional, config) + commission(
        exit_notional, config
    )
    baseline = commissions + (
        config.execution_deduction_bps_baseline
        / _BPS
        * (entry_notional + exit_notional)
    )
    confirmatory = commissions + (
        config.execution_deduction_bps_confirmatory
        / _BPS
        * (entry_notional + exit_notional)
    )
    return baseline, confirmatory


@dataclass(frozen=True, slots=True)
class TradeCostBreakdown:
    """Round-trip cost breakdown in both §10.7 columns."""

    entry_notional: Decimal
    exit_notional: Decimal
    entry_commission: Decimal
    exit_commission: Decimal
    baseline_total: Decimal
    confirmatory_total: Decimal
    extra_confirmatory_execution_bps: Decimal

    @property
    def commissions(self) -> Decimal:
        return self.entry_commission + self.exit_commission


def breakdown(
    *,
    entry_notional: Decimal,
    exit_notional: Decimal,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> TradeCostBreakdown:
    """Full evidence breakdown for one simulated round trip."""
    baseline, confirmatory = cost_columns(
        entry_notional=entry_notional,
        exit_notional=exit_notional,
        config=config,
    )
    extra = (
        config.execution_deduction_bps_confirmatory
        - config.execution_deduction_bps_baseline
    )
    return TradeCostBreakdown(
        entry_notional=entry_notional,
        exit_notional=exit_notional,
        entry_commission=commission(entry_notional, config),
        exit_commission=commission(exit_notional, config),
        baseline_total=baseline,
        confirmatory_total=confirmatory,
        extra_confirmatory_execution_bps=extra,
    )
