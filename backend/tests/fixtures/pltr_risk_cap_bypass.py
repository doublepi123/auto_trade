"""Replay data for production-paper order 64 (1269649624537280512).

On 2026-08-05, the OPENING_MOMENTUM path bought 1,111 PLTR.US shares at
$164.77 despite its captured entry caps.  The order must now be rejected
before broker submission because it exceeds notional, quantity, and trade-risk
limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal


@dataclass(frozen=True)
class EntrySizingCaps:
    max_position_notional: Decimal
    max_position_quantity: int
    max_risk_per_trade: Decimal
    stop_loss_pct: Decimal


@dataclass(frozen=True)
class AttemptedEntry:
    symbol: str
    side: Literal["BUY"]
    quantity: int
    price: Decimal


@dataclass(frozen=True)
class ExpectedOutcome:
    status: Literal["REJECTED"]


@dataclass(frozen=True)
class RiskCapBypassReplay:
    order_id: int
    broker_order_id: str
    strategy_source: Literal["OPENING_MOMENTUM"]
    caps: EntrySizingCaps
    attempted_entry: AttemptedEntry
    expected_outcome: ExpectedOutcome


PLTR_RISK_CAP_BYPASS_REPLAY: Final = RiskCapBypassReplay(
    order_id=64,
    broker_order_id="1269649624537280512",
    strategy_source="OPENING_MOMENTUM",
    caps=EntrySizingCaps(
        max_position_notional=Decimal("5000.0"),
        max_position_quantity=100,
        max_risk_per_trade=Decimal("250.0"),
        stop_loss_pct=Decimal("1.0"),
    ),
    attempted_entry=AttemptedEntry(
        symbol="PLTR.US",
        side="BUY",
        quantity=1111,
        price=Decimal("164.77"),
    ),
    expected_outcome=ExpectedOutcome(status="REJECTED"),
)
