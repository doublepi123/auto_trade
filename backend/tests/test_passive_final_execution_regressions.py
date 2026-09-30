# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Writer X exceptional-remediation regressions: review3 findings B1/B3(+P2).

Every test drives the REAL public surfaces with deterministic interleavings
(Event/barrier — no sleeps):

* B1 (P0): the private passive call state must be lock-scoped AND
  thread-owned. A generic ``execute()`` running while a dedicated passive
  entry is blocked inside its own submission must NEVER borrow the
  owner/cash state — it cannot obtain a passive submission right, cannot
  submit SPY 8 @ approved 625 (cost 5001.89 > the 5000 allotment), and
  cannot inherit passive metadata. A valid dedicated entry still crosses
  the boundary exactly once and mutates the broker exactly once.
* B2 (P0): a begin-CAS that COMMITS and then throws must not fabricate a
  zero-quantity owner (the fabricated constructor raises ValueError
  BEFORE any pause/incident on the frozen source). The real behaviour
  must be: ownerless pause + record_unresolved_reference + UNCERTAIN,
  durable CHECKING (no replay), no broker mutation.
* B2-P2: a direct UNKNOWN receipt must produce an incident (was 0).
* B3 (P1): an EMPTY pending owner ref on an ordinary RANGE settlement
  failure must leave the range failure behaviour untouched — original
  pause reason preserved, zero passive callbacks/incidents. A marked
  (nonempty ref) passive equivalent still pauses + records the incident
  with factual fills retained.

RED: these run against a hash-verified copy of the frozen review3
snapshot; the assertions fail on the ACTUAL unsafe outcomes (a submitted
generic order, an escaped ValueError, an overwritten range pause reason),
never on imports or fixture errors.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
from app.services import trade_execution_service as tes
from app.services.passive_allocation_service import PassiveAllocationService
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
SEC98_FIXED = Decimal("1.568")
SEC98_RATE = Decimal("0.0000641")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Cash:
    __slots__ = (
        "amount", "currency", "request_started_at",
        "request_completed_at", "provenance",
    )

    def __init__(self, amount: Decimal, *, now: datetime | None = None) -> None:
        at = now or NOW
        self.amount = amount
        self.currency = "USD"
        self.request_started_at = at - timedelta(seconds=2)
        self.request_completed_at = at - timedelta(seconds=1)
        self.provenance = passive_protocol.PASSIVE_CASH_PROVENANCE


class _Broker(BrokerGateway):
    """Fake broker: journal, margin-read counter, status control."""

    def __init__(
        self,
        *,
        cash: Decimal = Decimal("10000"),
        clock: Any = None,
        journal: Path | None = None,
        next_status: str = "SUBMITTED",
    ) -> None:
        self.positions: list[Position] = []
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []
        self.cash_amount = cash
        self.clock = clock or (lambda: NOW)
        self.journal = journal
        self.margin_reads = 0
        self.next_status = next_status
        self.next_order_id = "probe-b1"
        self.boundary_reads = 0

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_cash(self, currency: str | None = None) -> Decimal:
        return self.cash_amount

    def get_strict_usd_cash_snapshot(self) -> Any:
        now = self.clock()
        return _Cash(self.cash_amount, now=now)

    def estimate_margin_max_quantity(
        self, symbol: str, side: str, price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        self.margin_reads += 1
        return Decimal("1000")

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        if self.journal is not None:
            import os as _os

            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"submit|{symbol}|{side}|{quantity}|{price}"
                    f"|{self.next_order_id}\n",
                )
                fh.flush()
                _os.fsync(fh.fileno())
        return OrderResult(
            self.next_order_id, symbol, side, quantity, price,
            self.next_status,
        )

    def get_order_status(self, order_id: str) -> Any:
        from app.core.broker import OrderStatusResult

        return OrderStatusResult(
            broker_order_id=order_id,
            status=self.next_status,
            executed_quantity=Decimal("8"),
            executed_price=Decimal("600.00"),
        )


def _quote(symbol: str = PASSIVE_SYMBOL, price: float = 600.0) -> Quote:
    return Quote(
        symbol, price, price - 0.01, price + 0.01,
        "2026-09-30T15:00:00Z",
    )


def _reprice_to(final: str):
    def check(
        _b: BrokerGateway, _s: str, _a: str, _p: Decimal,
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
        "final_order_quote_check": (
            lambda b, s, a, p: FinalOrderQuoteCheckResult(
                executable_price=p, bid=p, ask=p,
            )
        ),
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


class _Gate:
    def __init__(self) -> None:
        self.enabled = True
        self.paper = True

    def lane_on(self) -> bool:
        return self.enabled

    def paper_on(self) -> bool:
        return self.paper


class _Clock:
    def __init__(self) -> None:
        self.now_value = NOW

    def __call__(self) -> datetime:
        return self.now_value


@dataclass
class _IncidentRecord:
    reference: str
    reason: str
    broker_order_id: str | None


class _CountingHooks:
    """Wraps the real bundle; counts record_unresolved_reference calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.unresolved: list[_IncidentRecord] = []
        self.owner_writes: int = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        self.unresolved.append(
            _IncidentRecord(reference, reason, broker_order_id),
        )
        return self._inner.record_unresolved_reference(
            reference, reason, broker_order_id=broker_order_id,
        )

    def record_outcome(self, owner: Any, fact: Any) -> None:
        self.owner_writes += 1
        return self._inner.record_outcome(owner, fact)


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None,
        *,
        cash: Decimal = Decimal("10000"),
        next_status: str = "SUBMITTED",
        count_incidents: bool = True,
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.engine: Engine = create_engine(
            f"sqlite:///{tmp / f'b_{uuid4().hex}.db'}",
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
        self.gate = _Gate()
        self.clock = _Clock()
        self.execution = _svc()
        self.passive = PassiveAllocationService(
            execution=self.execution,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )
        bundle = self.passive.build_hook_bundle()
        self.hooks_counts = _CountingHooks(bundle) if count_incidents else None
        self.execution.passive_submit_hooks = (
            self.hooks_counts if self.hooks_counts is not None else bundle
        )
        self.broker = _Broker(
            cash=cash, clock=self.clock, journal=tmp / "broker.journal",
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


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tes, "is_trading_hours", lambda _m: True)


# ---------------------------------------------------------------------------
# B1 (P0): shared call state must be lock-scoped + thread-owned
# ---------------------------------------------------------------------------


class TestB1CallStateThreadOwnership:
    def test_generic_cannot_borrow_state_while_dedicated_blocked(
        self, tmp_path: Path,
    ) -> None:
        """Deterministic interleaving (B1): the dedicated passive entry
        installs its private call state and parks inside its own
        ``execute()`` call; a concurrent GENERIC BUY (no lane marker, no
        sized quantity, its own risk controller) runs to completion.

        The generic call must never borrow the passive owner/cash: its
        execution context carries NO passive lane markers, it obtains NO
        passive submission right (the mandate is not bound/mutated by
        it), and the only order it could submit is an ordinary
        margin-sized range order — the passive budget/submit-right
        channel stays with the dedicated entry."""
        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _reprice_to("625")
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref

        dedicated_installed = threading.Event()
        generic_entered = threading.Event()
        original_execute = setup.execution.execute
        observed_generic_context: dict[str, object] = {}
        main_thread_id = threading.get_ident()

        def dedicated() -> OrderStatus | None:
            def execute_probe(*args: Any, **kwargs: Any) -> Any:
                # Runs immediately after the dedicated entry installed its
                # call state. Park UNTIL the generic call has entered its
                # own execute() — the exact window in which an ambient,
                # non-thread-owned state would be borrowed.
                dedicated_installed.set()
                generic_entered.wait(timeout=10)
                return original_execute(*args, **kwargs)

            setup.execution.execute = execute_probe  # type: ignore[method-assign]
            try:
                return setup.execution.execute_passive_entry(
                    ref=ref,
                    quote=_quote(),
                    broker=setup.broker,
                    risk=RiskController(),
                    notifier=ServerChanNotifier(""),
                )
            finally:
                setup.execution.execute = original_execute  # type: ignore[method-assign]

        dedicated_thread = threading.Thread(target=dedicated)
        dedicated_thread.start()
        assert dedicated_installed.wait(timeout=10)

        # While the dedicated entry holds its installed call state, a
        # GENERIC range BUY runs on this thread to completion.
        generic_risk = RiskController()
        original_precheck = setup.execution.pre_submit_risk_check

        def execute_probe_generic(*args: Any, **kwargs: Any) -> Any:
            generic_entered.set()
            return original_execute(*args, **kwargs)

        def generic_precheck(request: Any, broker: Any) -> Any:
            if threading.get_ident() == main_thread_id:
                observed_generic_context.update(
                    dict(setup.execution._active_execution_context),
                )
            return original_precheck(request, broker)

        setup.execution.execute = execute_probe_generic  # type: ignore[method-assign]
        setup.execution.pre_submit_risk_check = generic_precheck  # type: ignore[method-assign]
        try:
            setup.execution.execute(
                "BUY",
                PASSIVE_SYMBOL,
                _quote(),
                setup.broker,
                generic_risk,
                ServerChanNotifier(""),
                "USD",
            )
        finally:
            setup.execution.execute = original_execute  # type: ignore[method-assign]
            setup.execution.pre_submit_risk_check = original_precheck  # type: ignore[method-assign]
            dedicated_thread.join(timeout=10)

        # B1 acceptance: the generic execution context must carry no
        # passive protocol markers — the order never silently became a
        # passive entry from ambient state.
        assert "passive_lane" not in observed_generic_context, (
            observed_generic_context,
        )
        assert "passive_entry_claim_token" not in observed_generic_context
        assert "passive_sized_quantity" not in observed_generic_context
        # And the generic order did not consume the passive submit right:
        # the mandate is NOT bound by the generic call — with the
        # dedicated parked before its broker call, no passive binding may
        # exist yet.
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id in (None, ""), (
            f"generic path bound the passive mandate: "
            f"{row.bound_broker_order_id}"
        )
        assert row.submit_state in {
            passive_protocol.SUBMIT_STATE_CHECKING,
            passive_protocol.SUBMIT_STATE_NO_SUBMIT,
            passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
        }

    def test_dedicated_entry_one_boundary_one_broker_mutation(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        boundary_calls: list[str] = []
        original = setup.execution.pre_submit_risk_check

        def counting_precheck(request: Any, broker: Any) -> Any:
            boundary_calls.append(request.action)
            return original(request, broker)

        setup.execution.pre_submit_risk_check = counting_precheck  # type: ignore[method-assign]
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "SUBMITTED", (
            getattr(status, "reason", None)
        )
        assert boundary_calls == ["BUY"]
        assert len(setup.broker.submissions) == 1
        assert setup.broker.margin_reads == 0

    def test_call_state_cleared_after_dedicated_entry(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert setup.execution._passive_call_state is None


# ---------------------------------------------------------------------------
# B2 (P0): ownership CAS persistence fault -> ownerless uncertainty
# ---------------------------------------------------------------------------


class _BeginCommitThenThrowHooks:
    """begin_execution commits the CAS, THEN raises (named DB fault)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.begun = 0
        self.unresolved: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def begin_execution(self, ref: Any, token: str) -> Any:
        result = self._inner.begin_execution(ref, token)
        if isinstance(result, passive_protocol.PassiveRejection):
            return result
        self.begun += 1
        # The DB write COMMITTED (result is an owner); the fault happens
        # after the commit — e.g. the session close/connection teardown.
        raise RuntimeError("named DB fault: commit-then-throw")

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        self.unresolved.append((reference, reason, broker_order_id))
        return self._inner.record_unresolved_reference(
            reference, reason, broker_order_id=broker_order_id,
        )


class TestB2OwnershipCasCommitThenThrow:
    def test_commit_then_throw_is_ownerless_uncertain_no_fabrication(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        inner = setup.passive.build_hook_bundle()
        faulty = _BeginCommitThenThrowHooks(inner)
        setup.execution.passive_submit_hooks = faulty  # type: ignore[assignment]
        risk = RiskController()
        raised: BaseException | None = None
        status: OrderStatus | None = None
        try:
            status = setup.execution.execute_passive_entry(
                ref=ref,
                quote=_quote(),
                broker=setup.broker,
                risk=risk,
                notifier=ServerChanNotifier(""),
            )
        except BaseException as exc:  # noqa: BLE001 - record what escapes
            raised = exc
        # The fabricated zero-quantity owner constructor must NOT raise a
        # ValueError past the caller (frozen behaviour: it did).
        assert not isinstance(raised, ValueError), (
            f"begin-CAS commit-then-throw escaped as ValueError: {raised!r}"
        )
        # Either an explicit UNCERTAIN status or a typed error mapping to
        # uncertainty is acceptable; a normal success/refusal is not.
        outcome = status if raised is None else None
        if outcome is not None:
            assert outcome.status == "UNCERTAIN", (
                f"expected UNCERTAIN, got {outcome.status!r}"
            )
        # Real non-auto pause with the ORDER_RECONCILIATION_UNCERTAIN
        # prefix happened regardless.
        assert risk.paused is True
        assert (risk.pause_reason or "").startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:",
        ), risk.pause_reason
        # An unresolved-reference incident was recorded with the REAL ref.
        assert faulty.unresolved, (
            "no record_unresolved_reference incident for the "
            "commit-then-throw ownership fault"
        )
        incident_ref = faulty.unresolved[0][0]
        assert ref.claim_token in incident_ref
        # Durable state: CHECKING with the row's own token — no replay,
        # no broker mutation.
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING
        assert setup.broker.submissions == []
        # And the SAME ref cannot replay into a second mutation.
        replay = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert replay is None or replay.status == "SKIPPED"
        assert setup.broker.submissions == []


# ---------------------------------------------------------------------------
# B2-P2: direct UNKNOWN must produce an incident
# ---------------------------------------------------------------------------


class TestP2DirectUnknownIncident:
    def test_direct_unknown_records_incident(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path, _mandate(), next_status="SOMETHING_ELSE",
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        risk = RiskController()
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=risk,
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "UNCERTAIN"
        assert risk.paused is True
        assert setup.hooks_counts is not None
        assert len(setup.hooks_counts.unresolved) >= 1, (
            "direct UNKNOWN produced 0 record_unresolved_reference "
            "incidents"
        )

    def test_receipt_binding_failure_records_incident(
        self, tmp_path: Path,
    ) -> None:
        class _FailBindHooks(_CountingHooks):
            def record_outcome(self, owner: Any, fact: Any) -> None:
                if fact.outcome == (
                    passive_protocol.SUBMIT_STATE_ORDER_KNOWN
                ):
                    raise RuntimeError("bind DB down")
                return super().record_outcome(owner, fact)

        setup = _Setup(tmp_path, _mandate())
        inner = setup.passive.build_hook_bundle()
        setup.execution.passive_submit_hooks = _FailBindHooks(inner)  # type: ignore[assignment]
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        risk = RiskController()
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=risk,
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "UNCERTAIN"
        assert risk.paused is True
        assert setup.broker.submissions, "the broker mutation happened"
        assert status.broker_order_id == "probe-b1"
        hooks_now = setup.execution.passive_submit_hooks
        assert isinstance(hooks_now, _FailBindHooks)
        assert hooks_now.unresolved, (
            "receipt binding failure produced no incident"
        )


# ---------------------------------------------------------------------------
# B3 (P1): empty pending ref preserves ordinary range failure behaviour
# ---------------------------------------------------------------------------


class TestB3EmptyRefRangePreservation:
    def _range_pending(self, setup: _Setup) -> Any:
        from app.services.trade_execution_service import _PendingOrder

        return _PendingOrder(
            broker=setup.broker,
            broker_order_id="range-1",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("10"),
            price=Decimal("100"),
            engine_snapshot=None,
            passive_owner_ref="",  # ORDINARY RANGE ORDER: no passive ref
        )

    def test_range_settlement_failure_keeps_original_reason(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())

        def broken_settle(_intent: Any) -> Any:
            raise RuntimeError("AAPL settlement DB down")

        setup.execution._settle_fill = broken_settle  # type: ignore[method-assign]
        pending = self._range_pending(setup)
        risk = RiskController()
        with pytest.raises(Exception, match="AAPL settlement DB down"):
            setup.execution._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
                pending,
                OrderStatus("range-1", "FILLED", Decimal("10"), Decimal("100")),
                risk=risk,
                notifier=ServerChanNotifier(""),
            )
        # The RANGE failure behaviour is preserved: paused with the
        # ORIGINAL range persistence reason (never overwritten by any
        # passive text), zero passive callbacks/incidents.
        assert risk.paused is True
        pause_reason = risk.pause_reason or ""
        assert pause_reason.startswith(
            "ORDER_STATUS_PERSISTENCE_UNCERTAIN:",
        ), pause_reason
        assert "range-1" in pause_reason
        assert "SPY_PASSIVE" not in pause_reason
        assert "no owner reference" not in pause_reason
        assert setup.hooks_counts is not None
        assert setup.hooks_counts.unresolved == [], (
            "ordinary range settlement failure emitted passive incidents: "
            f"{setup.hooks_counts.unresolved}"
        )
        assert setup.hooks_counts.owner_writes == 0

    def test_marked_passive_equivalent_still_escalates(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            lambda b, s, a, p: FinalOrderQuoteCheckResult(
                executable_price=p, bid=p, ask=p,
            )
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        status = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert status is not None and status.status == "SUBMITTED"
        pending = setup.execution.pending_order
        assert pending is not None
        assert pending.passive_owner_ref, "marked passive order carries ref"

        def broken_settle(_intent: Any) -> Any:
            raise RuntimeError("settlement DB down")

        setup.execution._settle_fill = broken_settle  # type: ignore[method-assign]
        risk = RiskController()
        with pytest.raises(Exception):
            setup.execution._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
                pending,
                OrderStatus(
                    pending.broker_order_id, "FILLED",
                    Decimal("8"), Decimal("600.00"),
                ),
                risk=risk,
                notifier=ServerChanNotifier(""),
            )
        assert risk.paused is True
        assert setup.hooks_counts is not None
        assert setup.hooks_counts.unresolved, (
            "marked passive settlement failure produced no incident"
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == "probe-b1"
