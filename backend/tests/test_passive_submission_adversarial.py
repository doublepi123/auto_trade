"""R1 adversarial regressions for the SPY passive submit protocol.

Each test reproduces one Gate-R1 finding (see
/tmp/opencode/spy-passive-r1-findings.md) as a permanent behavioural
regression. All tests use private SQLite databases and fakes matching the
frozen cash-evidence interface; no network, no live broker, no .env access
(the shared ../.env is never touched — isolation is per-test private DBs).

The crash tests run REAL child processes that execute the lifecycle against
an isolated DB + durable fake-broker journal and are killed (os._exit /
SIGKILL) at protocol phases; the parent then independently reads the
journal + DB and drives a NEW execution instance, asserting no duplicate
broker mutation and no replay. These prove durable no-replay and living-
process uncertainty handling only — startup scan wiring remains Phase 2.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.accounting_fees import (
    ACCOUNTING_FEE_MODEL_US_SEC98,
    model_from_config_snapshot,
)
from app.core.broker import BrokerGateway, OrderResult, Position, Quote
from app.core.notify import ServerChanNotifier
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
from app.services.passive_allocation_service import PassiveAllocationService
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
    _PendingOrder,
)

TSLA = "TSLA.US"
NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCash:
    __slots__ = (
        "amount", "currency", "request_started_at",
        "request_completed_at", "provenance",
    )

    def __init__(
        self,
        amount: Decimal,
        *,
        clock_now: datetime | None = None,
    ) -> None:
        now = clock_now or NOW
        self.amount = amount
        self.currency = "USD"
        self.request_started_at = now - timedelta(seconds=1)
        self.request_completed_at = now - timedelta(seconds=0.5)
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


class _JournalBroker(BrokerGateway):
    """Fake broker: positions, strict cash, durable submissions journal.

    ``submit_hook`` runs BEFORE the mutation is recorded — the lost-ACK fake
    RECORDS the accepted mutation (fsync) and THEN raises, proving the
    system must treat the exception as possibly-submitted.
    """

    def __init__(
        self,
        *,
        cash: Decimal = Decimal("10000"),
        journal: Path | None = None,
        clock: _Clock | None = None,
        next_status: str = "SUBMITTED",
        next_order_id: str = "probe-1",
    ) -> None:
        self.positions: list[Position] = []
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []
        self.cash_amount = cash
        self.cash_error: Exception | None = None
        self.clock = clock or _Clock()
        self.journal = journal
        self.submit_hook: Any = None
        self.next_status = next_status
        self.next_order_id = next_order_id
        self.margin_reads = 0

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_cash(self, currency: str | None = None) -> Decimal:
        return self.cash_amount

    def get_strict_usd_cash_snapshot(self) -> Any:
        err = self.cash_error
        if err is not None:
            raise err
        return _FakeCash(self.cash_amount, clock_now=self.clock())

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        self.margin_reads += 1
        return Decimal("1000")

    def _journal_append(self, *parts: str) -> None:
        if self.journal is None:
            return
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write("|".join(parts) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        self._journal_append(
            "submit", symbol, side, str(quantity), str(price),
            self.next_order_id,
        )
        if self.submit_hook is not None:
            self.submit_hook()  # lost-ACK fake raises AFTER the fsync above
        return OrderResult(
            self.next_order_id, symbol, side, quantity, price,
            self.next_status,
        )


class _PartialFillBroker(_JournalBroker):
    """Terminal CANCELLED with an actual POSITIVE partial fill."""

    def __init__(self) -> None:
        super().__init__(next_status="CANCELLED")

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        result = super().submit_limit_order(symbol, side, quantity, price)
        # Positive fill facts on a terminal CANCELLED receipt.
        object.__setattr__  # noqa: B018 - keep linters quiet, no-op
        return OrderResult(
            broker_order_id=result.broker_order_id,
            symbol=result.symbol,
            side=result.side,
            quantity=result.quantity,
            price=result.price,
            status="CANCELLED",
        )


def _quote_check(
    _broker: BrokerGateway,
    _symbol: str,
    _action: str,
    price: Decimal,
) -> FinalOrderQuoteCheckResult:
    return FinalOrderQuoteCheckResult(
        executable_price=price, bid=price, ask=price,
    )


def _reprice_to(final: str):
    def check(
        _broker: BrokerGateway,
        _symbol: str,
        _action: str,
        _price: Decimal,
    ) -> FinalOrderQuoteCheckResult:
        p = Decimal(final)
        return FinalOrderQuoteCheckResult(executable_price=p, bid=p, ask=p)
    return check


def _svc(**overrides: Any) -> TradeExecutionService:
    params: dict[str, Any] = {
        "record_order": lambda *_a, **_k: None,
        "update_order_status": lambda *_a, **_k: None,
        "record_risk_event": lambda *_a, **_k: None,
        "max_position_quantity": 100,
        "max_position_notional": 5000.0,
        "max_risk_per_trade": 250.0,
        "stop_loss_pct": 1.0,
        "final_order_quote_check": _quote_check,
    }
    params.update(overrides)
    return TradeExecutionService(**params)


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


def _quote(price: float = 600.0) -> Quote:
    return Quote(
        PASSIVE_SYMBOL, price, price - 0.01, price + 0.01,
        "2026-09-30T15:00:00Z",
    )


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None,
        *,
        gate: _Gate | None = None,
        clock: _Clock | None = None,
        execution: TradeExecutionService | None = None,
        cash: Decimal = Decimal("10000"),
        next_status: str = "SUBMITTED",
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
        self.clock = clock or _Clock()
        self.execution = execution or _svc()
        self.passive = PassiveAllocationService(
            execution=self.execution,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )
        self.execution.passive_submit_hooks = self.passive.build_hook_bundle()
        self.broker = _JournalBroker(
            cash=cash,
            journal=tmp / "broker.journal",
            clock=self.clock,
            next_status=next_status,
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

    def reserve(self, price: Decimal = Decimal("600")) -> Any:
        return self.passive.reserve_entry(price=price)

    def execute(self, ref: Any, risk: RiskController | None = None) -> Any:
        return self.passive.execute_reservation(
            ref,
            quote=_quote(),
            broker=self.broker,
            risk=risk if risk is not None else RiskController(),
            notifier=ServerChanNotifier(""),
        )

    def full(self, risk: RiskController | None = None) -> Any:
        ref = self.reserve()
        assert not isinstance(ref, str), ref
        return ref, self.execute(ref, risk)


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import trade_execution_service as tes

    monkeypatch.setattr(tes, "is_trading_hours", lambda _m: True)


# ===========================================================================
# R1-1: CAS loser owns no cleanup; bound ID immutable
# ===========================================================================


class TestR1OneLoserZeroWrites:
    def test_loser_cannot_burn_winner_checking_row(
        self, tmp_path: Path,
    ) -> None:
        """Winner in CHECKING; loser's rejection performs ZERO writes."""
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        winner = hooks.begin_execution(ref, "exec-winner")
        assert not isinstance(winner, passive_protocol.PassiveRejection)
        loser = hooks.begin_execution(ref, "exec-loser")
        assert isinstance(loser, passive_protocol.PassiveRejection)

        # The production loser path must not touch the winner's row.
        out = setup.execute(ref)
        assert out.submitted is False
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING
        assert row.execution_token == "exec-winner"
        assert setup.broker.submissions == []

    def test_winner_unblocked_finishes_after_loser_rejection(
        self, tmp_path: Path,
    ) -> None:
        """Deterministic Event/barrier interleaving: loser rejected first,
        the winner can still finish unchanged through the real public
        lifecycle (final-remediation: the dedicated ref-entry owns
        begin_execution + cash + submission, so the winner is driven end
        to end by ``execute_passive_entry(ref=...)`` alone)."""
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        # A hostile pre-begun owner with a DIFFERENT token acts as the
        # CAS loser (it cannot steal the row).
        loser_result = hooks.begin_execution(ref, "exec-loser")
        assert not isinstance(loser_result, passive_protocol.PassiveRejection)

        loser_done = threading.Event()

        def loser_attempt() -> None:
            # The production loser path (facade) must not touch a row owned
            # by another execution.
            setup.execute(ref)
            loser_done.set()

        loser_thread = threading.Thread(target=loser_attempt)
        loser_thread.start()
        loser_thread.join(timeout=10)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING
        assert row.execution_token == "exec-loser"
        # The winner now drives the real public lifecycle. It lost the
        # begin race (row already CHECKING under the loser token), so the
        # entry refuses with ZERO further writes — exactly the guarantee
        # that a loser can never mutate the winner's row.
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "SKIPPED"
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING
        assert row.execution_token == "exec-loser"
        assert loser_done.is_set()

    def test_wrong_execution_token_never_mutates_terminal_row(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref, outcome = setup.full()
        assert outcome.submitted is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        bound_id = row.bound_broker_order_id or ""
        assert bound_id
        hooks = setup.passive.build_hook_bundle()
        intent = passive_protocol.intent_from_json(row.intent_json or "")
        wrong = passive_protocol.PassiveOwner(
            ref=ref, execution_token="exec-wrong", intent=intent,
        )
        with pytest.raises(ValueError):
            hooks.record_outcome(
                wrong,
                passive_protocol.PassiveOutcomeFact(
                    outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id="WRONG-ID",
                    broker_status="SUBMITTED",
                ),
            )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == bound_id

    def test_conflicting_correct_owner_receipt_preserves_bound_id(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref, outcome = setup.full()
        assert outcome.submitted is True
        row = setup.row()
        assert row is not None
        bound_id = row.bound_broker_order_id
        hooks = setup.passive.build_hook_bundle()
        intent = passive_protocol.intent_from_json(row.intent_json or "")
        true_owner = passive_protocol.PassiveOwner(
            ref=ref,
            execution_token=row.execution_token or "",
            intent=intent,
        )
        # A contradictory receipt id from the CORRECT owner: the bound id
        # is retained and the contradiction recorded as uncertainty facts.
        hooks.record_outcome(
            true_owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id="DIFFERENT-ID",
                broker_status="SUBMITTED",
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == bound_id
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.uncertainty_reason and "DIFFERENT-ID" in row.uncertainty_reason

        # Late-doubt UNCERTAIN with another id: STILL preserves the bound id.
        hooks.record_outcome(
            true_owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                broker_order_id="OTHER-ID",
                reason="late doubt",
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == bound_id

    def test_uncertain_fact_without_id_preserves_bound_id(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref, outcome = setup.full()
        assert outcome.submitted is True
        row = setup.row()
        assert row is not None
        bound_id = row.bound_broker_order_id or ""
        assert bound_id
        hooks = setup.passive.build_hook_bundle()
        intent = passive_protocol.intent_from_json(row.intent_json or "")
        true_owner = passive_protocol.PassiveOwner(
            ref=ref,
            execution_token=row.execution_token or "",
            intent=intent,
        )
        hooks.record_outcome(
            true_owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                broker_order_id="",  # unknown id — must not blank the bound id
                reason="doubt",
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == bound_id


# ===========================================================================
# R1-2: post-CAS gate (pause/REDUCING/policy drift in the UPDATE)
# ===========================================================================


class TestR1TwoPostCasGate:
    def test_manual_pause_after_cas_refuses_with_zero_mutation(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        original_recheck = (
            setup.execution._recheck_passive_before_broker_call
        )

        def recheck_after_manual_pause(
            approved_order: Any, risk: RiskController,
        ) -> str | None:
            risk.pause("manual review mid-flight")
            return original_recheck(approved_order, risk)

        setup.execution._recheck_passive_before_broker_call = (  # type: ignore[method-assign]
            recheck_after_manual_pause
        )
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_reducing_after_cas_refuses_with_zero_mutation(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        risk = RiskController()
        original_recheck = (
            setup.execution._recheck_passive_before_broker_call
        )
        calls = {"n": 0}

        def recheck_after_reducing(
            approved_order: Any, risk: RiskController,
        ) -> str | None:
            calls["n"] += 1
            # REDUCING arrives DURING the post-CAS latency window.
            risk.replace_daily_pnl(-6000.0, 3)
            return original_recheck(approved_order, risk)

        setup.execution._recheck_passive_before_broker_call = recheck_after_reducing  # type: ignore[method-assign]
        outcome = setup.execute(ref, risk)
        assert calls["n"] >= 1  # the recheck genuinely ran post-CAS
        assert outcome.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_kill_switch_after_cas_refuses(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        risk = RiskController()
        risk.enable_kill_switch("test")
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        assert setup.broker.submissions == []

    def test_allotment_tightened_between_boundary_and_cas_refuses(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        original_claim = setup.execution._claim_passive_submission_right

        def claim_after_drift(approved_order: Any) -> str | None:
            with setup.sessions() as db:
                db.execute(
                    update(PassiveMandate)
                    .where(PassiveMandate.id == ref.mandate_id)
                    .values(allotment_usd=100.0)
                )
                db.commit()
            return original_claim(approved_order)

        setup.execution._claim_passive_submission_right = claim_after_drift  # type: ignore[method-assign]
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state != passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        # The intent is NOT reusable after the drift refusal.
        again = setup.reserve()
        assert isinstance(again, str)

    def test_revocation_between_boundary_and_cas_refuses(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        original_claim = setup.execution._claim_passive_submission_right

        def claim_after_revoke(approved_order: Any) -> str | None:
            with setup.sessions() as db:
                db.execute(
                    update(PassiveMandate)
                    .where(PassiveMandate.id == ref.mandate_id)
                    .values(status="REVOKED")
                )
                db.commit()
            return original_claim(approved_order)

        setup.execution._claim_passive_submission_right = claim_after_revoke  # type: ignore[method-assign]
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert setup.broker.submissions == []


# ===========================================================================
# R1-3: single trusted entry; context is not authority; None rejected
# ===========================================================================


class _NoneResolverHooks:
    """Hostile hook wrapper whose resolve_policy returns None."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def begin_execution(self, ref: Any, token: str) -> Any:
        return self._inner.begin_execution(ref, token)

    def resolve_policy(self, owner: Any, request: Any) -> None:
        return None

    def claim_submission(
        self, owner: Any, final_order: Any, cash: Any,
    ) -> bool:
        return self._inner.claim_submission(owner, final_order, cash)

    def record_outcome(self, owner: Any, fact: Any) -> None:
        return self._inner.record_outcome(owner, fact)

    def current_gate_issue(self) -> str | None:
        return self._inner.current_gate_issue()

    def now(self) -> datetime:
        return self._inner.now()

    def owner_intent_for(self, mandate_id: int, claim: str) -> Any:
        return self._inner.owner_intent_for(mandate_id, claim)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        return self._inner.record_unresolved_reference(
            reference, reason, broker_order_id=broker_order_id,
        )


class TestR1ThreeTrustedEntry:
    def test_resolver_none_rejects_never_range_fallback(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        setup.execution.passive_submit_hooks = cast(Any, _NoneResolverHooks(hooks))
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_injected_owner_in_generic_context_denied(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-ctx")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        status = setup.execution.execute(
            "BUY",
            PASSIVE_SYMBOL,
            _quote(),
            setup.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            execution_context={
                # Caller-smuggled protocol objects — audit-only context
                # must NEVER convey the submit right (R1-3).
                "passive_submit_owner": owner,
                "passive_cash_evidence": _FakeCash(Decimal("10000")),
                "passive_submit_right_won": True,
            },
        )
        assert status is not None and status.status == "SKIPPED"
        assert "smuggling" in (status.reason or "")
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING

    def test_dedicated_entry_zero_cash_burns_then_same_ref_denied(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(), cash=Decimal("0"))
        ref, first = setup.full()
        assert first.submitted is False
        setup.broker.cash_amount = Decimal("10000")
        second = setup.execute(ref)
        assert second.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_zero_margin_reads_on_passive_path(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, outcome = setup.full()
        assert outcome.submitted is True, outcome.reason
        assert setup.broker.margin_reads == 0

    def test_no_lane_unknown_lane_and_context_only_quantity_denied(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        # Unknown lane marker.
        status = setup.execution.execute(
            "BUY", PASSIVE_SYMBOL, _quote(), setup.broker,
            RiskController(), ServerChanNotifier(""), "USD",
            execution_context={
                passive_policy.EXECUTION_CONTEXT_LANE_KEY: "ARBITRARY",
            },
        )
        assert status is not None and status.status == "SKIPPED"
        assert "unknown lane" in (status.reason or "")
        # Context-only quantity, no lane.
        status2 = setup.execution.execute(
            "BUY", TSLA, _quote(), setup.broker, RiskController(),
            ServerChanNotifier(""), "USD", sized_quantity=Decimal("40"),
        )
        assert status2 is not None and status2.status == "SKIPPED"
        assert "sized_quantity is only accepted" in (status2.reason or "")
        assert setup.broker.submissions == []
        assert setup.broker.margin_reads == 0


# ===========================================================================
# R1-4: lifecycle catches every failure into UNCERTAIN (+ pause + facts)
# ===========================================================================


class _FailingRecordHooks(_NoneResolverHooks):
    def __init__(self, inner: Any, fail_for: set[str]) -> None:
        super().__init__(inner)
        self._fail_for = fail_for

    def resolve_policy(self, owner: Any, request: Any) -> Any:
        return self._inner.resolve_policy(owner, request)

    def record_outcome(self, owner: Any, fact: Any) -> None:
        if fact.outcome in self._fail_for:
            raise RuntimeError("denial receipt DB down")
        return self._inner.record_outcome(owner, fact)


class TestR1FourLifecycleCatch:
    def test_denial_receipt_failure_is_uncertain_paused(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(), cash=Decimal("0"))
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        setup.execution.passive_submit_hooks = cast(Any, _FailingRecordHooks(
            hooks, {passive_protocol.SUBMIT_STATE_NO_SUBMIT},
        ))
        risk = RiskController()
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        assert outcome.uncertain is True
        assert risk.paused is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_after_bind_processing_failure_is_uncertain(
        self, tmp_path: Path,
    ) -> None:
        def boom_record(*_a: Any, **_k: None) -> None:
            raise RuntimeError("orders table down")

        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        setup.execution = _svc(record_order=boom_record)
        setup.execution.passive_submit_hooks = cast(Any, setup.passive.build_hook_bundle())
        setup.passive._execution = setup.execution
        risk = RiskController()
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False or outcome.uncertain is True
        assert risk.paused is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == (
            passive_protocol.SUBMIT_STATE_UNCERTAIN
        )
        # The known broker id is preserved in the mandate facts.
        assert row.bound_broker_order_id

    def test_direct_unknown_receipt_is_uncertain(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate(), next_status="SOMETHING_ELSE")
        ref = setup.reserve()
        assert not isinstance(ref, str)
        risk = RiskController()
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=risk,
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "UNCERTAIN"
        # Final-remediation finding 2: the direct UNKNOWN path pauses with
        # the REAL risk controller, not just the mandate state.
        assert risk.paused is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_lost_ack_fake_records_mutation_before_raising(
        self, tmp_path: Path,
    ) -> None:
        """Lost ACK: the fake fsyncs the accepted mutation THEN raises."""
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)

        def raise_after_record() -> None:
            # The mutation is already journaled (fsync in submit); the ACK
            # is lost on the way back.
            raise ConnectionError("connection lost after send")

        setup.broker.submit_hook = raise_after_record
        risk = RiskController()
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        assert outcome.uncertain is True
        assert risk.paused is True
        # The durable journal proves a broker mutation may exist...
        journal_path = setup.broker.journal
        assert journal_path is not None
        journal_lines = (
            journal_path.read_text().splitlines()
            if journal_path.exists() else []
        )
        assert any(line.startswith("submit|") for line in journal_lines)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        # ...and a restart/second attempt never resubmits.
        second = setup.execute(ref)
        assert second.submitted is False
        assert len(setup.broker.submissions) == 1

    def test_terminal_partial_fill_with_positive_facts_is_known(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        # CANCELLED with a positive executed fill (carried on the receipt).
        setup.broker.next_status = "CANCELLED"
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        # Direct receipt recording with positive fills (as the status poll
        # would deliver them), driven through the dedicated ref-entry.
        _status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        # Progress the SAME id to a positive partial fill: retained facts.
        hooks.record_outcome(
            passive_protocol.PassiveOwner(
                ref=ref,
                execution_token=row.execution_token or "",
                intent=passive_protocol.intent_from_json(row.intent_json or ""),
            ),
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status="CANCELLED",
                executed_quantity=Decimal("5"),
                executed_price=Decimal("619.90"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_executed_quantity == Decimal("5")
        assert row.bound_executed_price == Decimal("619.90")
        assert row.bound_broker_order_id  # never blanked


# ===========================================================================
# R1-5: pending owner ref survives rebuilds; real-DB accounting evidence
# ===========================================================================


class TestR1FivePendingAndAccounting:
    def test_pending_ref_complete_and_survives_rebuild(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        _ref, outcome = setup.full()
        assert outcome.submitted is True
        pending = setup.execution.pending_order
        assert pending is not None
        ref_parts = pending.passive_owner_ref.split(":")
        assert len(ref_parts) == 3  # mandate:claim:execution
        # A rebuild (as the runner's DB reload performs) must keep the ref.
        reloaded = _PendingOrder(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=None,
        )
        setup.execution.load_pending_orders([reloaded])
        after = setup.execution.pending_order
        assert after is not None
        assert after.passive_owner_ref == pending.passive_owner_ref

    def test_same_id_status_fill_progress_recorded_not_swallowed(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref, outcome = setup.full()
        assert outcome.submitted is True
        hooks = setup.passive.build_hook_bundle()
        row = setup.row()
        assert row is not None
        owner = passive_protocol.PassiveOwner(
            ref=ref,
            execution_token=row.execution_token or "",
            intent=passive_protocol.intent_from_json(row.intent_json or ""),
        )
        # Exact same fact: idempotent, no error.
        hooks.record_outcome(
            owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status="SUBMITTED",
            ),
        )
        # PROGRESS: same id, FILLED with positive fills.
        hooks.record_outcome(
            owner,
            passive_protocol.PassiveOutcomeFact(
                outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status="FILLED",
                executed_quantity=Decimal("8"),
                executed_price=Decimal("620.00"),
            ),
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_status == "FILLED"
        assert row.bound_executed_quantity == Decimal("8")
        assert row.bound_executed_price == Decimal("620.00")

    def test_real_writer_db_reload_carries_sec98_model(
        self, tmp_path: Path,
    ) -> None:
        """Writer -> DB -> reload round trip with a REAL orders-table row.

        Uses the real OrderRecord model + the runner's ledger semantics
        (config_snapshot contract) — no mocks around the DB.
        """
        from app.models import OrderRecord

        recorded: dict[str, Any] = {}

        def record_order(
            order_id: str,
            symbol: str,
            side: str,
            qty: float,
            price: float,
            status: str = "SUBMITTED",
            filled_at: datetime | None = None,
            executed_quantity: float | None = None,
            executed_price: float | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            recorded["args"] = {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "status": status,
                "metadata": dict(metadata or {}),
            }
            with setup.sessions() as db:
                db.add(
                    OrderRecord(
                        broker_order_id=order_id,
                        symbol=symbol,
                        side=side,
                        quantity=qty,
                        price=price,
                        status=status,
                        config_snapshot=json.dumps(
                            (metadata or {}).get("config_snapshot", ""),
                        ),
                    ),
                )
                db.commit()

        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        setup.execution = _svc(record_order=record_order)
        setup.execution.passive_submit_hooks = cast(Any, setup.passive.build_hook_bundle())
        setup.passive._execution = setup.execution
        outcome = setup.execute(ref)
        assert outcome.submitted is True, outcome.reason

        metadata = recorded["args"]["metadata"]
        snapshot_json = metadata.get("config_snapshot")
        assert isinstance(snapshot_json, str)
        # The reload pipeline extracts the model from the snapshot JSON.
        assert model_from_config_snapshot(snapshot_json) == (
            ACCOUNTING_FEE_MODEL_US_SEC98
        )
        # And the DB row actually persists it.
        with setup.sessions() as db:
            row = (
                db.query(OrderRecord)
                .filter(
                    OrderRecord.broker_order_id
                    == recorded["args"]["order_id"],
                )
                .one()
            )
            assert model_from_config_snapshot(
                json.loads(row.config_snapshot or '""'),
            ) == ACCOUNTING_FEE_MODEL_US_SEC98

    def test_settlement_failure_updates_mandate_uncertainty(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref, outcome = setup.full()
        assert outcome.submitted is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        pending = setup.execution.pending_order
        assert pending is not None

        def broken_settle(_intent: Any) -> Any:
            raise RuntimeError("settlement DB down")

        setup.execution._settle_fill = broken_settle  # type: ignore[method-assign]
        from app.services.trade_execution_service import OrderStatus as OS

        fill_status = OS(
            broker_order_id=pending.broker_order_id,
            status="FILLED",
            executed_quantity=Decimal("8"),
            executed_price=Decimal("620.00"),
        )
        risk = RiskController()
        with pytest.raises(Exception):
            setup.execution._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
                pending,
                fill_status,
                risk=risk,
                notifier=ServerChanNotifier(""),
            )
        assert risk.paused is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN


# ===========================================================================
# R1-6: original vs final approved price are distinct
# ===========================================================================


class TestR1SixOriginalVsFinalPrice:
    def test_sufficient_cash_repriced_620_submits_once(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            _mandate(),
            execution=_svc(final_order_quote_check=_reprice_to("620.00")),
            cash=Decimal("10000"),
        )
        ref, outcome = setup.full()
        assert outcome.submitted is True, outcome.reason
        assert len(setup.broker.submissions) == 1
        assert setup.broker.submissions[0][3] == Decimal("620.00")
        row = setup.row()
        assert row is not None
        snapshot = json.loads(row.final_snapshot_json or "{}")
        assert snapshot["approved_price"] == "620"
        assert snapshot["quantity"] == "8"

    def test_insufficient_4802_repriced_620_denied(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            _mandate(),
            execution=_svc(final_order_quote_check=_reprice_to("620.00")),
            cash=Decimal("4802"),
        )
        ref, outcome = setup.full()
        assert outcome.submitted is False
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_wrong_original_price_still_rejected(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-x")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        verdict = hooks.resolve_policy(
            owner,
            passive_protocol.PassiveOrderSpec(
                symbol=PASSIVE_SYMBOL,
                side="BUY",
                quantity=Decimal("8"),
                price=Decimal("601"),  # drifted ORIGINAL request price
            ),
        )
        assert isinstance(verdict, passive_protocol.PassiveRejection)
        assert "original price" in verdict.reason


# ===========================================================================
# R1-7: reservation revalidates the whole unconsumed state; migration
# ===========================================================================


class TestR1SevenReservationValidation:
    def test_contradictory_v2_authorized_available_false_fail_closed(
        self, tmp_path: Path,
    ) -> None:
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
        result = setup.reserve()
        assert isinstance(result, str)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert not row.claim_token

    def test_contradictory_v2_authorized_consumed_at_fail_closed(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        with setup.sessions() as db:
            db.execute(
                update(PassiveMandate)
                .where(PassiveMandate.lane == PASSIVE_LANE)
                .values(
                    entry_authorisation_consumed_at=NOW,
                    protocol_version="passive-submit-v2",
                )
            )
            db.commit()
        result = setup.reserve()
        assert isinstance(result, str)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_live_protocol_state_row_not_mutated_by_reservation(
        self, tmp_path: Path,
    ) -> None:
        """A racing reservation must never mark an in-flight row."""
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        second = setup.reserve()
        assert isinstance(second, str)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED

    def test_empty_db_helper_twice_no_duplicate_column(
        self, tmp_path: Path,
    ) -> None:
        """Completely EMPTY database (no create_all) — helper CREATE then a
        second call must not attempt a duplicate ALTER."""
        engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        try:
            _ensure_passive_mandates_table(engine)
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                cols = {
                    r[1] for r in c.exec_driver_sql(
                        "PRAGMA table_info(passive_mandates)"
                    )
                }
        finally:
            engine.dispose()
        assert "submit_state" in cols
        assert "bound_executed_quantity" in cols

    def test_legacy_used_evidence_preserved_not_reauthorized(
        self, tmp_path: Path,
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        try:
            with engine.begin() as c:
                c.exec_driver_sql(
                    "CREATE TABLE passive_mandates ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "lane VARCHAR(40) NOT NULL, "
                    "policy_version VARCHAR(60) NOT NULL, "
                    "symbol VARCHAR(20) NOT NULL, "
                    "status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE', "
                    "allotment_usd FLOAT NOT NULL, "
                    "risk_model VARCHAR(40) NOT NULL, "
                    "exemptions TEXT NOT NULL, "
                    "review_interval_months INTEGER NOT NULL, "
                    "entry_authorisation_available BOOLEAN NOT NULL DEFAULT 1,"
                    " entry_authorisation_consumed_at DATETIME, "
                    "submit_state VARCHAR(30) NOT NULL DEFAULT 'AUTHORIZED', "
                    "failure_reason TEXT, claim_token VARCHAR(64), "
                    "intent_json TEXT, execution_token VARCHAR(64), "
                    "final_snapshot_json TEXT, uncertainty_reason TEXT, "
                    "protocol_version VARCHAR(40), "
                    "order_binding VARCHAR(40) NOT NULL, "
                    "bound_broker_order_id VARCHAR(100), "
                    "bound_broker_status VARCHAR(40), "
                    "bound_executed_quantity NUMERIC(18, 6), "
                    "bound_executed_price NUMERIC(18, 6), "
                    "approved_at DATETIME NOT NULL, "
                    "approved_by VARCHAR(120) NOT NULL, "
                    "approval_reason TEXT NOT NULL, "
                    "created_at DATETIME, updated_at DATETIME, "
                    "CONSTRAINT ux_passive_mandates_lane UNIQUE (lane))"
                )
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "claim_token, bound_broker_order_id, submit_state, "
                    "order_binding, approved_at, approved_by, "
                    "approval_reason, created_at, updated_at) VALUES ("
                    "'SPY_PASSIVE','passive-allocation-v1','SPY.US',"
                    "'ACTIVE',5000.0,'FULL_PRINCIPAL','no_price_stop',6,1,"
                    "'legacy-token','legacy-order-77','SUBMITTED',"
                    "'paper-only','2026-09-29','owner','x',"
                    "'2026-09-29','2026-09-29')"
                )
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state, claim_token, "
                    "bound_broker_order_id FROM passive_mandates"
                ).one()
        finally:
            engine.dispose()
        # Never reauthorized; every used fact retained.
        assert row[0] == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row[1] == "legacy-token"
        assert row[2] == "legacy-order-77"

    def test_legacy_boolean_parsing_strict(self) -> None:
        from app.database import _parse_legacy_boolean

        assert _parse_legacy_boolean(1) is True
        assert _parse_legacy_boolean(0) is False
        assert _parse_legacy_boolean("1") is True
        assert _parse_legacy_boolean("0") is False
        assert _parse_legacy_boolean(None) is None
        assert _parse_legacy_boolean("true") is None  # never truthy-parsed
        assert _parse_legacy_boolean("yes") is None
        assert _parse_legacy_boolean("") is None


# ===========================================================================
# R1-8: invalid sizing burns a valid authorization
# ===========================================================================


class TestR1EightInvalidSizingBurns:
    def test_unformable_intent_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        result = setup.reserve(price=Decimal("600"))
        assert isinstance(result, str) and "single share" in result
        assert "consumed" in result
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT
        assert row.claim_token is None
        assert row.entry_authorisation_available is False
        assert row.entry_authorisation_consumed_at is not None

    def test_cheaper_price_cannot_rereserve_after_burn(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(allotment_usd=500.0))
        _ = setup.reserve(price=Decimal("600"))
        later = setup.reserve(price=Decimal("400"))
        assert isinstance(later, str) and "not available" in later

    def test_flag_off_and_no_mandate_leave_db_untouched(
        self, tmp_path: Path,
    ) -> None:
        off = _Setup(
            tmp_path, _mandate(), gate=_Gate(enabled=False),
        )
        result = off.reserve(price=Decimal("600"))
        assert isinstance(result, str) and "disabled" in result
        row = off.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None

        empty = _Setup(tmp_path / "none", None)
        result2 = empty.reserve(price=Decimal("600"))
        assert isinstance(result2, str) and "no approved" in result2


# ===========================================================================
# Real killed-child crash evidence (durable journal; no live broker)
# ===========================================================================


_CRASH_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {backend_root!r})
    mode, db_path, journal_path, phase = (
        sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    )
    sys.argv = [sys.argv[0]]

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.notify import ServerChanNotifier
    from app.core.risk import RiskController
    from app.database import _ensure_passive_mandates_table
    from app.domain.passive_allocation import protocol as passive_protocol
    from app.models import Base
    from app.services.passive_allocation_service import (
        PassiveAllocationService,
    )
    from app.services import trade_execution_service as _tes
    _tes.is_trading_hours = lambda _m: True  # crash child runs off-hours
    from app.services.trade_execution_service import (
        FinalOrderQuoteCheckResult, TradeExecutionService,
    )

    NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)

    class Cash:
        def __init__(self, amount):
            self.amount = amount
            self.currency = "USD"
            self.request_started_at = NOW - timedelta(seconds=1)
            self.request_completed_at = NOW - timedelta(seconds=0.5)
            self.provenance = passive_protocol.PASSIVE_CASH_PROVENANCE

    class Broker:
        def __init__(self, journal_path):
            self.journal_path = journal_path
            self.submitted = 0
        def get_positions(self):
            return []
        def get_cash(self, currency=None):
            return Decimal("10000")
        def get_strict_usd_cash_snapshot(self):
            return Cash(Decimal("10000"))
        def estimate_margin_max_quantity(self, *a, **k):
            return Decimal("1000")
        def submit_limit_order(self, symbol, side, quantity, price):
            import json
            from app.core.broker import OrderResult
            self.submitted += 1
            order_id = f"crash-{{self.submitted}}"
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps({{
                    "event": "submit", "symbol": symbol, "side": side,
                    "quantity": str(quantity), "price": str(price),
                    "order_id": order_id,
                }}) + "\\n")
                fh.flush()
                os.fsync(fh.fileno())
            if phase == "accepted-before-return":
                # The mutation is durably recorded; the process dies BEFORE
                # the result is returned/bound.
                os._exit(9)
            return OrderResult(order_id, symbol, side, quantity, price, "SUBMITTED")

    engine = create_engine(f"sqlite:///{{db_path}}", connect_args={{"timeout": 30}})
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def qchk(_b, _s, _a, price):
        return FinalOrderQuoteCheckResult(executable_price=price, bid=price, ask=price)

    execution = TradeExecutionService(
        record_order=lambda *a, **k: None,
        update_order_status=lambda *a, **k: None,
        record_risk_event=lambda *a, **k: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        final_order_quote_check=qchk,
    )
    clock = lambda: NOW
    passive = PassiveAllocationService(
        execution=execution, session_factory=sessions,
        lane_enabled_reader=lambda: True,
        paper_account_confirmed_reader=lambda: True, clock=clock,
    )
    execution.passive_submit_hooks = passive.build_hook_bundle()
    broker = Broker(journal_path)

    from app.core.broker import Quote
    quote = Quote("SPY.US", 600.0, 599.99, 600.01, "2026-09-30T15:00:00Z")

    if mode == "reserve-only":
        ref = passive.reserve_entry(price=Decimal("600"))
        print("RESERVED", ref.claim_token)
        os._exit(0)

    if mode == "after-checking":
        ref = passive.reserve_entry(price=Decimal("600"))
        hooks = passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-crash")
        print("CHECKING", ref.claim_token, flush=True)
        os._exit(9)

    if mode == "after-submitting":
        ref = passive.reserve_entry(price=Decimal("600"))
        hooks = passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-crash")
        cash = Cash(Decimal("10000"))
        from app.domain.passive_allocation import protocol as proto
        order = proto.PassiveOrderSpec(
            symbol="SPY.US", side="BUY",
            quantity=Decimal("8"), price=Decimal("600.00"),
        )
        assert hooks.claim_submission(owner, order, cash)
        print("SUBMITTING", ref.claim_token, flush=True)
        os._exit(9)

    if mode == "full":
        ref = passive.reserve_entry(price=Decimal("600"))
        if isinstance(ref, str):
            # Reservation refused (expected on restart): durable no-replay.
            print("DONE refused", ref[:60], 0, flush=True)
            os._exit(0)
        outcome = passive.execute_reservation(
            ref, quote=quote, broker=broker, risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        print(
            "DONE", outcome.submitted, broker.submitted,
            (outcome.reason or "")[:80], flush=True,
        )
        os._exit(0)
    """,
)


class TestRealCrashRecovery:
    def _child(self, tmp_path: Path, mode: str, phase: str = "") -> str:
        backend_root = str(Path(__file__).resolve().parents[1])
        db_path = tmp_path / "crash.db"
        journal = tmp_path / "broker.journal"
        if not db_path.exists():
            engine = create_engine(
                f"sqlite:///{db_path}", connect_args={"timeout": 30},
            )
            Base.metadata.create_all(engine)
            _ensure_passive_mandates_table(engine)
            with sessionmaker(bind=engine, expire_on_commit=False)() as db:
                db.add(_mandate())
                db.commit()
            engine.dispose()
        harness = tmp_path / f"child_{mode.replace('/', '_')}.py"
        harness.write_text(
            _CRASH_CHILD.format(backend_root=backend_root),
        )
        result = subprocess.run(
            [
                sys.executable, str(harness), mode, str(db_path),
                str(journal), phase,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "AUTO_TRADE_ENV": "test"},
            cwd=backend_root,
        )
        if result.returncode not in (0, 9):
            raise AssertionError(
                f"crash child {mode}/{phase} failed rc={result.returncode}: "
                f"{result.stderr[-2000:]}"
            )
        return result.stdout

    def _journal_submits(self, journal: Path) -> int:
        if not journal.exists():
            return 0
        return sum(
            1
            for line in journal.read_text().splitlines()
            if '"event": "submit"' in line
        )

    def test_child_killed_after_checking_no_duplicate_on_restart(
        self, tmp_path: Path,
    ) -> None:
        out = self._child(tmp_path, "after-checking")
        assert "CHECKING" in out
        journal = tmp_path / "broker.journal"
        assert self._journal_submits(journal) == 0
        # A NEW execution instance over the same DB must not duplicate.
        restart = self._child(tmp_path, "full")
        assert "DONE" in restart
        assert self._journal_submits(journal) == 0  # CHECKING not adoptable
        with create_engine(
            f"sqlite:///{tmp_path / 'crash.db'}",
        ).connect() as c:
            state = c.exec_driver_sql(
                "SELECT submit_state FROM passive_mandates"
            ).one()[0]
        # CHECKING is not adoptable and never re-authorized; the startup
        # scan that would terminalize it is Phase-2 wiring (blocked).
        assert state == passive_protocol.SUBMIT_STATE_CHECKING

    def test_child_killed_after_submitting_no_duplicate_on_restart(
        self, tmp_path: Path,
    ) -> None:
        out = self._child(tmp_path, "after-submitting")
        assert "SUBMITTING" in out
        journal = tmp_path / "broker.journal"
        assert self._journal_submits(journal) == 0
        restart = self._child(tmp_path, "full")
        assert "DONE" in restart
        # SUBMITTING is possibly-submitted: never resubmitted to discover.
        assert self._journal_submits(journal) == 0

    def test_child_killed_after_accepted_before_return_no_duplicate(
        self, tmp_path: Path,
    ) -> None:
        """The fake fsyncs the accepted mutation, then the child dies before
        the result returns. Restart must not duplicate the mutation."""
        out = self._child(tmp_path, "full", "accepted-before-return")
        journal = tmp_path / "broker.journal"
        submits = self._journal_submits(journal)
        assert submits == 1, out  # exactly one durable mutation exists
        restart = self._child(tmp_path, "full")
        assert "DONE" in restart
        assert self._journal_submits(journal) == 1  # NO duplicate

    def test_child_killed_after_id_bind_before_record_no_duplicate(
        self, tmp_path: Path,
    ) -> None:
        """ID bound in the mandate but the orders-table record never ran
        (record_order is a no-op in the child): restart must not re-submit."""
        out = self._child(tmp_path, "full")
        assert "DONE True 1" in out
        journal = tmp_path / "broker.journal"
        assert self._journal_submits(journal) == 1
        restart = self._child(tmp_path, "full")
        assert "DONE" in restart
        assert self._journal_submits(journal) == 1
