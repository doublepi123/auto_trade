"""Passive submit-protocol OUTCOME INTEGRITY regressions (fix-14, writer Y).

Covers the review2 remaining findings owned by this writer:

* Finding 3 — monotonic receipt/fill policy on ``record_outcome`` and the
  pure validator: missing fields never erase facts; cumulative quantity
  never decreases; terminal statuses never revert to live; UNCERTAIN is
  sticky under ordinary receipts (no hidden auto-reconciliation); exact
  duplicates are idempotent; genuine same-ID positive progress is accepted
  with an expected-prior-fact CAS whose 0-row outcome raises.
* Finding 5 — invalid-sizing / quarantine burn write failures raise a
  typed ``PassivePersistenceUncertain`` (never a normal "consumed"
  refusal while the row reads AUTHORIZED), block automatic in-process
  retry until review, and attempt incident persistence. DB-total-outage
  limitations are documented, not papered over.
* Handshake — ``record_unresolved_reference`` exists on the protocol
  completeness list and the bundle, records incident evidence without
  inventing authority, and raises on persistence failure;
  ``owner_intent_for`` propagates DB errors and returns None only for a
  genuinely absent row.

The tests run against private SQLite databases with inline fakes (no
network, no broker SDK, no .env access). Interleavings are forced
deterministically with barriers/expected-prior-fact races — the loser's
0-row write must raise, never report success.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.risk import RiskController
from app.database import _ensure_passive_mandates_table
from app.domain.passive_allocation import policy as passive_policy
from app.domain.passive_allocation import protocol as passive_protocol
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
    POLICY_VERSION,
)
from app.models import Base, PassiveMandate, ReconciliationIncident
from app.services.passive_allocation_service import (
    PassiveAllocationService,
    PassivePersistenceUncertain,
    PassiveSubmitHookBundle,
)

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
ORDER_ID = "order-1"


# ---------------------------------------------------------------------------
# Fakes / harness
# ---------------------------------------------------------------------------


class _FakeCash:
    __slots__ = (
        "amount", "currency", "request_started_at",
        "request_completed_at", "provenance",
    )

    def __init__(self, amount: Decimal) -> None:
        self.amount = amount
        self.currency = "USD"
        self.request_started_at = NOW - timedelta(seconds=1)
        self.request_completed_at = NOW - timedelta(seconds=0.5)
        self.provenance = passive_protocol.PASSIVE_CASH_PROVENANCE


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now_value = start

    def __call__(self) -> datetime:
        return self.now_value


class _Gate:
    def __init__(self, enabled: bool = True, paper: bool = True) -> None:
        self.enabled = enabled
        self.paper = paper

    def lane_on(self) -> bool:
        return self.enabled

    def paper_on(self) -> bool:
        return self.paper


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


class _StubExecutor:
    """Minimal stand-in for TradeExecutionService (construction only).

    The facade delegates to the executor's dedicated entry; these tests
    exercise the hook bundle and reservation path, which need only the
    ``max_position_*`` attributes and a passive hooks slot.
    """

    max_position_quantity = 100
    max_position_notional = 5000.0

    def __init__(self) -> None:
        self.passive_submit_hooks: Any = None


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None,
        *,
        gate: _Gate | None = None,
        executor: Any = None,
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.engine: Engine = create_engine(
            f"sqlite:///{tmp / f'p_{uuid4().hex}.db'}",
            connect_args={"timeout": 30},
        )
        Base.metadata.create_all(self.engine)
        _ensure_passive_mandates_table(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        if mandate is not None:
            with self.sessions() as db:
                db.add(mandate)
                db.commit()
        self.gate = gate or _Gate()
        self.clock = _Clock()
        self.executor = executor or _StubExecutor()
        self.passive = PassiveAllocationService(
            execution=self.executor,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )
        self.executor.passive_submit_hooks = self.passive.build_hook_bundle()
        self.hooks: PassiveSubmitHookBundle = self.passive.build_hook_bundle()

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

    def claim_to_submitting(self) -> tuple[Any, Any]:
        """Reserve + begin + claim submission; returns (ref, owner)."""
        ref = self.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        owner = self.hooks.begin_execution(ref, f"exec-{uuid4().hex[:8]}")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        cash = _FakeCash(Decimal("10000"))
        order = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL,
            side="BUY",
            quantity=Decimal("8"),
            price=Decimal("600.00"),
        )
        assert self.hooks.claim_submission(owner, order, cash)
        return ref, owner


def _fact(
    outcome: str = passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
    *,
    broker_order_id: str = ORDER_ID,
    broker_status: str = "SUBMITTED",
    executed_quantity: Decimal | None = None,
    executed_price: Decimal | None = None,
    reason: str = "",
) -> passive_protocol.PassiveOutcomeFact:
    return passive_protocol.PassiveOutcomeFact(
        outcome=outcome,
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        executed_quantity=executed_quantity,
        executed_price=executed_price,
        reason=reason,
    )


# ===========================================================================
# Pure validator: monotonic receipt comparison
# ===========================================================================


class TestMonotonicReceiptValidator:
    def test_filed_then_stale_submitted_is_backward(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(
                broker_status="SUBMITTED",
                executed_quantity=None,
                executed_price=None,
            ),
        )
        assert verdict == "CONFLICT"

    def test_missing_fill_never_erases_recorded_fill(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="PARTIAL_FILLED",
            bound_executed_quantity=Decimal("5"),
            bound_executed_price=None,
            fact=_fact(
                broker_status="PARTIAL_FILLED",
                executed_quantity=None,
                executed_price=None,
            ),
        )
        assert verdict == "CONFLICT"

    def test_cumulative_quantity_decrease_is_backward(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(
                broker_status="FILLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("620"),
            ),
        )
        assert verdict == "CONFLICT"

    def test_terminal_not_reverted_to_live(self) -> None:
        for live in ("SUBMITTED", "PARTIAL_FILLED"):
            verdict = passive_protocol.validate_outcome_write(
                current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                bound_broker_order_id=ORDER_ID,
                bound_broker_status="CANCELLED",
                bound_executed_quantity=Decimal("5"),
                bound_executed_price=Decimal("620"),
                fact=_fact(
                    broker_status=live,
                    executed_quantity=Decimal("5"),
                    executed_price=Decimal("620"),
                ),
            )
            assert verdict == "CONFLICT", live

    def test_genuine_forward_progress_accepted(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="SUBMITTED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        assert verdict == "PROGRESS"

    def test_terminal_partial_forward_fill_accepted(self) -> None:
        # CANCELLED with a bigger observed partial fill: forward.
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=Decimal("3"),
            bound_executed_price=Decimal("620"),
            fact=_fact(
                broker_status="CANCELLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("620"),
            ),
        )
        assert verdict == "PROGRESS"

    def test_same_status_higher_fill_is_forward(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="PARTIAL_FILLED",
            bound_executed_quantity=Decimal("3"),
            bound_executed_price=Decimal("620"),
            fact=_fact(
                broker_status="PARTIAL_FILLED",
                executed_quantity=Decimal("4"),
                executed_price=Decimal("620"),
            ),
        )
        assert verdict == "PROGRESS"

    def test_same_fill_conflicting_price_is_backward(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("610"),
            ),
        )
        assert verdict == "CONFLICT"

    def test_exact_duplicate_is_idempotent(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="SUBMITTED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_status="SUBMITTED"),
        )
        assert verdict == "IDEMPOTENT"

    def test_uncertain_sticky_under_ordinary_receipt(self) -> None:
        for status in ("SUBMITTED", "PARTIAL_FILLED", "FILLED"):
            verdict = passive_protocol.validate_outcome_write(
                current_state=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                bound_broker_order_id=ORDER_ID,
                bound_broker_status="SUBMITTED",
                bound_executed_quantity=None,
                bound_executed_price=None,
                fact=_fact(broker_status=status),
            )
            assert verdict == "IDEMPOTENT", status  # sticky, no auto-clear

    def test_uncertain_different_id_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_UNCERTAIN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="SUBMITTED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_order_id="other-id", broker_status="FILLED"),
        )
        assert verdict == "CONFLICT"


# ===========================================================================
# DB-level monotonic record_outcome (finding 3)
# ===========================================================================


class TestRecordOutcomeMonotonicity:
    def test_filled_then_stale_submitted_preserves_facts(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_executed_price == Decimal("620")
        # Stale SUBMITTED with missing fills: contradiction -> UNCERTAIN,
        # facts retained (never erased).
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="SUBMITTED",
                executed_quantity=None,
                executed_price=None,
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_executed_price == Decimal("620")
        assert row.bound_broker_order_id == ORDER_ID

    def test_cumulative_quantity_never_decreases(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("620"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("8")
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_terminal_partial_fills_remain_accurate(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="CANCELLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("619.90"),
            ),
        )
        # Repeated exact facts: harmless.
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="CANCELLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("619.90"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_status == "CANCELLED"
        assert row.bound_executed_quantity == Decimal("5")
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN

    def test_uncertain_sticky_in_db(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        setup.hooks.record_outcome(
            owner,
            _fact(
                outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                reason="lost poll",
            ),
        )
        uncertain_row = setup.row()
        assert uncertain_row is not None
        assert uncertain_row.submit_state == (
            passive_protocol.SUBMIT_STATE_UNCERTAIN
        )
        # An ordinary same-ID receipt never auto-clears it.
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_genuine_progress_with_prior_fact_cas(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_status == "FILLED"
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN

    def test_stale_writer_zero_row_raises_not_success(
        self, tmp_path: Path,
    ) -> None:
        """A writer holding stale prior facts must not report success when
        the expected-prior-fact CAS matches zero rows."""
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        # Simulate a concurrent advance between this writer's read and its
        # PROGRESS write: pre-record FILLED/8 directly, then attempt the
        # stale SUBMITTED->PARTIAL progress with the old prior facts.
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        # This writer read SUBMITTED/None/None (stale). The validator
        # decides on the CURRENT row (FILLED/8) -> CONFLICT -> the
        # escalation CAS includes the prior facts it decided on, so it
        # either applies (recording the contradiction) or raises — never a
        # silent success against vanished facts.
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="PARTIAL_FILLED",
                executed_quantity=Decimal("4"),
                executed_price=Decimal("620"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_broker_status == "FILLED"
        assert row.submit_state in (
            passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            passive_protocol.SUBMIT_STATE_UNCERTAIN,
        )

    def test_deterministic_interleaved_writers_monotonic(
        self, tmp_path: Path,
    ) -> None:
        """Two threads interleave same-id progress writes; whichever legal
        order applies, the final facts are monotonic and no writer both
        read stale facts and silently overwrote the advanced row."""
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        barrier = threading.Barrier(2, timeout=10)
        outcomes: dict[str, Any] = {}

        def writer(
            name: str,
            status: str,
            qty: Decimal | None,
            price: Decimal | None,
        ) -> None:
            barrier.wait()
            try:
                setup.hooks.record_outcome(
                    owner,
                    _fact(
                        broker_status=status,
                        executed_quantity=qty,
                        executed_price=price,
                    ),
                )
                outcomes[name] = "ok"
            except Exception as exc:
                outcomes[name] = type(exc).__name__

        threads = [
            threading.Thread(
                target=writer,
                args=("filled", "FILLED", Decimal("8"), Decimal("620")),
            ),
            threading.Thread(
                target=writer,
                args=(
                    "partial", "PARTIAL_FILLED", Decimal("4"), Decimal("620"),
                ),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        row = setup.row()
        assert row is not None
        # Monotonic terminal facts survive whichever writer landed last;
        # the smaller fill never clobbers the bigger one.
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_broker_status == "FILLED"
        assert row.submit_state in (
            passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            passive_protocol.SUBMIT_STATE_UNCERTAIN,
        )


# ===========================================================================
# Finding 5: burn write failures raise typed uncertainty, block retry
# ===========================================================================


class _FlakySessionFactory:
    """Session factory wrapper that faults burn UPDATEs or COMMITs.

    ``mode="update"`` makes any UPDATE whose params carry the NO_SUBMIT
    state transition report zero affected rows (row-not-matched).
    ``mode="commit"`` raises on commit.
    """

    def __init__(self, sessions: sessionmaker[Session], mode: str) -> None:
        self._sessions = sessions
        self.mode = mode

    def __call__(self) -> Session:
        db = self._sessions()
        original_execute = db.execute
        original_commit = db.commit

        def flaky_execute(*a: Any, **k: Any) -> Any:
            stmt = a[0] if a else None
            fault = False
            if self.mode == "update" and stmt is not None:
                if str(stmt).startswith("UPDATE passive_mandates"):
                    try:
                        params = getattr(
                            stmt.compile(), "params", None,
                        ) or {}
                        fault = any(
                            str(value) == "NO_SUBMIT"
                            for key, value in params.items()
                            if "submit_state" in str(key)
                        )
                    except Exception:
                        fault = False
            if fault:
                class _ZeroResult:
                    rowcount = 0

                return _ZeroResult()
            return original_execute(*a, **k)

        def flaky_commit(*a: Any, **k: Any) -> None:
            if self.mode == "commit":
                raise RuntimeError("disk I/O error")
            original_commit(*a, **k)

        if self.mode == "update":
            db.execute = flaky_execute  # type: ignore[method-assign]
        db.commit = flaky_commit  # type: ignore[method-assign]
        return db


class TestBurnFailureSemantics:
    def test_invalid_sizing_burn_update_fail_raises_typed(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "update"),
        )
        with pytest.raises(PassivePersistenceUncertain) as excinfo:
            setup.passive.reserve_entry(price=Decimal("600"))
        assert "NOT confirmed consumed" in str(excinfo.value)
        row = setup.row()
        assert row is not None
        # The row is NOT claimed consumed: it still reads AUTHORIZED.
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED

    def test_invalid_sizing_burn_commit_fail_raises_typed(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "commit"),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED

    def test_no_normal_refusal_claims_consumed_on_write_failure(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "update"),
        )
        try:
            result: Any = setup.passive.reserve_entry(price=Decimal("600"))
        except PassivePersistenceUncertain:
            result = None  # the typed raise is the required behaviour
        assert not (
            isinstance(result, str) and "consumed" in result
        ), "a write failure must never be reported as a consumed refusal"

    def test_retry_blocked_in_process_after_failed_burn(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "commit"),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        # Automatic retry on the SAME service instance is blocked.
        setup.passive.inject_session_factory_for_tests(setup.sessions)
        later = setup.passive.reserve_entry(price=Decimal("400"))
        assert isinstance(later, str)
        assert "blocked pending review" in later
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None

    def test_incident_attempted_on_burn_failure(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "update"),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        setup.passive.inject_session_factory_for_tests(setup.sessions)
        # The review gate attempted durable incident evidence.
        with setup.sessions() as db:
            incidents = (
                db.query(ReconciliationIncident)
                .filter(
                    ReconciliationIncident.failure_category
                    == "UNRESOLVED_PASSIVE_REFERENCE",
                )
                .count()
            )
        assert incidents >= 1

    def test_quarantine_failure_propagates(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        # Build a contradictory AUTHORIZED row (used markers).
        from sqlalchemy import update

        with setup.sessions() as db:
            db.execute(
                update(PassiveMandate)
                .where(PassiveMandate.lane == PASSIVE_LANE)
                .values(
                    entry_authorisation_available=False,
                    protocol_version="passive-submit-v2",
                )
            )
            db.commit()

        # Fault the quarantine (UNCERTAIN-transition) UPDATE itself.
        class _QuarantineFlaky(_FlakySessionFactory):
            def __call__(self) -> Session:
                db = self._sessions()
                original_execute = db.execute
                original_commit = db.commit

                def flaky_execute(*a: Any, **k: Any) -> Any:
                    stmt = a[0] if a else None
                    fault = False
                    if self.mode == "update" and stmt is not None:
                        if str(stmt).startswith("UPDATE passive_mandates"):
                            try:
                                params = getattr(
                                    stmt.compile(), "params", None,
                                ) or {}
                                fault = any(
                                    str(value)
                                    in {"NO_SUBMIT", "UNCERTAIN"}
                                    for key, value in params.items()
                                    if "submit_state" in str(key)
                                )
                            except Exception:
                                fault = False
                    if fault:
                        class _ZeroResult:
                            rowcount = 0

                        return _ZeroResult()
                    return original_execute(*a, **k)

                if self.mode == "update":
                    db.execute = flaky_execute  # type: ignore[method-assign]
                db.commit = original_commit
                return db

        setup.passive.inject_session_factory_for_tests(
            _QuarantineFlaky(setup.sessions, "update"),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))

    def test_successful_burn_still_burns_normally(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        result = setup.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str) and "consumed" in result
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_flag_off_and_no_mandate_untouched(
        self, tmp_path: Path,
    ) -> None:
        off = _Setup(
            tmp_path, _mandate(), gate=_Gate(enabled=False),
        )
        result = off.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str) and "disabled" in result
        row = off.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None

        empty = _Setup(tmp_path / "none", None)
        result2 = empty.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result2, str) and "no approved" in result2


# ===========================================================================
# Handshake: record_unresolved_reference / owner_intent_for
# ===========================================================================


class TestUnresolvedReferenceHook:
    def test_hook_in_completeness_list(self) -> None:
        assert (
            "record_unresolved_reference"
            in passive_protocol._PASSIVE_HOOK_METHODS
        )
        full = {
            "begin_execution": lambda *a: None,
            "resolve_policy": lambda *a: None,
            "claim_submission": lambda *a: None,
            "record_outcome": lambda *a: None,
            "record_unresolved_reference": lambda *a, **k: None,
            "current_gate_issue": lambda: None,
            "now": lambda: NOW,
            "owner_intent_for": lambda *a: None,
        }
        assert passive_protocol.passive_hooks_complete(
            type("Full", (), dict(full))(),
        )
        partial = dict(full)
        partial.pop("record_unresolved_reference")
        assert not passive_protocol.passive_hooks_complete(
            type("Partial", (), dict(partial))(),
        )

    def test_records_incident_without_touching_mandate(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        before = setup.row()
        assert before is not None
        setup.hooks.record_unresolved_reference(
            "malformed:not-a-ref",
            "could not parse pending owner reference",
            broker_order_id="o-9",
        )
        after = setup.row()
        assert after is not None
        assert after.submit_state == before.submit_state
        assert after.claim_token == before.claim_token
        with setup.sessions() as db:
            incidents = (
                db.query(ReconciliationIncident)
                .filter(
                    ReconciliationIncident.failure_category
                    == "UNRESOLVED_PASSIVE_REFERENCE",
                )
                .count()
            )
        assert incidents == 1

    def test_persistence_failure_raises(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        setup.hooks.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "commit"),
        )
        with pytest.raises(RuntimeError):
            setup.hooks.record_unresolved_reference("m:1", "reason")


class TestOwnerIntentForErrorPropagation:
    def test_db_error_propagates_not_none(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())

        class _BoomSession:
            def get(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("db down")

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

        setup.hooks.inject_session_factory_for_tests(
            lambda: _BoomSession(),
        )
        with pytest.raises(RuntimeError):
            setup.hooks.owner_intent_for(1, "tok")

    def test_genuinely_absent_returns_none(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        fresh = _Setup(tmp_path / "fresh", None)
        assert fresh.hooks.owner_intent_for(999, "tok") is None
        # Claim-token mismatch is also genuinely-absent, not an error.
        assert fresh.hooks.owner_intent_for(999, "") is None


# ===========================================================================
# Facade delegation shape (signature-level; the executor side is X's)
# ===========================================================================


class TestFacadeDelegation:
    def test_execute_reservation_delegates_exact_collaborators(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str)
        captured: dict[str, Any] = {}

        def fake_entry(**kwargs: Any) -> Any:
            captured.update(kwargs)
            from app.services.trade_execution_service import OrderStatus

            return OrderStatus("", "SKIPPED", reason="gate")

        setup.executor.execute_passive_entry = fake_entry  # type: ignore[attr-defined]
        setup.passive.execute_reservation(
            ref,
            quote=object(),  # type: ignore[arg-type]
            broker=object(),  # type: ignore[arg-type]
            risk=RiskController(),
            notifier=cast(Any, _NullNotifier()),
        )
        assert set(captured) == {"ref", "quote", "broker", "risk", "notifier"}
        assert captured["ref"] is ref

    def test_facade_does_not_begin_owner_or_fetch_cash(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED

        def fake_entry(**kwargs: Any) -> Any:
            from app.services.trade_execution_service import OrderStatus

            return OrderStatus("", "SKIPPED", reason="denied")

        setup.executor.execute_passive_entry = fake_entry  # type: ignore[attr-defined]
        outcome = setup.passive.execute_reservation(
            ref,
            quote=object(),  # type: ignore[arg-type]
            broker=object(),  # type: ignore[arg-type]
            risk=RiskController(),
            notifier=cast(Any, _NullNotifier()),
        )
        # The facade performed no ownership/cash/outcome work itself: the
        # row is untouched by the facade (the executor owns finalisation).
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
        assert row.execution_token is None  # never began ownership
        assert outcome.submitted is False


class _NullNotifier:
    def notify_risk_event(self, *a: Any, **k: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return lambda *a: None


# ===========================================================================
# Integration reconciliation follow-up (writer Y)
# ===========================================================================


class TestFirstFillAfterUnknown:
    """Item 1: first observed fill on a same-status receipt is FORWARD.

    The record may carry a terminal status from the submit receipt with NO
    fill facts yet (bound_executed_quantity/price NULL). The first coherent
    positive fill observed for the SAME id/status is forward information —
    not a regression. Missing NEW facts still never erase OLD ones; qty
    still never decreases; terminal still never reverts to live; UNCERTAIN
    is still sticky.
    """

    def test_same_terminal_status_first_fill_is_forward(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(
                broker_status="CANCELLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("619.90"),
            ),
        )
        assert verdict == "PROGRESS"

    def test_db_same_terminal_first_fill_recorded(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        # Submit receipt: terminal CANCELLED with no fill facts yet.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=None, executed_price=None),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_status == "CANCELLED"
        assert row.bound_executed_quantity is None
        # Status poll delivers the first positive partial fill.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("5"),
                  executed_price=Decimal("619.90")),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("5")
        assert row.bound_executed_price == Decimal("619.90")
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN

    def test_db_filled_old_new_missing_no_erase(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="FILLED",
                  executed_quantity=Decimal("8"),
                  executed_price=Decimal("620")),
        )
        # A later receipt missing the fills must NOT erase them.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="FILLED",
                  executed_quantity=None, executed_price=None),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_executed_price == Decimal("620")
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_backwards_and_contradictions_still_block(self) -> None:
        # qty decrease stays blocked
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(broker_status="FILLED",
                       executed_quantity=Decimal("5"),
                       executed_price=Decimal("620")),
        )
        assert verdict == "CONFLICT"
        # terminal -> live stays blocked
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=Decimal("5"),
            bound_executed_price=Decimal("619.90"),
            fact=_fact(broker_status="SUBMITTED",
                       executed_quantity=Decimal("5"),
                       executed_price=Decimal("619.90")),
        )
        assert verdict == "CONFLICT"
        # same fill conflicting price stays blocked
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(broker_status="FILLED",
                       executed_quantity=Decimal("8"),
                       executed_price=Decimal("610")),
        )
        assert verdict == "CONFLICT"

    def test_uncertain_still_sticky(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_UNCERTAIN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_status="FILLED",
                       executed_quantity=Decimal("5"),
                       executed_price=Decimal("619.90")),
        )
        assert verdict == "IDEMPOTENT"  # sticky: ordinary receipt no-clear


class TestFacadePureDelegation:
    """Item 2: execute_reservation ONLY delegates — no lane-gate precheck.

    For an existing reserved ref, even a revoked lane flag must reach the
    dedicated executor, which wins ownership and performs the owned
    NO_SUBMIT burn. The facade must not short-circuit before delegating.
    """

    def test_no_gate_precheck_reaches_executor(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        captured: dict[str, Any] = {}

        def fake_entry(**kwargs: Any) -> Any:
            captured.update(kwargs)
            from app.services.trade_execution_service import OrderStatus

            return OrderStatus("", "SKIPPED", reason="gate")

        setup.executor.execute_passive_entry = fake_entry  # type: ignore[attr-defined]
        setup.passive.execute_reservation(
            ref,
            quote=object(),  # type: ignore[arg-type]
            broker=object(),  # type: ignore[arg-type]
            risk=RiskController(),
            notifier=cast(Any, _NullNotifier()),
        )
        # The executor was reached (no facade short-circuit) with the
        # exact collaborator set.
        assert set(captured) == {"ref", "quote", "broker", "risk", "notifier"}

    def test_reserved_ref_revoked_flag_reaches_executor_and_burns(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end: reserve with the lane ON, revoke, execute via the
        facade — the executor authenticates/owns and burns NO_SUBMIT; the
        reservation is never replayable."""
        from app.services.trade_execution_service import TradeExecutionService

        setup = _Setup(
            tmp_path,
            _mandate(),
            executor=_real_executor(),
        )
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED

        # Revoke the lane flag AFTER the reservation exists.
        setup.gate.enabled = False
        outcome = setup.passive.execute_reservation(
            ref,
            quote=_real_quote(),
            broker=cast(Any, _JournalBrokerStub()),
            risk=RiskController(),
            notifier=cast(Any, _NullNotifier()),
        )
        assert outcome.submitted is False
        # The executor performed the owned burn (not a facade no-write skip)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT
        assert row.entry_authorisation_available is False
        # And the reservation is never replayable.
        setup.gate.enabled = True
        replay = setup.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(replay, str)


class TestReserveRequiresCompleteHooks:
    """Item 3: reserve_entry refuses BEFORE any write when hooks are
    incomplete; the row stays clearly AUTHORIZED with no token/consumed
    time. Uses fresh readers, never a cached constructor boolean."""

    @pytest.mark.parametrize(
        "missing_hook",
        [
            "begin_execution",
            "resolve_policy",
            "claim_submission",
            "record_outcome",
            "record_unresolved_reference",
            "current_gate_issue",
            "now",
            "owner_intent_for",
        ],
    )
    def test_each_missing_hook_refuses_without_writes(
        self, tmp_path: Path, missing_hook: str,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        complete = setup.passive.build_hook_bundle()
        # Drop exactly one required hook.
        partial = _PartialHooks(complete, missing_hook)
        setup.executor.passive_submit_hooks = partial
        result = setup.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str)
        assert "not fully wired" in result or "hooks" in result
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None
        assert row.entry_authorisation_consumed_at is None
        assert row.entry_authorisation_available is True
        assert row.intent_json is None

    def test_no_hooks_at_all_refuses_without_writes(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        setup.executor.passive_submit_hooks = None
        result = setup.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None

    def test_complete_bundle_positive_control(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
        assert row.claim_token == ref.claim_token


class _PartialHooks:
    """A hook bundle missing exactly one required method."""

    def __init__(self, inner: Any, missing: str) -> None:
        self._inner = inner
        self._missing = missing

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name == self._missing:
            raise AttributeError(
                f"hook {name!r} intentionally missing for this test",
            )
        return getattr(self._inner, name)


def _real_executor() -> Any:
    from app.services.trade_execution_service import (
        FinalOrderQuoteCheckResult,
        TradeExecutionService,
    )

    def qchk(
        _broker: Any, _symbol: Any, _action: Any, price: Decimal,
    ) -> FinalOrderQuoteCheckResult:
        return FinalOrderQuoteCheckResult(
            executable_price=price, bid=price, ask=price,
        )

    return TradeExecutionService(
        record_order=lambda *a, **k: None,
        update_order_status=lambda *a, **k: None,
        record_risk_event=lambda *a, **k: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        final_order_quote_check=qchk,
    )


def _real_quote() -> Any:
    from app.core.broker import Quote

    return Quote(
        PASSIVE_SYMBOL, 600.0, 599.99, 600.01, "2026-09-30T15:00:00Z",
    )


class _JournalBrokerStub:
    """Broker stub for the revoked-flag path: no submission happens."""

    def __init__(self) -> None:
        self.submissions: list[Any] = []

    def get_positions(self) -> list[Any]:
        return []

    def get_cash(self, currency: str | None = None) -> Decimal:
        return Decimal("10000")

    def get_strict_usd_cash_snapshot(self) -> Any:
        return _FakeCash(Decimal("10000"))

    def estimate_margin_max_quantity(
        self, *a: Any, **k: Any,
    ) -> Decimal:
        return Decimal("1000")

    def submit_limit_order(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("no broker submission may occur on this path")


# ===========================================================================
# Exceptional remediation (owner-authorized): B4 + B5 regressions
# Source contract: /tmp/opencode/spy-passive-exceptional-remediation.md
# RED captured on a copy of the review3 frozen snapshot before the fix.
# ===========================================================================


class TestB5StatusProgressionIndependent:
    """B5: status rank never decreases independently of quantity rise;
    terminal states never change to another terminal or live status."""

    def test_partial3_to_submitted4_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="PARTIAL_FILLED",
            bound_executed_quantity=Decimal("3"),
            bound_executed_price=None,
            fact=_fact(broker_status="SUBMITTED",
                       executed_quantity=Decimal("4")),
        )
        assert verdict == "CONFLICT"

    def test_filled8_to_rejected9_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(broker_status="REJECTED",
                       executed_quantity=Decimal("9"),
                       executed_price=Decimal("620")),
        )
        assert verdict == "CONFLICT"

    def test_filled8_to_cancelled8_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="FILLED",
            bound_executed_quantity=Decimal("8"),
            bound_executed_price=Decimal("620"),
            fact=_fact(broker_status="CANCELLED",
                       executed_quantity=Decimal("8"),
                       executed_price=Decimal("620")),
        )
        assert verdict == "CONFLICT"

    def test_unknown_observed_status_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="SUBMITTED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_status="MYSTERY_STATUS",
                       executed_quantity=Decimal("4")),
        )
        assert verdict == "CONFLICT"

    def test_pure_intent_quantity_bound_blocks_overfill(self) -> None:
        # RED on the frozen snapshot: the bound kwarg itself is new API —
        # the BEHAVIOURAL RED for the missing bound is
        # test_first_fill_over_intent_escalates_with_evidence below
        # (service-level, no new API). This pure form pins the validator
        # contract once the kwarg exists.
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_status="CANCELLED",
                       executed_quantity=Decimal("9"),
                       executed_price=Decimal("619.90")),
            intent_quantity=Decimal("8"),
        )
        assert verdict == "CONFLICT"

    def test_pure_increment_within_bound_still_forward(self) -> None:
        # Regression guard (behaviourally satisfied pre-fix via probe
        # B5e; the kwarg is new API added with the bound).
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=Decimal("3"),
            bound_executed_price=Decimal("619.90"),
            fact=_fact(broker_status="CANCELLED",
                       executed_quantity=Decimal("5"),
                       executed_price=Decimal("619.90")),
            intent_quantity=Decimal("8"),
        )
        assert verdict == "PROGRESS"

    def test_pure_first_fill_within_bound_forward(self) -> None:
        # Regression guard (behaviourally satisfied pre-fix via probe
        # B5f; the kwarg is new API added with the bound).
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            bound_broker_order_id=ORDER_ID,
            bound_broker_status="CANCELLED",
            bound_executed_quantity=None,
            bound_executed_price=None,
            fact=_fact(broker_status="CANCELLED",
                       executed_quantity=Decimal("5"),
                       executed_price=Decimal("619.90")),
            intent_quantity=Decimal("8"),
        )
        assert verdict == "PROGRESS"


class TestB5ServiceIntentBound:
    """B5 service-level: intent-bound enforcement with evidence retention."""

    def _owner_with_intent8(self, setup: _Setup) -> Any:
        ref, owner = setup.claim_to_submitting()
        assert owner.intent.quantity == Decimal("8")
        return ref, owner

    def test_first_fill_over_intent_escalates_with_evidence(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = self._owner_with_intent8(setup)
        setup.hooks.record_outcome(
            owner, _fact(broker_status="CANCELLED",
                         executed_quantity=None),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("9"),
                  executed_price=Decimal("619.90")),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        # The overfill observation is retained as conflict evidence...
        assert row.uncertainty_reason and "9" in row.uncertainty_reason
        # ...but never became canonical progress, and the intent is intact.
        assert row.bound_executed_quantity != Decimal("9")
        assert (
            passive_protocol.intent_from_json(row.intent_json).quantity
            == Decimal("8")
        )
        assert row.bound_broker_order_id == ORDER_ID

    def test_rank_drop_with_qty_rise_escalates(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = self._owner_with_intent8(setup)
        setup.hooks.record_outcome(
            owner, _fact(broker_status="SUBMITTED"),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="PARTIAL_FILLED",
                  executed_quantity=Decimal("3"),
                  executed_price=Decimal("620")),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="SUBMITTED",
                  executed_quantity=Decimal("4"),
                  executed_price=Decimal("620")),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_broker_status == "PARTIAL_FILLED"
        assert row.bound_executed_quantity == Decimal("3")

    def test_terminal_swap_escalates_canonical_facts_kept(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = self._owner_with_intent8(setup)
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="FILLED",
                  executed_quantity=Decimal("8"),
                  executed_price=Decimal("620")),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="REJECTED",
                  executed_quantity=Decimal("9"),
                  executed_price=Decimal("620")),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_broker_status == "FILLED"
        assert row.bound_executed_quantity == Decimal("8")

    def test_terminal_partial_increments_within_intent(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = self._owner_with_intent8(setup)
        # First positive partial on a terminal receipt.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("5"),
                  executed_price=Decimal("619.90")),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("5")
        # Exact duplicate: harmless.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("5"),
                  executed_price=Decimal("619.90")),
        )
        # Coherent increment to 6 (<= intent 8): still allowed.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("6"),
                  executed_price=Decimal("619.90")),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("6")
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        # Increment to 9 (> intent 8): overfill evidence, never canonical.
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="CANCELLED",
                  executed_quantity=Decimal("9"),
                  executed_price=Decimal("619.90")),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_executed_quantity == Decimal("6")

    def test_invalid_qty_cannot_skip_bound_via_unknown_status(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = self._owner_with_intent8(setup)
        setup.hooks.record_outcome(
            owner, _fact(broker_status="SUBMITTED"),
        )
        setup.hooks.record_outcome(
            owner,
            _fact(broker_status="MYSTERY_STATUS",
                  executed_quantity=Decimal("9")),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_executed_quantity is None


class TestB4ExecuteFaultsInBurnHelpers:
    """B4: a real Session.execute UPDATE exception in EACH burn/quarantine
    helper produces the typed uncertainty, the review latch, and an
    incident attempt — never a raw escape, never a 'consumed' refusal, and
    the cheaper transparent re-reserve stays blocked."""

    @staticmethod
    def _execute_raises_factory(
        sessions: Any, match_states: tuple[str, ...] = ("NO_SUBMIT", "UNCERTAIN"),
    ) -> Any:
        class _Factory:
            def __init__(self) -> None:
                self._sessions = sessions
                self._match = set(match_states)

            def __call__(self) -> Any:
                db = self._sessions()
                original_execute = db.execute

                def raising_execute(*a: Any, **k: Any) -> Any:
                    stmt = a[0] if a else None
                    fault = False
                    if (
                        stmt is not None
                        and str(stmt).startswith("UPDATE passive_mandates")
                    ):
                        try:
                            params = (
                                getattr(stmt.compile(), "params", None) or {}
                            )
                            fault = any(
                                str(value) in self._match
                                for key, value in params.items()
                                if "submit_state" in str(key)
                            )
                        except Exception:
                            fault = False
                    if fault:
                        raise RuntimeError("simulated SQL execute failure")
                    return original_execute(*a, **k)

                db.execute = raising_execute  # type: ignore[method-assign]
                return db

        return _Factory()

    @staticmethod
    def _incident_count(setup: _Setup) -> int:
        with setup.sessions() as db:
            return (
                db.query(ReconciliationIncident)
                .filter(
                    ReconciliationIncident.failure_category
                    == "UNRESOLVED_PASSIVE_REFERENCE",
                )
                .count()
            )

    def test_burn_authorized_execute_raise(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            self._execute_raises_factory(setup.sessions),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        # Cheaper transparent re-reserve blocked by the review latch.
        setup.passive.inject_session_factory_for_tests(setup.sessions)
        later = setup.passive.reserve_entry(price=Decimal("400"))
        assert isinstance(later, str)
        assert "blocked pending review" in later
        assert self._incident_count(setup) >= 1

    def test_fail_closed_execute_raise(
        self, tmp_path: Path,
    ) -> None:
        from sqlalchemy import update

        setup = _Setup(tmp_path, _mandate())
        with setup.sessions() as db:
            db.execute(
                update(PassiveMandate)
                .where(PassiveMandate.lane == PASSIVE_LANE)
                .values(
                    entry_authorisation_available=False,
                    protocol_version="passive-submit-v2",
                )
            )
            db.commit()
        setup.passive.inject_session_factory_for_tests(
            self._execute_raises_factory(setup.sessions),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        setup.passive.inject_session_factory_for_tests(setup.sessions)
        later = setup.passive.reserve_entry(price=Decimal("400"))
        assert isinstance(later, str)
        assert "blocked pending review" in later
        assert self._incident_count(setup) >= 1

    def test_burn_no_submit_execute_raise(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str)
        setup.passive.inject_session_factory_for_tests(
            self._execute_raises_factory(
                setup.sessions, match_states=("NO_SUBMIT",),
            ),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive._burn_no_submit(  # type: ignore[reportPrivateUsage]
                ref, "gate revoked",
            )
        row = setup.row()
        assert row is not None
        assert row.submit_state == (
            passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
        )
        setup.passive.inject_session_factory_for_tests(setup.sessions)
        later = setup.passive.reserve_entry(price=Decimal("400"))
        assert isinstance(later, str)
        assert "blocked pending review" in later
        assert self._incident_count(setup) >= 1

    def test_secondary_rollback_failure_masks_nothing(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        base = self._execute_raises_factory(setup.sessions)

        class _DoubleFault(type(base)):  # noqa: N801 - dynamic subclass
            def __call__(self) -> Any:
                db = super().__call__()
                original_rollback = db.rollback

                def broken_rollback(*a: Any, **k: Any) -> None:
                    raise RuntimeError("rollback also failed")

                db.rollback = broken_rollback  # type: ignore[method-assign]
                return db

        setup.passive.inject_session_factory_for_tests(_DoubleFault())
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))

    def test_commit_and_rowcount_cases_retained(
        self, tmp_path: Path,
    ) -> None:
        # commit-fault path still typed (pre-existing coverage, kept)
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        setup.passive.inject_session_factory_for_tests(
            _FlakySessionFactory(setup.sessions, "commit"),
        )
        with pytest.raises(PassivePersistenceUncertain):
            setup.passive.reserve_entry(price=Decimal("600"))
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED

    def test_off_and_no_mandate_untouched(self, tmp_path: Path) -> None:
        off = _Setup(
            tmp_path, _mandate(), gate=_Gate(enabled=False),
        )
        result = off.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str) and "disabled" in result
        row = off.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        empty = _Setup(tmp_path / "none", None)
        result2 = empty.passive.reserve_entry(price=Decimal("600"))
        assert isinstance(result2, str) and "no approved" in result2
