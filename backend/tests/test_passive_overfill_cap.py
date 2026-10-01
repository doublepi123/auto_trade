"""Phase2a P3: overfill cap + typed OutcomeWriteResult (writer W1).

Covers the frozen interface change: ``validate_outcome_write`` takes a
REQUIRED ``intent_quantity`` validated on ALL branches (including the
first SUBMITTING bind and UNCERTAIN/unknown observations);
``PassiveSubmitHooks.record_outcome`` returns ``OutcomeWriteResult``
(APPLIED / IDEMPOTENT / ESCALATED_UNCERTAIN), IO/CAS failures still
raise. First-bind overfill binds the known broker id/status, leaves the
canonical bound qty empty, retains the observed excess in the reason, and
never overwrites an existing id or the intent.

RED evidence: on the pristine bc9ef3c5 snapshot the first-bind overfill
became canonical ORDER_KNOWN with bound qty = the excess (verified
behaviourally before any fix — see /tmp/opencode/p2a/RED logs).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
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
from app.models import Base, PassiveMandate
from app.services.passive_allocation_service import (
    PassiveAllocationService,
    PassiveSubmitHookBundle,
)

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
ORDER_ID = "order-1"
INTENT_QTY = Decimal("8")


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
    max_position_quantity = 100
    max_position_notional = 5000.0

    def __init__(self) -> None:
        self.passive_submit_hooks: Any = None


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None = None,
        *,
        gate: _Gate | None = None,
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
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
        self.executor = _StubExecutor()
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
        ref = self.passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        owner = self.hooks.begin_execution(
            ref, f"exec-{uuid4().hex[:8]}",
        )
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        cash = _FakeCash(Decimal("10000"))
        order = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL,
            side="BUY",
            quantity=INTENT_QTY,
            price=Decimal("600.00"),
        )
        assert self.hooks.claim_submission(owner, order, cash)
        return ref, owner


def _fact(
    *,
    outcome: str = passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
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
# Pure validator: required bound on ALL branches
# ===========================================================================


class TestIntentQuantityRequired:
    def test_missing_bound_is_refused(self) -> None:
        # intent_quantity is REQUIRED: omitting it must not be accepted.
        kwargs: dict[str, Any] = {
            "current_state": passive_protocol.SUBMIT_STATE_SUBMITTING,
            "bound_broker_order_id": None,
            "fact": _fact(executed_quantity=Decimal("8")),
            # deliberately no intent_quantity
        }
        with pytest.raises(TypeError):
            passive_protocol.validate_outcome_write(**kwargs)

    def test_first_bind_overfill_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
            bound_broker_order_id=None,
            fact=_fact(
                broker_status="FILLED",
                executed_quantity=Decimal("9"),
                executed_price=Decimal("620"),
            ),
            intent_quantity=INTENT_QTY,
        )
        assert verdict == "CONFLICT"

    def test_first_bind_within_bound_is_allowed(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
            bound_broker_order_id=None,
            fact=_fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
            intent_quantity=INTENT_QTY,
        )
        assert verdict is None

    def test_uncertain_fact_overfill_is_conflict(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
            bound_broker_order_id=None,
            fact=_fact(
                outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                broker_order_id=ORDER_ID,
                executed_quantity=Decimal("9"),
            ),
            intent_quantity=INTENT_QTY,
        )
        assert verdict == "CONFLICT"

    def test_invalid_bound_is_refused(self) -> None:
        for bad in (Decimal("0"), Decimal("-1")):
            verdict = passive_protocol.validate_outcome_write(
                current_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
                bound_broker_order_id=None,
                fact=_fact(executed_quantity=Decimal("8")),
                intent_quantity=bad,
            )
            assert isinstance(verdict, str) and "intent_quantity" in verdict

    def test_invalid_observed_qty_is_refused(self) -> None:
        verdict = passive_protocol.validate_outcome_write(
            current_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
            bound_broker_order_id=None,
            fact=_fact(executed_quantity=Decimal("-3")),
            intent_quantity=INTENT_QTY,
        )
        assert isinstance(verdict, str) and "non-negative" in verdict


# ===========================================================================
# Typed result + first-bind overfill DB semantics
# ===========================================================================


class TestTypedResultAndFirstBindOverfill:
    def test_first_bind_overfill_returns_escalated_and_binds_known_id(
        self, tmp_path: Path,
    ) -> None:
        """THE core Phase2a P3 behaviour (RED on bc9: canonical qty 9)."""
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        result = setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("9"),
                executed_price=Decimal("620"),
            ),
        )
        assert result == (
            passive_protocol.OutcomeWriteResult.ESCALATED_UNCERTAIN
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        # Known broker id retained (never dropped), canonical qty EMPTY.
        assert row.bound_broker_order_id == ORDER_ID
        assert row.bound_broker_status == "FILLED"
        assert row.bound_executed_quantity is None
        # Observed excess preserved in the reason.
        assert row.uncertainty_reason and "9" in row.uncertainty_reason
        # Intent never rewritten.
        assert (
            passive_protocol.intent_from_json(row.intent_json).quantity
            == INTENT_QTY
        )

    def test_first_bind_within_bound_applies(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        result = setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="SUBMITTED",
                executed_quantity=None,
                executed_price=None,
            ),
        )
        assert result == passive_protocol.OutcomeWriteResult.APPLIED
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        assert row.bound_broker_order_id == ORDER_ID

    def test_exact_duplicate_returns_idempotent(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        first = setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        second = setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        assert first == passive_protocol.OutcomeWriteResult.APPLIED
        assert second == passive_protocol.OutcomeWriteResult.IDEMPOTENT

    def test_conflict_returns_escalated(self, tmp_path: Path) -> None:
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
        result = setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("620"),
            ),
        )
        assert result == (
            passive_protocol.OutcomeWriteResult.ESCALATED_UNCERTAIN
        )

    def test_forward_progress_returns_applied(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        result = setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620"),
            ),
        )
        assert result == passive_protocol.OutcomeWriteResult.APPLIED
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("8")

    def test_existing_id_and_intent_never_overwritten_on_overfill(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(
            owner,
            _fact(
                broker_status="SUBMITTED",
                executed_quantity=None,
            ),
        )
        # Later overfill with a DIFFERENT id must not replace the bound id.
        result = setup.hooks.record_outcome(
            owner,
            _fact(
                broker_order_id="other-id",
                broker_status="FILLED",
                executed_quantity=Decimal("9"),
                executed_price=Decimal("620"),
            ),
        )
        assert result == (
            passive_protocol.OutcomeWriteResult.ESCALATED_UNCERTAIN
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == ORDER_ID
        assert (
            passive_protocol.intent_from_json(row.intent_json).quantity
            == INTENT_QTY
        )

    def test_io_failure_still_raises(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()

        class _Boom:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

            def get(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("db down")

        setup.hooks.inject_session_factory_for_tests(  # type: ignore[reportPrivateUsage]
            lambda: _Boom(),
        )
        with pytest.raises(RuntimeError):
            setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))

    def test_uncertain_sticky_returns_escalated_or_idempotent(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, owner = setup.claim_to_submitting()
        setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        escalated = setup.hooks.record_outcome(
            owner,
            _fact(
                outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                reason="doubt",
            ),
        )
        assert escalated == (
            passive_protocol.OutcomeWriteResult.ESCALATED_UNCERTAIN
        )
        again = setup.hooks.record_outcome(owner, _fact(broker_status="SUBMITTED"))
        assert again in (
            passive_protocol.OutcomeWriteResult.IDEMPOTENT,
            passive_protocol.OutcomeWriteResult.ESCALATED_UNCERTAIN,
        )
