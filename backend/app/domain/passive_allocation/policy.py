"""Pure policy for the SPY passive buy-and-hold lane (phase 1).

Everything here is arithmetic and comparison over injected values: no DB, no
services, no settings, no wall clock (mirrors the domain purity contract).

The lane is identified by the persisted mandate row (``PassiveMandateFacts``),
never by a ``symbol == "SPY.US"`` string match, so a forged SPY order with no
mandate (or a consumed authorisation) is refused at the boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, ROUND_FLOOR
from typing import Final

from app.domain.passive_allocation.model import (
    PASSIVE_ALLOTMENT_USD,
    PASSIVE_LANE,
    PASSIVE_ORDER_BINDING,
    PASSIVE_REVIEW_INTERVAL_MONTHS,
    PASSIVE_SYMBOL,
    POLICY_VERSION,
    PassiveEntrySizing,
    PassiveMandateFacts,
    ResolvedPassivePolicy,
    RiskModel,
)

REQUIRED_EXEMPTIONS: Final[tuple[str, ...]] = (
    "no_price_stop",
    "no_time_stop",
    "no_eod_flatten",
    "no_profit_lock",
    "no_daily_loss_exit",
)

EXECUTION_CONTEXT_LANE_KEY: Final[str] = "passive_lane"
EXECUTION_CONTEXT_CLAIM_TOKEN_KEY: Final[str] = "passive_entry_claim_token"
EXECUTION_CONTEXT_SIZED_QUANTITY_KEY: Final[str] = "passive_sized_quantity"

# One-time submit state machine (architect contract spy-passive-submit-contract).
# Superseded by protocol.py's AUTHORIZED -> SUBMIT_CLAIMED -> CHECKING ->
# SUBMITTING -> ORDER_KNOWN/NO_SUBMIT/UNCERTAIN chain; these legacy names are
# retained ONLY for the conservative migration of pre-protocol rows.
SUBMIT_STATE_AUTHORIZED: Final[str] = "AUTHORIZED"
SUBMIT_STATE_SUBMIT_CLAIMED: Final[str] = "SUBMIT_CLAIMED"
SUBMIT_STATE_SUBMITTING: Final[str] = "SUBMITTING"
SUBMIT_STATE_SUBMITTED: Final[str] = "SUBMITTED"
SUBMIT_STATE_FAILED: Final[str] = "FAILED"
SUBMIT_STATES: Final[tuple[str, ...]] = (
    SUBMIT_STATE_AUTHORIZED,
    SUBMIT_STATE_SUBMIT_CLAIMED,
    SUBMIT_STATE_SUBMITTING,
    SUBMIT_STATE_SUBMITTED,
    SUBMIT_STATE_FAILED,
)


def validate_passive_stop_policy(
    *,
    stop_loss_pct: Decimal | None,
) -> str | None:
    """Passive lane: a ZERO stop is allowed; range orders keep their own rule.

    The validated passive lane bypasses ONLY the range stop-distance risk
    arithmetic (its risk is the full principal). A zero/missing stop on a
    RANGE order is still "stop distance is unavailable" and must be
    rejected by the caller; this helper exists so the boundary can express
    both branches without duplicating the message.
    """
    if stop_loss_pct is None or not stop_loss_pct.is_finite() or stop_loss_pct < 0:
        return "stop distance is unavailable"
    return None


def validate_submit_claim(
    *,
    state: str | None,
    claim_token: str | None,
    expected_token: str,
) -> str | None:
    """Gate a one-time submit against the persisted state machine.

    ``SUBMITTING`` is entered by an irreversible compare-and-swap inside the
    pre-submit boundary (under the submission lock); every later call with
    the same token — rejected attempt, replay, crash-restart, second service
    instance — finds a state past SUBMIT_CLAIMED and is refused.
    """
    if not expected_token:
        return (
            f"{PASSIVE_LANE} entry authorisation has not been claimed for "
            "this order; the claim token is missing"
        )
    if state is None or state == SUBMIT_STATE_AUTHORIZED:
        return (
            f"{PASSIVE_LANE} entry authorisation has not been claimed for "
            "this order; the claim token is missing or not yet claimed"
        )
    if not claim_token or claim_token != expected_token:
        return (
            f"{PASSIVE_LANE} entry authorisation has not been claimed for "
            "this order; the claim token is missing or forged"
        )
    if state == SUBMIT_STATE_SUBMIT_CLAIMED:
        return None
    if state == SUBMIT_STATE_SUBMITTING:
        return (
            f"{PASSIVE_LANE} one-time entry authorisation is already being "
            "consumed by a submission; replay refused"
        )
    if state == SUBMIT_STATE_SUBMITTED:
        return (
            f"{PASSIVE_LANE} one-time entry authorisation has already been "
            "used to submit a broker order; replay refused"
        )
    if state == SUBMIT_STATE_FAILED:
        return (
            f"{PASSIVE_LANE} one-time entry authorisation was consumed by a "
            "failed submission; a new entry needs a new owner-approved "
            "authorisation row"
        )
    return f"{PASSIVE_LANE} submit state {state!r} is not recognised"


def validate_passive_entry_risk(
    *,
    resolved: ResolvedPassivePolicy,
    quantity: Decimal,
    approved_price: Decimal,
    max_quantity: Decimal,
    max_notional: Decimal,
    commission: Decimal,
) -> str | None:
    """Passive-branch risk check: allotment INCLUDING fees, not stop risk.

    Enforced order: quantity/price validity is the caller's job (shared
    path); here the share cap and the hard notional cap bind first, then
    ``notional + commission <= allotment``. ``resolved`` comes from the
    service-layer resolver and already proves a valid claimed mandate.
    The allotment itself is re-pinned to the owner-approved ceiling here as
    a defence in depth (item 4): a bad row must never buy past $5,000.
    """
    if (
        not resolved.allotment_usd.is_finite()
        or resolved.allotment_usd <= 0
        or resolved.allotment_usd > PASSIVE_ALLOTMENT_USD
    ):
        return (
            f"passive mandate allotment {resolved.allotment_usd} is invalid; "
            f"it must be positive and no greater than {PASSIVE_ALLOTMENT_USD}"
        )
    projected_quantity = quantity
    if projected_quantity > max_quantity:
        return (
            f"projected quantity {projected_quantity} exceeds cap {max_quantity}"
        )
    projected_notional = projected_quantity * approved_price
    if projected_notional > max_notional:
        return (
            f"projected notional {projected_notional} exceeds cap "
            f"{max_notional}"
        )
    if not commission.is_finite() or commission < 0:
        return "passive entry commission must be finite and non-negative"
    notional_with_fees = projected_notional + commission
    if notional_with_fees > resolved.allotment_usd:
        return (
            f"projected notional with fees {notional_with_fees.quantize(Decimal('0.01'))} "
            f"exceeds passive mandate allotment {resolved.allotment_usd}"
        )
    return None



def validate_mandate_for_entry(facts: PassiveMandateFacts) -> str | None:
    """Return the blocking issue for an entry under this mandate, or None."""
    if facts.lane != PASSIVE_LANE:
        return f"mandate lane {facts.lane!r} is not the approved {PASSIVE_LANE} lane"
    if facts.policy_version != POLICY_VERSION:
        return (
            f"mandate policy version {facts.policy_version!r} does not match "
            f"the code policy version {POLICY_VERSION!r}"
        )
    if facts.symbol != PASSIVE_SYMBOL:
        return f"mandate symbol {facts.symbol!r} is not the approved {PASSIVE_SYMBOL}"
    if facts.status != "ACTIVE":
        return f"mandate status {facts.status!r} is not ACTIVE"
    if facts.risk_model is not RiskModel.FULL_PRINCIPAL:
        return f"mandate risk model {facts.risk_model.value!r} is not FULL_PRINCIPAL"
    missing = [name for name in REQUIRED_EXEMPTIONS if name not in facts.exemptions]
    if missing:
        return f"mandate is missing owner-approved exemptions: {', '.join(missing)}"
    if facts.order_binding != PASSIVE_ORDER_BINDING:
        return (
            f"mandate order binding {facts.order_binding!r} is not "
            f"{PASSIVE_ORDER_BINDING!r}; only paper accounts are authorised"
        )
    if not facts.allotment_usd.is_finite():
        return "mandate allotment must be finite"
    if facts.allotment_usd <= 0:
        return f"mandate allotment {facts.allotment_usd} must be positive"
    if facts.allotment_usd > PASSIVE_ALLOTMENT_USD:
        # The owner-approved ceiling is $5,000 including fees. A persisted row
        # can only TIGHTEN the allotment, never widen it (review 2026-09-29,
        # item 4): a $10,000 row under a 25k hard notional cap must not buy
        # $9,600 of SPY.
        return (
            f"mandate allotment {facts.allotment_usd} exceeds the "
            f"owner-approved ceiling {PASSIVE_ALLOTMENT_USD}"
        )
    if facts.review_interval_months != PASSIVE_REVIEW_INTERVAL_MONTHS:
        return (
            "mandate review interval "
            f"{facts.review_interval_months} does not match the approved "
            f"{PASSIVE_REVIEW_INTERVAL_MONTHS} months"
        )
    return None


def size_passive_entry(
    *,
    price: Decimal,
    allotment_usd: Decimal,
    max_quantity: Decimal | int,
    max_notional: Decimal,
    commission: Callable[[Decimal, Decimal], Decimal],
) -> PassiveEntrySizing:
    """Largest integer quantity with ``qty*price + commission <= allotment``.

    The 100-share hard cap and the hard notional cap are applied on top of the
    allotment; they can only shrink the result. ``commission(price, qty)``
    comes from ``app.core.accounting_fees`` in the service layer so this
    module stays pure.
    """
    if not price.is_finite() or price <= 0:
        raise ValueError("price must be finite and greater than zero")
    if not allotment_usd.is_finite() or allotment_usd <= 0:
        raise ValueError("allotment must be finite and greater than zero")
    max_qty_decimal = Decimal(max_quantity)
    if not max_qty_decimal.is_finite() or max_qty_decimal <= 0:
        raise ValueError("max_quantity must be finite and greater than zero")
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError("max_notional must be finite and greater than zero")

    by_allotment = (allotment_usd / price).to_integral_value(rounding=ROUND_FLOOR)
    by_notional = (max_notional / price).to_integral_value(rounding=ROUND_FLOOR)
    ceiling = min(by_allotment, by_notional, max_qty_decimal.to_integral_value())
    capped_by = "allotment"
    if by_notional < by_allotment:
        capped_by = "notional"
    if max_qty_decimal < min(by_allotment, by_notional):
        capped_by = "shares"

    quantity = int(ceiling)
    while quantity > 0:
        qty = Decimal(quantity)
        fee = commission(price, qty)
        if not fee.is_finite() or fee < 0:
            raise ValueError("commission must be finite and non-negative")
        notional = price * qty
        if notional + fee <= allotment_usd and notional <= max_notional:
            return PassiveEntrySizing(
                quantity=quantity,
                notional=notional,
                commission=fee,
                notional_with_fees=notional + fee,
                capped_by=capped_by,
            )
        quantity -= 1
    return PassiveEntrySizing(
        quantity=0,
        notional=Decimal("0"),
        commission=Decimal("0"),
        notional_with_fees=Decimal("0"),
        capped_by=capped_by,
    )
