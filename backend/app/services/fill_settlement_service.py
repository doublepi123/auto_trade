from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.domain.fill_settlement import FillFacts, RepeatVerdict, compare_repeat
from app.models import FillSettlement


class FillSettlementConflict(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SettlementIntent:
    broker_order_id: str
    facts: FillFacts
    first_terminal_status: str
    tracked_side: str | None
    tracked_quantity_after: Decimal
    tracked_cost_after: Decimal
    cost_basis_price: Decimal | None = None
    consumed_quantity: Decimal | None = None
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    pnl_source: str | None = None
    persist_position: bool = False
    cost_basis_opened_at: datetime | None = None


class FillSettlementLedger:
    """Record accounting receipts inside the caller's accounting transaction."""

    def record_or_get(self, db: Session, intent: SettlementIntent) -> tuple[FillSettlement, bool]:
        stored = self.get(db, intent.broker_order_id)
        if stored is not None:
            comparison = compare_repeat(
                FillFacts(
                    symbol=stored.symbol,
                    action=stored.action,
                    quantity=Decimal(str(stored.booked_quantity)),
                    price=Decimal(str(stored.booked_price)),
                    quantity_source=stored.quantity_source,
                    price_source=stored.price_source,
                ),
                intent.facts,
            )
            match comparison.verdict:
                case RepeatVerdict.MATCH | RepeatVerdict.TOLERATED_FALLBACK:
                    return stored, False
                case RepeatVerdict.CONFLICT:
                    raise FillSettlementConflict(comparison.reason)
                case unreachable:
                    assert_never(unreachable)
        row = FillSettlement(
            broker_order_id=intent.broker_order_id,
            symbol=intent.facts.symbol,
            action=intent.facts.action,
            booked_quantity=float(intent.facts.quantity),
            booked_price=float(intent.facts.price),
            quantity_source=intent.facts.quantity_source,
            price_source=intent.facts.price_source,
            first_terminal_status=intent.first_terminal_status,
            tracked_side=intent.tracked_side,
            tracked_quantity_after=float(intent.tracked_quantity_after),
            tracked_cost_after=float(intent.tracked_cost_after),
            cost_basis_price=(
                float(intent.cost_basis_price) if intent.cost_basis_price is not None else None
            ),
            consumed_quantity=(
                float(intent.consumed_quantity) if intent.consumed_quantity is not None else None
            ),
            gross_pnl=float(intent.gross_pnl) if intent.gross_pnl is not None else None,
            net_pnl=float(intent.net_pnl) if intent.net_pnl is not None else None,
            pnl_source=intent.pnl_source,
            persist_position=intent.persist_position,
            cost_basis_opened_at=(
                intent.cost_basis_opened_at.astimezone(timezone.utc)
                if intent.cost_basis_opened_at is not None else None
            ),
        )
        db.add(row)
        return row, True

    def get(self, db: Session, key: str) -> FillSettlement | None:
        # SessionLocal disables autoflush; pending receipts are not in its identity map.
        for pending in db.new:
            if isinstance(pending, FillSettlement) and pending.broker_order_id == key:
                return pending
        row = db.get(FillSettlement, key)
        if row is not None and row.cost_basis_opened_at is not None:
            if row.cost_basis_opened_at.tzinfo is None:
                # SQLite drops timezone metadata; writes above are always UTC.
                # Restore the loaded value without scheduling an accounting UPDATE.
                set_committed_value(
                    row, "cost_basis_opened_at",
                    row.cost_basis_opened_at.replace(tzinfo=timezone.utc),
                )
        return row

    def mark_risk_applied(self, db: Session, key: str, via: str) -> bool:
        row = self.get(db, key)
        if row is None or row.risk_applied_at is not None:
            return False
        row.risk_applied_at = datetime.now(timezone.utc)
        row.risk_applied_via = via
        return True

    def owed_risk(self, db: Session) -> list[FillSettlement]:
        return list(db.scalars(select(FillSettlement).where(
            FillSettlement.net_pnl.is_not(None),
            FillSettlement.risk_applied_at.is_(None),
        )))
