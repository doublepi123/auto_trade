"""Pure mandate facts for the SPY passive buy-and-hold lane (phase 1).

Data in, values out: no DB, no services, no settings, no wall clock. The
service layer loads a ``PassiveMandate`` row and hands the resulting facts to
``app.domain.passive_allocation.policy``.

Owner approvals (2026-09-29) pin these constants:
- SPY.US only, buy once, hold long-term, no timing / add-ons / rebalancing;
- $5,000 total allotment INCLUDING fees (risk model FULL_PRINCIPAL);
- no automatic price stop; exempt from 60-min max hold, EOD flatten,
  profit-lock and daily-loss exit; global pause and kill switch still bind;
- manual review every 6 months, which never trades automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

PASSIVE_LANE: str = "SPY_PASSIVE"
PASSIVE_SYMBOL: str = "SPY.US"
POLICY_VERSION: str = "passive-allocation-v1"
PASSIVE_ALLOTMENT_USD: Decimal = Decimal("5000")
PASSIVE_REVIEW_INTERVAL_MONTHS: int = 6
PASSIVE_ORDER_BINDING: str = "paper-only"


class RiskModel(str, Enum):
    """How the lane measures risk; only FULL_PRINCIPAL is authorised."""

    FULL_PRINCIPAL = "FULL_PRINCIPAL"


@dataclass(frozen=True, slots=True)
class PassiveMandateFacts:
    """Plain values of one persisted mandate row, for pure validation."""

    lane: str
    policy_version: str
    symbol: str
    status: str
    allotment_usd: Decimal
    risk_model: RiskModel
    exemptions: tuple[str, ...]
    review_interval_months: int
    order_binding: str


@dataclass(frozen=True, slots=True)
class ResolvedPassivePolicy:
    """The passive risk policy the pre-submit boundary enforces.

    Returned by the service-layer resolver when a request carries the
    SPY_PASSIVE lane and a valid claimed mandate stands behind it. The
    boundary then checks notional + fees against ``allotment_usd`` instead
    of the range strategy's stop-distance risk budget.
    """

    allotment_usd: Decimal


@dataclass(frozen=True, slots=True)
class PassiveEntrySizing:
    """Largest integer share count that fits the mandate, fees included."""

    quantity: int
    notional: Decimal
    commission: Decimal
    notional_with_fees: Decimal
    capped_by: str  # "allotment" | "shares" | "notional"

    @property
    def fits(self) -> bool:
        return self.quantity > 0
