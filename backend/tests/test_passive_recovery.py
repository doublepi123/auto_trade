"""Phase2a OFF-recovery: inventory, classification, CAS burns (writer W1).

Private SQLite, fake read-only broker/local adapters, CAS-failure
injection. The service never mutates broker state, never creates
authorization, never adopts positions heuristically; missing
table/unreadable DB is HARD (parent reconciliation #1), contradictions
are validated before CLEAR (#2), and CAS losers never clobber a live
winner (#3, bounded single re-read).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import _ensure_passive_mandates_table
from app.domain.passive_allocation import policy as passive_policy
from app.domain.passive_allocation import protocol as passive_protocol
from app.domain.passive_allocation import recovery as recovery_types
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
    POLICY_VERSION,
)
from app.models import Base, PassiveMandate, ReconciliationIncident
from app.services.passive_recovery_service import (
    PassiveRecoveryService,
    PassiveRecoverySnapshot,
)

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
INTENT_QTY = Decimal("8")
_LAST_CALLS: list[str] = []
ORDER_ID = "order-r1"
# The row seeds claim c1 / exec e1 on the first mandate row (id 1).
OWNER_REF = "1:c1:e1"


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now_value = start

    def __call__(self) -> datetime:
        return self.now_value


def _mandate(**overrides: Any) -> PassiveMandate:
    values: dict[str, Any] = dict(
        lane=PASSIVE_LANE,
        policy_version=POLICY_VERSION,
        symbol=PASSIVE_SYMBOL,
        status="ACTIVE",
        allotment_usd=5000.0,
        risk_model="FULL_PRINCIPAL",
        exemptions=",".join(passive_policy.REQUIRED_EXEMPTIONS),
        review_interval_months=6,
        entry_authorisation_available=True,
        approved_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        approved_by="owner",
        approval_reason="owner approval 2026-09-29",
        order_binding="paper-only",
    )
    values.update(overrides)
    return PassiveMandate(**values)


def _intent_json(
    qty: str = "8", price: str = "600",
) -> str:
    intent = passive_protocol.ImmutablePassiveIntent(
        symbol=PASSIVE_SYMBOL,
        side="BUY",
        quantity=Decimal(qty),
        original_price=Decimal(price),
        policy=passive_protocol.PassivePolicySnapshot(
            policy_version=POLICY_VERSION,
            allotment_usd=Decimal("5000"),
            risk_model="FULL_PRINCIPAL",
            exemptions=passive_policy.REQUIRED_EXEMPTIONS,
            order_binding="paper-only",
            review_interval_months=6,
        ),
    )
    return passive_protocol.intent_to_json(intent)


class _Setup:
    """Private DB + service; optional pre-seeded row state."""

    def __init__(self, tmp: Path, rows: list[PassiveMandate] | None) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.engine = create_engine(
            f"sqlite:///{tmp / f'r_{uuid4().hex}.db'}",
            connect_args={"timeout": 30},
        )
        Base.metadata.create_all(self.engine)
        _ensure_passive_mandates_table(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        for row in rows or []:
            with self.sessions() as db:
                db.add(row)
                db.commit()
        self.service = PassiveRecoveryService(
            self.sessions, clock=_Clock(),
        )

    def row(self) -> PassiveMandate | None:
        with self.sessions() as db:
            r = (
                db.query(PassiveMandate)
                .filter(PassiveMandate.lane == PASSIVE_LANE)
                .one_or_none()
            )
            if r is None:
                return None
            db.expunge(r)
            return r

    def set_state(self, **values: Any) -> None:
        with self.sessions() as db:
            db.execute(
                update(PassiveMandate)
                .where(PassiveMandate.lane == PASSIVE_LANE)
                .values(**values)
            )
            db.commit()

    def incidents(self) -> int:
        with self.sessions() as db:
            return (
                db.query(ReconciliationIncident)
                .filter(
                    ReconciliationIncident.failure_category
                    == "PASSIVE_RECOVERY",
                )
                .count()
            )


def _broker_fact(
    status: str | None = "SUBMITTED",
    qty: Decimal | None = None,
    price: Decimal | None = None,
    error: str | None = None,
) -> recovery_types.BrokerOrderFact:
    return recovery_types.BrokerOrderFact(
        broker_order_id=ORDER_ID,
        status=status,
        executed_quantity=qty,
        executed_price=price,
        error=error,
    )


def _local_fact(
    *,
    exists: bool = True,
    symbol: str = PASSIVE_SYMBOL,
    side: str = "BUY",
    qty: Decimal | None = INTENT_QTY,
    lane_ok: bool = True,
    provenance: str | None = OWNER_REF,
) -> recovery_types.LocalOrderFact:
    return recovery_types.LocalOrderFact(
        broker_order_id=ORDER_ID,
        exists=exists,
        symbol=symbol,
        side=side,
        quantity=qty,
        lane_marker_ok=lane_ok,
        provenance_ref=provenance,
    )


def _holding(
    spy_qty: Decimal | None = Decimal("0"),
    tracked_qty: Decimal | None = Decimal("0"),
    tracked_cost: Decimal | None = Decimal("0"),
    others: tuple[str, ...] = (),
) -> recovery_types.HoldingFacts:
    return recovery_types.HoldingFacts(
        broker_spy_qty=spy_qty,
        other_nonzero_symbols=others,
        tracked_spy_qty=tracked_qty,
        tracked_spy_cost=tracked_cost,
    )


def _reconcile(
    setup: _Setup,
    *,
    order: recovery_types.BrokerOrderFact | None,
    local: recovery_types.LocalOrderFact | None,
    holding: recovery_types.HoldingFacts | None,
) -> PassiveRecoverySnapshot:
    inv = setup.service.load_inventory()
    calls: list[str] = []

    def order_status(oid: str) -> recovery_types.BrokerOrderFact:
        calls.append(f"order:{oid}")
        assert order is not None
        return order

    def local_order(oid: str) -> recovery_types.LocalOrderFact:
        calls.append(f"local:{oid}")
        assert local is not None
        return local

    snap = setup.service.reconcile(
        inv,
        order_status=order_status,
        local_order=local_order,
        holding=holding,
    )
    _LAST_CALLS.clear()
    _LAST_CALLS.extend(calls)
    return snap


# ===========================================================================
# Inventory: read errors are HARD (parent reconciliation #1)
# ===========================================================================


class TestInventoryReadErrors:
    def test_missing_table_is_read_error_hard(self, tmp_path: Path) -> None:
        engine = create_engine(
            f"sqlite:///{tmp_path / f'empty_{uuid4().hex}.db'}",
        )
        Base.metadata.create_all(engine)  # schema WITHOUT passive_mandates
        from sqlalchemy import inspect

        with engine.connect() as c:
            c.exec_driver_sql("DROP TABLE IF EXISTS passive_mandates")
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        service = PassiveRecoveryService(sessions, clock=_Clock())
        inv = service.load_inventory()
        assert inv.read_error is not None
        assert inv.rows == ()
        snap = service.preliminary(inv)
        assert snap.complete is False
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols

    def test_unreadable_db_is_read_error(self, tmp_path: Path) -> None:
        class _BoomFactory:
            def __call__(self) -> Any:
                raise OperationalError(
                    "stmt", {}, RuntimeError("disk unreachable"),
                )

        service = PassiveRecoveryService(
            _BoomFactory(),  # type: ignore[arg-type]
            clock=_Clock(),
        )
        inv = service.load_inventory()
        assert inv.read_error is not None
        snap = service.preliminary(inv)
        assert snap.complete is False
        assert snap.hard_reasons

    def test_successful_zero_rows_is_clear_with_zero_reads(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, None)
        inv = setup.service.load_inventory()
        assert inv.read_error is None
        assert inv.rows == ()
        assert inv.needs_recovery is False
        calls: list[str] = []

        def order_status(oid: str) -> recovery_types.BrokerOrderFact:
            calls.append(oid)
            return _broker_fact()

        def local_order(oid: str) -> recovery_types.LocalOrderFact:
            calls.append(oid)
            return _local_fact()

        snap = setup.service.reconcile(
            inv,
            order_status=order_status,
            local_order=local_order,
            holding=None,
        )
        assert snap.complete is True
        assert snap.hard_reasons == ()
        assert _LAST_CALLS == []  # zero external reads for empty verified inventory


# ===========================================================================
# Contradiction validation BEFORE clear (parent reconciliation #2)
# ===========================================================================


class TestContradictionsBeforeClear:
    def test_authorized_with_claim_token_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(claim_token="leak", entry_authorisation_available=True)],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols
        row = setup.row()
        assert row is not None
        assert row.submit_state == "AUTHORIZED"  # no mutation on hard

    def test_authorized_with_consumed_at_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                entry_authorisation_consumed_at=NOW,
                entry_authorisation_available=False,
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons

    def test_no_submit_with_bound_id_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="NO_SUBMIT",
                entry_authorisation_available=False,
                bound_broker_order_id=ORDER_ID,
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons

    def test_valid_never_used_authorized_is_clear(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        inv = setup.service.load_inventory()
        assert inv.needs_recovery is False
        snap = setup.service.preliminary(inv)
        assert snap.hard_reasons == ()
        assert snap.quarantined_symbols == frozenset()
        assert snap.decisions[0].cls == recovery_types.RecoveryClass.CLEAR

    def test_valid_no_submit_is_clear(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="NO_SUBMIT",
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
                failure_reason="denied",
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons == ()
        assert snap.decisions[0].cls == recovery_types.RecoveryClass.CLEAR

    def test_unknown_state_fails_closed(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(submit_state="MYSTERY")],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols

    def test_bad_intent_on_used_state_fails_closed(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="SUBMITTING",
                claim_token="c1",
                execution_token="e1",
                intent_json="{not-json",
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons


# ===========================================================================
# Provable CAS burns (SUBMIT_CLAIMED unowned / CHECKING complete)
# ===========================================================================


class TestProvableBurns:
    def test_submit_claimed_unowned_burns_no_submit(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="SUBMIT_CLAIMED",
                claim_token="c1",
                execution_token=None,
                intent_json=_intent_json(),
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons == ()
        row = setup.row()
        assert row is not None
        assert row.submit_state == "NO_SUBMIT"

    def test_checking_complete_tokens_burns_no_submit(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="CHECKING",
                claim_token="c1",
                execution_token="e1",
                intent_json=_intent_json(),
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons == ()
        row = setup.row()
        assert row is not None
        assert row.submit_state == "NO_SUBMIT"

    def test_checking_incomplete_tokens_is_hard_no_burn(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="CHECKING",
                claim_token="c1",
                execution_token=None,  # incomplete
                intent_json=_intent_json(),
                entry_authorisation_available=False,
            )],
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons
        row = setup.row()
        assert row is not None
        assert row.submit_state == "CHECKING"

    def test_cas_loser_never_clobbers_live_winner(
        self, tmp_path: Path,
    ) -> None:
        """Zero-row burn with a concurrent winner: bounded re-read,
        reclassify once, never overwrite SUBMITTING."""
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="SUBMIT_CLAIMED",
                claim_token="c1",
                execution_token=None,
                intent_json=_intent_json(),
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )
        started = threading.Event()
        finished = threading.Event()

        class _RacingFactory:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __call__(self) -> Session:
                db = self._inner()
                original_execute = db.execute

                def racing_execute(*a: Any, **k: Any) -> Any:
                    stmt = a[0] if a else None
                    if (
                        stmt is not None
                        and str(stmt).startswith("UPDATE passive_mandates")
                        and "NO_SUBMIT" in str(
                            getattr(stmt.compile(), "params", {}),
                        ).replace("'", "")
                    ):
                        # Concurrent winner flips to SUBMITTING before the
                        # burn's zero-row re-read.
                        with setup.sessions() as other:
                            other.execute(
                                update(PassiveMandate)
                                .where(
                                    PassiveMandate.lane == PASSIVE_LANE,
                                )
                                .values(
                                    submit_state="SUBMITTING",
                                    execution_token="winner-exec",
                                )
                            )
                            other.commit()
                        started.set()
                    result = original_execute(*a, **k)
                    finished.set()
                    return result

                db.execute = racing_execute  # type: ignore[method-assign]
                return db

        setup.service._session_factory = _RacingFactory(  # type: ignore[reportPrivateUsage]
            setup.sessions,
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        row = setup.row()
        assert row is not None
        # The winner's SUBMITTING is preserved; the outcome is hard.
        assert row.submit_state == "SUBMITTING"
        assert row.execution_token == "winner-exec"
        assert snap.hard_reasons

    def test_burn_write_failure_is_hard_with_incident(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="SUBMIT_CLAIMED",
                claim_token="c1",
                execution_token=None,
                intent_json=_intent_json(),
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )

        class _FailingFactory:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __call__(self) -> Session:
                db = self._inner()
                original_execute = db.execute

                def failing(*a: Any, **k: Any) -> Any:
                    stmt = a[0] if a else None
                    if (
                        stmt is not None
                        and str(stmt).startswith("UPDATE passive_mandates")
                    ):
                        raise RuntimeError("disk I/O error")
                    return original_execute(*a, **k)

                db.execute = failing  # type: ignore[method-assign]
                return db

        setup.service._session_factory = _FailingFactory(  # type: ignore[reportPrivateUsage]
            setup.sessions,
        )
        snap = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols
        assert setup.incidents() >= 1
        # Never returned cleared.
        row = setup.row()
        assert row is not None
        assert row.submit_state == "SUBMIT_CLAIMED"


# ===========================================================================
# Final classification matrix
# ===========================================================================


def _known_row(**extra: Any) -> PassiveMandate:
    values: dict[str, Any] = dict(
        submit_state="ORDER_KNOWN",
        claim_token="c1",
        execution_token="e1",
        intent_json=_intent_json(),
        bound_broker_order_id=ORDER_ID,
        bound_broker_status="SUBMITTED",
        bound_executed_quantity=None,
        entry_authorisation_available=False,
        entry_authorisation_consumed_at=NOW,
    )
    values.update(extra)
    return _mandate(**values)


class TestFinalClassification:
    @pytest.mark.parametrize("status,qty", [("FILLED", "8"), ("CANCELLED", "0"), ("SUBMITTED", "0")])
    @pytest.mark.parametrize("fault", ["raise", "none", "unknown"])
    def test_observation_failure_is_hard_for_every_progress_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        status: str, qty: str, fault: str,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])

        def fail(*args: Any) -> Any:
            if fault == "raise":
                raise OSError("observation write failed")
            return None if fault == "none" else "APPLIED"

        if fault == "raise":
            from app.services.passive_recovery_service import _RecoveryObservationHooks
            monkeypatch.setattr(_RecoveryObservationHooks, "record_outcome", fail)
        else:
            monkeypatch.setattr(setup.service, "_record_guarded_observation", fail)
        q = Decimal(qty)
        snap = _reconcile(setup, order=_broker_fact(status, q, Decimal("600")),
                          local=_local_fact(), holding=_holding(q, q, q * 600))
        assert snap.hard_reasons
        assert snap.decisions[0].cls == recovery_types.RecoveryClass.HARD_UNCERTAIN
        assert snap.quarantined_symbols == frozenset({PASSIVE_SYMBOL})
        assert setup.incidents() >= 1

    @pytest.mark.parametrize("fault", ["state", "qty", "identity", "read", "raise_read"])
    def test_postwrite_holding_requires_fresh_durable_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        original = setup.service._record_guarded_observation

        def write_then_change(row: Any, fact: Any) -> passive_protocol.OutcomeWriteResult:
            result = original(row, fact)
            if fault == "raise_read":
                def failed_read() -> Any:
                    raise OSError("post-write read failed")
                monkeypatch.setattr(setup.service, "load_inventory", failed_read)
            elif fault == "read":
                monkeypatch.setattr(setup.service, "load_inventory", lambda: recovery_types.PassiveInventory(rows=(), read_error="postread fault"))
            else:
                changes = {"state": {"submit_state": "UNCERTAIN"},
                           "qty": {"bound_executed_quantity": Decimal("7")},
                           "identity": {"execution_token": "other"}}
                setup.set_state(**changes[fault])
            return result

        monkeypatch.setattr(setup.service, "_record_guarded_observation", write_then_change)
        snap = _reconcile(setup, order=_broker_fact("FILLED", Decimal("8"), Decimal("600")),
                          local=_local_fact(), holding=_holding(Decimal("8"), Decimal("8"), Decimal("4800")))
        assert snap.hard_reasons
        assert snap.decisions[0].cls == recovery_types.RecoveryClass.HARD_UNCERTAIN
        assert setup.incidents() >= 1

    def test_forward_then_duplicate_holding_and_sticky_uncertain(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        for _ in range(2):
            snap = _reconcile(setup, order=_broker_fact("FILLED", Decimal("8"), Decimal("600")),
                              local=_local_fact(), holding=_holding(Decimal("8"), Decimal("8"), Decimal("4800")))
            assert not snap.hard_reasons
            assert snap.decisions[0].cls == recovery_types.RecoveryClass.HOLDING_CONFIRMED
        setup.set_state(submit_state="UNCERTAIN")
        snap = _reconcile(setup, order=_broker_fact("FILLED", Decimal("8"), Decimal("600")),
                          local=_local_fact(), holding=_holding(Decimal("8"), Decimal("8"), Decimal("4800")))
        assert snap.hard_reasons
        row = setup.row()
        assert row is not None and row.submit_state == "UNCERTAIN"

    @pytest.mark.parametrize("previous_status,observed_price", [
        ("CANCELLED", "600"), ("FILLED", "600.01"),
    ])
    def test_durable_conflict_cannot_advertise_confirmed_holding(
        self, tmp_path: Path, previous_status: str, observed_price: str,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row(
            bound_broker_status=previous_status,
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("600"),
        )])
        snap = _reconcile(
            setup, order=_broker_fact("FILLED", Decimal("8"), Decimal(observed_price)),
            local=_local_fact(),
            holding=_holding(Decimal("8"), Decimal("8"), Decimal("4800")),
        )
        row = setup.row()
        assert row is not None and row.submit_state == "UNCERTAIN"
        assert snap.hard_reasons, "durable UNCERTAIN must not advertise a clear snapshot"
        assert snap.decisions[0].cls == recovery_types.RecoveryClass.HARD_UNCERTAIN
        assert PASSIVE_SYMBOL in snap.quarantined_symbols
        assert setup.incidents() >= 1

    def test_submitting_no_id_is_hard_no_adoption(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="SUBMITTING",
                claim_token="c1",
                execution_token="e1",
                intent_json=_intent_json(),
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )
        snap = _reconcile(setup, order=None, local=None, holding=None)
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols

    def test_order_known_full_proof_holding_confirmed(
        self, tmp_path: Path,
    ) -> None:
        qty, price = Decimal("6"), Decimal("619.90")
        setup = _Setup(tmp_path, [_known_row(bound_broker_status="FILLED")])
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", qty, price),
            local=_local_fact(),
            holding=_holding(
                spy_qty=qty,
                tracked_qty=qty,
                tracked_cost=qty * price,
            ),
        )
        assert snap.hard_reasons == ()
        decision = snap.decisions[0]
        assert decision.cls == recovery_types.RecoveryClass.HOLDING_CONFIRMED
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == qty
        assert row.submit_state == "ORDER_KNOWN"

    def test_order_known_wrong_provenance_is_hard(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("SUBMITTED"),
            local=_local_fact(provenance="someone-else"),
            holding=_holding(),
        )
        assert snap.hard_reasons
        assert PASSIVE_SYMBOL in snap.quarantined_symbols

    def test_order_known_wrong_qty_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("SUBMITTED"),
            local=_local_fact(qty=Decimal("7")),
            holding=_holding(),
        )
        assert snap.hard_reasons

    def test_order_known_missing_local_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("SUBMITTED"),
            local=_local_fact(exists=False),
            holding=_holding(),
        )
        assert snap.hard_reasons

    def test_order_known_no_lane_marker_is_hard(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("SUBMITTED"),
            local=_local_fact(lane_ok=False),
            holding=_holding(),
        )
        assert snap.hard_reasons

    def test_broker_overfill_is_hard_observation_preserved(
        self, tmp_path: Path,
    ) -> None:
        """qty 9 > intent 8: UNCERTAIN via guarded outcome; the known id is
        retained, canonical bound qty stays empty, observation preserved."""
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="FILLED")],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", Decimal("9"), Decimal("620")),
            local=_local_fact(),
            holding=_holding(
                spy_qty=Decimal("9"),
                tracked_qty=Decimal("9"),
                tracked_cost=Decimal("9") * Decimal("620"),
            ),
        )
        assert snap.hard_reasons
        decision = snap.decisions[0]
        assert decision.cls == recovery_types.RecoveryClass.HARD_UNCERTAIN
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_broker_order_id == ORDER_ID  # known id retained
        assert row.bound_executed_quantity is None  # canonical stays empty
        assert row.uncertainty_reason and "9" in row.uncertainty_reason

    def test_unknown_broker_status_is_hard(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("MYSTERY"),
            local=_local_fact(),
            holding=_holding(),
        )
        assert snap.hard_reasons

    def test_broker_read_error_never_mutates_mandate(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact(error="timeout"),
            local=_local_fact(),
            holding=_holding(),
        )
        assert snap.hard_reasons
        row = setup.row()
        assert row is not None
        assert row.submit_state == "ORDER_KNOWN"  # untouched

    def test_live_status_is_order_live_with_restore(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, [_known_row()])
        snap = _reconcile(
            setup,
            order=_broker_fact("SUBMITTED"),
            local=_local_fact(),
            holding=_holding(),
        )
        assert snap.order_live is True
        decision = snap.decisions[0]
        assert decision.cls == recovery_types.RecoveryClass.ORDER_LIVE
        assert PASSIVE_SYMBOL in snap.quarantined_symbols
        # Preliminary offered a restore_ref the runner must authenticate.
        prelim = setup.service.preliminary(
            setup.service.load_inventory(),
        )
        assert prelim.pending_refs.get(ORDER_ID)

    def test_terminal_no_fill_requires_explicit_zero_and_holding_zero(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="REJECTED")],
        )
        # explicit qty 0 + broker SPY 0 => TERMINAL_NO_FILL
        snap = _reconcile(
            setup,
            order=_broker_fact("REJECTED", Decimal("0"), None),
            local=_local_fact(),
            holding=_holding(
                spy_qty=Decimal("0"),
                tracked_qty=Decimal("0"),
                tracked_cost=Decimal("0"),
            ),
        )
        assert snap.hard_reasons == ()
        assert snap.decisions[0].cls == (
            recovery_types.RecoveryClass.TERMINAL_NO_FILL
        )
        # missing qty isn't zero => HARD
        setup2 = _Setup(
            tmp_path / "b",
            [_known_row(bound_broker_status="REJECTED")],
        )
        snap2 = _reconcile(
            setup2,
            order=_broker_fact("REJECTED", None, None),
            local=_local_fact(),
            holding=_holding(),
        )
        assert snap2.hard_reasons
        # broker holding nonzero => HARD
        setup3 = _Setup(
            tmp_path / "c",
            [_known_row(bound_broker_status="REJECTED")],
        )
        snap3 = _reconcile(
            setup3,
            order=_broker_fact("REJECTED", Decimal("0"), None),
            local=_local_fact(),
            holding=_holding(spy_qty=Decimal("3")),
        )
        assert snap3.hard_reasons

    def test_missing_holding_is_hard_not_empty(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="FILLED")],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", Decimal("6"), Decimal("619.90")),
            local=_local_fact(),
            holding=None,
        )
        assert snap.hard_reasons
        assert "holding" in snap.hard_reasons[0].lower()

    def test_cost_mismatch_is_hard(self, tmp_path: Path) -> None:
        qty, price = Decimal("6"), Decimal("619.90")
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="FILLED")],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", qty, price),
            local=_local_fact(),
            holding=_holding(
                spy_qty=qty,
                tracked_qty=qty,
                tracked_cost=qty * price + Decimal("5"),  # out of tolerance
            ),
        )
        assert snap.hard_reasons

    def test_unit_avg_mistaken_as_total_is_hard(
        self, tmp_path: Path,
    ) -> None:
        """A tracked cost equal to the UNIT average (not the TOTAL) must be
        rejected: 619.90 != 6*619.90."""
        qty, price = Decimal("6"), Decimal("619.90")
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="FILLED")],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", qty, price),
            local=_local_fact(),
            holding=_holding(
                spy_qty=qty,
                tracked_qty=qty,
                tracked_cost=price,  # unit average, not total
            ),
        )
        assert snap.hard_reasons

    def test_other_positions_is_hard(self, tmp_path: Path) -> None:
        qty, price = Decimal("6"), Decimal("619.90")
        setup = _Setup(
            tmp_path,
            [_known_row(bound_broker_status="FILLED")],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", qty, price),
            local=_local_fact(),
            holding=_holding(
                spy_qty=qty,
                tracked_qty=qty,
                tracked_cost=qty * price,
                others=("TSLA.US",),
            ),
        )
        assert snap.hard_reasons

    def test_uncertain_sticky_no_auto_clear(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="UNCERTAIN",
                claim_token="c1",
                execution_token="e1",
                intent_json=_intent_json(),
                bound_broker_order_id=ORDER_ID,
                bound_broker_status="SUBMITTED",
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
                uncertainty_reason="prior doubt",
            )],
        )
        snap = _reconcile(
            setup,
            order=_broker_fact("FILLED", Decimal("8"), Decimal("620")),
            local=_local_fact(),
            holding=_holding(
                spy_qty=Decimal("8"),
                tracked_qty=Decimal("8"),
                tracked_cost=Decimal("8") * Decimal("620"),
            ),
        )
        assert snap.hard_reasons
        row = setup.row()
        assert row is not None
        # Ordinary receipt cannot clear UNCERTAIN.
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN


# ===========================================================================
# Recovery-only observation bundle denies entry, permits observation
# ===========================================================================


class TestRecoveryObservationBundle:
    def _bundle(self, setup: _Setup) -> Any:
        from app.services.passive_recovery_service import (
            _RecoveryObservationHooks,
        )

        return _RecoveryObservationHooks(setup.sessions, _Clock())

    def test_begin_execution_always_denied(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        bundle = self._bundle(setup)
        result = bundle.begin_execution(
            passive_protocol.PassiveAttemptRef(
                mandate_id=1, claim_token="c1",
            ),
            "exec-x",
        )
        assert isinstance(result, passive_protocol.PassiveRejection)

    def test_resolve_policy_denied(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        bundle = self._bundle(setup)
        owner = passive_protocol.PassiveOwner(
            ref=passive_protocol.PassiveAttemptRef(
                mandate_id=1, claim_token="c1",
            ),
            execution_token="e1",
            intent=passive_protocol.intent_from_json(_intent_json()),
        )
        result = bundle.resolve_policy(
            owner,
            passive_protocol.PassiveOrderSpec(
                symbol=PASSIVE_SYMBOL, side="BUY",
                quantity=INTENT_QTY, price=Decimal("600"),
            ),
        )
        assert isinstance(result, passive_protocol.PassiveRejection)

    def test_claim_submission_denied(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        bundle = self._bundle(setup)
        owner = passive_protocol.PassiveOwner(
            ref=passive_protocol.PassiveAttemptRef(
                mandate_id=1, claim_token="c1",
            ),
            execution_token="e1",
            intent=passive_protocol.intent_from_json(_intent_json()),
        )
        cash: Any = _FakeCash()
        assert bundle.claim_submission(
            owner,
            passive_protocol.PassiveOrderSpec(
                symbol=PASSIVE_SYMBOL, side="BUY",
                quantity=INTENT_QTY, price=Decimal("600"),
            ),
            cash,
        ) is False

    def test_gate_always_reports_disabled(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        bundle = self._bundle(setup)
        assert bundle.current_gate_issue() is not None

    def test_record_outcome_works_feature_off(self, tmp_path: Path) -> None:
        """Observation works even with the lane flag OFF (the gate denies
        ENTRY, not observation)."""
        setup = _Setup(
            tmp_path,
            [_mandate(
                submit_state="ORDER_KNOWN",
                claim_token="c1",
                execution_token="e1",
                intent_json=_intent_json(),
                bound_broker_order_id=ORDER_ID,
                bound_broker_status="SUBMITTED",
                entry_authorisation_available=False,
                entry_authorisation_consumed_at=NOW,
            )],
        )
        bundle = self._bundle(setup)
        owner = passive_protocol.PassiveOwner(
            ref=passive_protocol.PassiveAttemptRef(
                mandate_id=1, claim_token="c1",
            ),
            execution_token="e1",
            intent=passive_protocol.intent_from_json(_intent_json()),
        )
        result = bundle.record_outcome(
            owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id=ORDER_ID,
                broker_status="SUBMITTED",
            ),
        )
        assert result == passive_protocol.OutcomeWriteResult.IDEMPOTENT

    def test_owner_intent_for_propagates_db_errors(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, [_mandate()])
        bundle = self._bundle(setup)

        class _Boom:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

            def get(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("db down")

        bundle._session_factory = lambda: _Boom()  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            bundle.owner_intent_for(1, "c1")


class _FakeCash:
    __slots__ = (
        "amount", "currency", "request_started_at",
        "request_completed_at", "provenance",
    )

    def __init__(self) -> None:
        self.amount = Decimal("10000")
        self.currency = "USD"
        self.request_started_at = NOW - timedelta(seconds=1)
        self.request_completed_at = NOW - timedelta(seconds=0)
        self.provenance = passive_protocol.PASSIVE_CASH_PROVENANCE
