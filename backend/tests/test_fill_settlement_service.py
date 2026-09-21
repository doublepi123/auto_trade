from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import _ensure_fill_settlements_table
from app.domain.fill_settlement import FillFacts
from app.models import Base, FillSettlement
from app.services.fill_settlement_service import (
    FillSettlementConflict,
    FillSettlementLedger,
    SettlementIntent,
)


TEST_DATABASE_URL: Final = f"sqlite:////tmp/fill_settlement_service_{os.getpid()}"
ENTRY: Final = SettlementIntent(
    broker_order_id="order-1",
    facts=FillFacts("AAPL.US", "BUY", Decimal("50"), Decimal("0.1"), "BROKER", "BROKER"),
    first_terminal_status="FILLED",
    tracked_side="long",
    tracked_quantity_after=Decimal("50"),
    tracked_cost_after=Decimal("5"),
)
REDUCTION: Final = replace(
    ENTRY,
    facts=replace(ENTRY.facts, action="SELL"),
    tracked_side=None,
    tracked_quantity_after=Decimal("0"),
    tracked_cost_after=Decimal("0"),
    cost_basis_price=Decimal("0.2"),
    consumed_quantity=Decimal("50"),
    gross_pnl=Decimal("-5"),
    net_pnl=Decimal("-5.5"),
    pnl_source="TRACKED",
)


class TestFillSettlementLedger:
    engine: Engine
    ledger: FillSettlementLedger

    def setup_method(self) -> None:
        self.engine = create_engine(f"{TEST_DATABASE_URL}_{uuid4().hex}.db")
        Base.metadata.create_all(self.engine)
        _ensure_fill_settlements_table(self.engine)
        self.ledger = FillSettlementLedger()

    def teardown_method(self) -> None:
        self.engine.dispose()

    @pytest.mark.parametrize("persist_position", [False, True])
    @pytest.mark.parametrize("offset_hours", [0, 8])
    def test_record_or_get_round_trips_persist_position_and_opened_at(
        self, persist_position: bool, offset_hours: int,
    ) -> None:
        # Given a committed receipt carrying the original position intent.
        opened_at = datetime(2026, 9, 21, 14, 30, 12, 123456, tzinfo=timezone(timedelta(hours=offset_hours)))
        intent = replace(ENTRY, persist_position=persist_position, cost_basis_opened_at=opened_at)
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, intent)
            db.commit()
        # When a new session repeats the fill with different intent metadata.
        with Session(self.engine) as db:
            stored, inserted = self.ledger.record_or_get(db, replace(
                intent, persist_position=not persist_position, cost_basis_opened_at=None,
            ))
            # Then the durable original survives exactly, including its timezone.
            assert inserted is False
            assert (stored.persist_position, stored.cost_basis_opened_at) == (persist_position, opened_at)
            assert not db.dirty

    def test_persist_position_false_round_trips(self) -> None:
        # Given an explicit False even though a positive position remains.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, replace(ENTRY, persist_position=False))
            db.commit()
        # When reading the durable receipt from a fresh session.
        with Session(self.engine) as db:
            stored = self.ledger.get(db, ENTRY.broker_order_id)
            # Then positive quantity never substitutes for the stored decision.
            assert stored is not None
            assert stored.persist_position is False
            assert stored.cost_basis_opened_at is None

    @pytest.mark.parametrize("persist_first", [False, True])
    def test_record_or_get_inserts_once_and_returns_stored_on_repeat(
        self, persist_first: bool,
    ) -> None:
        # Given a receipt, including a pending one with autoflush disabled.
        with Session(self.engine, autoflush=False) as db:
            first, inserted = self.ledger.record_or_get(db, ENTRY)
            assert inserted is True
            if persist_first:
                db.commit()
                db.expunge_all()
            incoming = replace(
                ENTRY, first_terminal_status="CANCELED",
                tracked_quantity_after=Decimal("100"), tracked_cost_after=Decimal("10"),
            )
            # When another terminal status reports the same fill.
            stored, inserted = self.ledger.record_or_get(db, incoming)
            # Then the original accounting result is returned unchanged.
            assert inserted is False
            if not persist_first:
                assert stored is first
            assert stored.tracked_quantity_after == 50.0
            assert stored.tracked_cost_after == 5.0
            assert stored.first_terminal_status == "FILLED"
            db.flush()
            assert db.scalar(select(func.count()).select_from(FillSettlement)) == 1

    def test_record_or_get_raises_conflict_on_incompatible_broker_quantity(self) -> None:
        # Given a durable broker quantity of 50.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, ENTRY)
            db.commit()
        with Session(self.engine) as db:
            incoming = replace(ENTRY, facts=replace(ENTRY.facts, quantity=Decimal("60")))
            # When the broker reports an incompatible quantity.
            with pytest.raises(FillSettlementConflict, match="quantity differs"):
                self.ledger.record_or_get(db, incoming)
            # Then the stored accounting remains intact and the caller can commit.
            db.commit()
        with Session(self.engine) as db:
            stored = self.ledger.get(db, ENTRY.broker_order_id)
            assert stored is not None
            assert stored.booked_quantity == 50.0
            assert stored.tracked_quantity_after == 50.0

    def test_record_or_get_tolerates_broker_correcting_a_fallback_estimate(self) -> None:
        # Given a committed receipt using a fallback price.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, replace(
                ENTRY, facts=replace(ENTRY.facts, price_source="FALLBACK"),
            ))
            db.commit()
        with Session(self.engine) as db:
            # When a later broker price corrects that estimate.
            stored, inserted = self.ledger.record_or_get(db, replace(
                ENTRY, facts=replace(ENTRY.facts, price=Decimal("0.12")),
                tracked_cost_after=Decimal("6"),
            ))
            # Then neither the original price nor cost is overwritten.
            assert inserted is False
            assert stored.booked_price == 0.1
            assert stored.price_source == "FALLBACK"
            assert stored.tracked_cost_after == 5.0
            assert not db.dirty

    def test_record_or_get_never_commits(self) -> None:
        # Given a caller-owned transaction.
        with Session(self.engine) as db:
            # When recording and flushing a receipt before caller rollback.
            self.ledger.record_or_get(db, ENTRY)
            db.flush()
            db.rollback()
        # Then a new session sees no durable receipt.
        with Session(self.engine) as db:
            assert db.scalar(select(func.count()).select_from(FillSettlement)) == 0

    def test_mark_risk_applied_is_idempotent(self) -> None:
        # Given an unapplied reduction receipt.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, REDUCTION)
            db.commit()
            # When risk is marked once, then retried through a different path.
            assert self.ledger.mark_risk_applied(db, REDUCTION.broker_order_id, "LIVE") is True
            db.commit()
        with Session(self.engine) as db:
            stored = self.ledger.get(db, REDUCTION.broker_order_id)
            assert stored is not None
            applied_at = stored.risk_applied_at
            assert self.ledger.mark_risk_applied(db, REDUCTION.broker_order_id, "REPLAY") is False
            # Then the first application marker survives unchanged.
            assert applied_at is not None
            assert stored.risk_applied_at == applied_at
            assert stored.risk_applied_via == "LIVE"
            assert stored.net_pnl == -5.5

    def test_owed_risk_excludes_entries_and_already_applied_rows(self) -> None:
        # Given entries, applied reductions, losses, and zero-PnL reductions.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, ENTRY)
            self.ledger.record_or_get(db, replace(REDUCTION, broker_order_id="applied"))
            self.ledger.record_or_get(db, replace(REDUCTION, broker_order_id="owed"))
            self.ledger.record_or_get(db, replace(
                REDUCTION, broker_order_id="zero", net_pnl=Decimal("0"),
            ))
            self.ledger.mark_risk_applied(db, "applied", "LIVE")
            db.commit()
        with Session(self.engine) as db:
            # When querying risk still owed.
            owed = self.ledger.owed_risk(db)
            # Then only unapplied reductions are returned, including zero PnL.
            assert {row.broker_order_id for row in owed} == {"owed", "zero"}
            loss = next(row for row in owed if row.broker_order_id == "owed")
            assert (loss.cost_basis_price, loss.consumed_quantity) == (0.2, 50.0)
            assert (loss.gross_pnl, loss.net_pnl, loss.pnl_source) == (-5.0, -5.5, "TRACKED")
            assert (loss.tracked_side, loss.tracked_quantity_after, loss.tracked_cost_after) == (None, 0.0, 0.0)

    def test_delete_is_blocked_by_the_append_only_trigger(self) -> None:
        # Given a committed accounting receipt.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, ENTRY)
            db.commit()
            # When deleting it, then the real SQLite trigger rejects the write.
            with pytest.raises(IntegrityError, match="fill_settlements rows cannot be deleted"):
                db.execute(delete(FillSettlement))

    def test_risk_marker_remains_owned_by_caller_transaction(self) -> None:
        # Given a durable receipt that still owes risk.
        with Session(self.engine) as db:
            self.ledger.record_or_get(db, REDUCTION)
            db.commit()
            # When a caller rolls back a flushed risk marker.
            self.ledger.mark_risk_applied(db, REDUCTION.broker_order_id, "LIVE")
            db.flush()
            db.rollback()
        # Then the durable row still owes risk application.
        with Session(self.engine) as db:
            assert [row.broker_order_id for row in self.ledger.owed_risk(db)] == [REDUCTION.broker_order_id]

    def test_get_and_mark_risk_applied_when_receipt_is_missing(self) -> None:
        # Given an empty ledger, when looking up or marking an absent receipt.
        with Session(self.engine) as db:
            # Then no transition or new row is manufactured.
            assert self.ledger.get(db, "missing") is None
            assert self.ledger.mark_risk_applied(db, "missing", "LIVE") is False
