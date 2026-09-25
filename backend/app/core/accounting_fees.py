"""Accounting-only fee model selection (PREREGISTRATION §9.8).

Pure computation, no I/O. These helpers decide *which fee formula* accounting
and risk use for an order. They never influence trading decisions: entry/exit
guards keep using the configured ``fee_rate`` via ``core/fees.py``.

Forward-only: an order only uses the measured US commission model when its
config snapshot explicitly carries the marker written from deployment of this
model onward. Every order without it — historical rows, reconcile/broker-synced
orders, ``config_snapshot == "{}"`` — reproduces the legacy rate formula
exactly through every path.
"""

from __future__ import annotations

import json
from decimal import Decimal

ACCOUNTING_FEE_MODEL_US_SEC98 = "us-sec98-v1"
SEC98_FIXED_USD = Decimal("1.568")
SEC98_NOTIONAL_RATE = Decimal("0.0000641")


def model_applies(model: str | None, market: str) -> bool:
    """True only for the §9.8 constant on the US market."""
    return model == ACCOUNTING_FEE_MODEL_US_SEC98 and str(market).upper() == "US"


def order_fee(
    *,
    model: str | None,
    market: str,
    price: Decimal,
    quantity: Decimal,
    legacy_rate: Decimal,
) -> Decimal:
    """Fee for one broker order side under the applicable model."""
    if quantity <= 0:
        return Decimal("0")
    if model_applies(model, market):
        return SEC98_FIXED_USD + SEC98_NOTIONAL_RATE * price * quantity
    return price * quantity * legacy_rate


def allocated_entry_fee(
    *,
    model: str | None,
    market: str,
    cost_basis_price: Decimal,
    position_quantity_before: Decimal,
    fill_quantity: Decimal,
    legacy_rate: Decimal,
) -> Decimal:
    """Entry-side fee allocated to an exit fill.

    DECIDED rule: PROPORTIONAL allocation from the remaining position —
    ``order_fee(cost_basis_price, position_quantity_before) *
    fill_quantity / position_quantity_before`` under §9.8. Properties:

    * It matches the FIFO lot allocation in the ledger replay, so a
      remainder closed externally (a broker-synced order with no marker)
      still receives the rest of the entry order's fee through its own
      lot; no piece of the entry commission can be silently lost.
    * A single full close is exact: the allocation equals
      ``order_fee(cost_basis_price, position_quantity_before)``.
    * Several local partial reductions may over-recognise the fixed
      commission, because each re-derives a fresh share of the remaining
      fee pool. The over-charge is bounded by at most one extra fixed
      commission (1.568 USD) per additional partial reduction — e.g.
      100 @ 250 sold 40 then 60 books 1.2682 + 2.5295 = 3.7977 against
      ``order_fee(250, 100)`` = 3.1705. That direction is conservative
      for risk and never under-charges.
    """
    if (
        model_applies(model, market)
        and position_quantity_before > 0
    ):
        return (
            order_fee(
                model=model,
                market=market,
                price=cost_basis_price,
                quantity=position_quantity_before,
                legacy_rate=legacy_rate,
            )
            * fill_quantity
            / position_quantity_before
        )
    return cost_basis_price * fill_quantity * legacy_rate


def model_from_config_snapshot(raw: str | None) -> str | None:
    """Extract the accounting fee model marker from a config snapshot."""
    try:
        parsed = json.loads(raw) if raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("accounting_fee_model")
    if value == ACCOUNTING_FEE_MODEL_US_SEC98:
        return ACCOUNTING_FEE_MODEL_US_SEC98
    return None
