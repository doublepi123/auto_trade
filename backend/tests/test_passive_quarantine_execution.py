# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""W2 execution-layer guards + P3 quarantine/overfill integration (Phase2a).

Contract targets (Phase2a §W2 execution guards + §P3/W2 outcome
integration):

* ctor ``passive_reduction_quarantine: Callable[[str], str | None]``
  and ``passive_uncertainty_sink: Callable[[str, str | None], None]``,
  default None;
* the shared FINAL ``_final_submission_precheck`` blocks
  position-INCREASING actions when ``risk.external_block()`` exists —
  even for a direct generic caller with no runner entry-policy callback
  (parent correction #4); reductions whose quarantine callback reports
  the symbol are SKIPPED with a POSITION reason; a callback error is
  FAIL-CLOSED; empty callbacks + no block => exact legacy behaviour;
* the authoritative final reduction quarantine is ALWAYS consulted
  (an optional earlier check may exist, but it can never REPLACE the
  final one); exactly one pre-submit boundary and one broker submission
  are preserved; an unrelated HK proven reduction is NOT blocked when
  the quarantine reports nothing for it;
* ``_record_passive_fill_observation`` after ``_book_fill`` for
  immediate AND delayed fills, marked refs only, no double booking;
* on ESCALATED_UNCERTAIN / record_outcome exception: preserve an
  existing pause reason, otherwise a non-auto
  ORDER_RECONCILIATION_UNCERTAIN pause; unresolved-reference incident;
  ``passive_uncertainty_sink`` notified (even with the lane flag OFF —
  the sink is wired by the runner at startup, before any gating);
* an ACTUAL immediate overfill (9 filled on intent 8) MUST still book
  the actual 9 shares through the existing tracked/settlement path
  while the mandate goes uncertain + incident + guard — never an early
  UNCERTAIN return before accounting, never a trimmed 8-share booking.

RED: on pristine bc9ef3c5 these ctor callbacks / guard branches /
observation hooks do not exist and the overfill books without pause —
the failures below are behavioural.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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

from app.domain.passive_allocation.protocol import OutcomeWriteResult

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)


def _has_enum() -> bool:
    return OutcomeWriteResult is not None


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
    """Fake broker; submit carries configurable immediate fill facts."""

    def __init__(
        self,
        *,
        cash: Decimal = Decimal("10000"),
        clock: Any = None,
        submit_status: str = "SUBMITTED",
        submit_executed_quantity: Decimal | None = None,
        submit_executed_price: Decimal | None = None,
        poll_status: str | None = None,
        poll_executed_quantity: Decimal | None = None,
        poll_executed_price: Decimal | None = None,
    ) -> None:
        self.positions: list[Position] = []
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []
        self.cash_amount = cash
        self.clock = clock or (lambda: NOW)
        self.margin_reads = 0
        self.submit_status = submit_status
        self.submit_executed_quantity = submit_executed_quantity
        self.submit_executed_price = submit_executed_price
        # The FILLED-enrichment path re-queries the order immediately; a
        # poll defaulting to SUBMITTED would mask the immediate fill.
        self.poll_status = poll_status or submit_status
        self.poll_executed_quantity = (
            poll_executed_quantity
            if poll_executed_quantity is not None
            else submit_executed_quantity
        )
        self.poll_executed_price = (
            poll_executed_price
            if poll_executed_price is not None
            else submit_executed_price
        )
        self.next_order_id = "w2-1"

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
        result: Any = SimpleNamespace(
            broker_order_id=self.next_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status=self.submit_status,
        )
        if self.submit_executed_quantity is not None:
            result.executed_quantity = self.submit_executed_quantity
            result.executed_price = (
                self.submit_executed_price or price
            )
        return result

    def get_order_status(self, order_id: str) -> Any:
        from app.core.broker import OrderStatusResult

        return OrderStatusResult(
            broker_order_id=order_id,
            status=self.poll_status,
            executed_quantity=self.poll_executed_quantity,
            executed_price=self.poll_executed_price,
        )


def _quote(symbol: str = PASSIVE_SYMBOL, price: float = 600.0) -> Quote:
    return Quote(
        symbol, price, price - 0.01, price + 0.01,
        "2026-09-30T15:00:00Z",
    )


def _pass_through_check(
    _b: BrokerGateway, _s: str, _a: str, price: Decimal,
) -> FinalOrderQuoteCheckResult:
    return FinalOrderQuoteCheckResult(
        executable_price=price, bid=price, ask=price,
    )


def _svc(**overrides: Any) -> TradeExecutionService:
    params: dict[str, Any] = {
        "record_order": lambda *_a, **_k: None,
        "update_order_status": lambda *_a, **_k: None,
        "record_risk_event": lambda *_a, **_k: None,
        "max_position_quantity": 100,
        "max_position_notional": 5000.0,
        "max_risk_per_trade": 250.0,
        "stop_loss_pct": 1.0,
        "final_order_quote_check": _pass_through_check,
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
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
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
class _SinkRecord:
    reason: str
    broker_order_id: str | None


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[_SinkRecord] = []

    def __call__(self, reason: str, broker_order_id: str | None) -> None:
        self.records.append(_SinkRecord(reason, broker_order_id))


class _QuarantineCallback:
    def __init__(self, symbols: set[str], error: Exception | None = None):
        self.symbols = symbols
        self.error = error
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> str | None:
        self.calls.append(symbol)
        if self.error is not None:
            raise self.error
        return (
            f"quarantined: {symbol}" if symbol in self.symbols else None
        )


class _CountingHooks:
    """Live wrapper around the real bundle: forwards typed results,
    counts unresolved-reference incidents and outcome writes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.unresolved: list[Any] = []
        self.owner_writes: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

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

    def record_outcome(self, owner: Any, fact: Any) -> Any:
        self.owner_writes.append(fact)
        return self._inner.record_outcome(owner, fact)


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None,
        *,
        cash: Decimal = Decimal("10000"),
        lane_enabled: bool = True,
        broker_kwargs: dict[str, Any] | None = None,
        quarantine: _QuarantineCallback | None = None,
        sink: _RecordingSink | None = None,
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.engine: Engine = create_engine(
            f"sqlite:///{tmp / f'w2_{uuid4().hex}.db'}",
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
        self.gate = _Gate(lane_enabled)
        self.clock = _Clock()
        svc_kwargs: dict[str, Any] = {}
        if quarantine is not None:
            svc_kwargs["passive_reduction_quarantine"] = quarantine
        if sink is not None:
            svc_kwargs["passive_uncertainty_sink"] = sink
        self.execution = _svc(**svc_kwargs)
        self.passive = PassiveAllocationService(
            execution=self.execution,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )
        bundle = self.passive.build_hook_bundle()
        self.hooks_counts = _CountingHooks(bundle)
        self.execution.passive_submit_hooks = self.hooks_counts
        self.broker = _Broker(
            cash=cash, clock=self.clock, **(broker_kwargs or {}),
        )
        self.sink = sink
        self.quarantine = quarantine

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
# FINAL-precheck external-block gate (parent correction #4)
# ---------------------------------------------------------------------------


class TestFinalPrecheckExternalBlock:
    def test_direct_generic_entry_blocked_under_external_block(
        self, tmp_path: Path,
    ) -> None:
        """A DIRECT generic BUY (no runner entry-policy callback) must be
        refused at the FINAL precheck when risk.external_block() exists —
        TradingState REDUCING alone is not enough for callers that only
        check risk.check()."""
        setup = _Setup(tmp_path, None)
        risk = RiskController()
        risk.raise_external_block("passive_recovery", "hard uncertainty")
        # Direct generic entry: no lane, no policy callback, plain BUY.
        status = setup.execution.execute(
            "BUY",
            "AAPL.US",
            _quote("AAPL.US", 100.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
        )
        assert status is not None and status.status == "SKIPPED", (
            getattr(status, "reason", None)
        )
        assert setup.broker.submissions == []
        reason = str(status.reason or "")
        assert "external" in reason.lower() or "block" in reason.lower(), (
            reason
        )

    def test_reduction_still_allowed_under_external_block(
        self, tmp_path: Path,
    ) -> None:
        """The external block stops NEW exposure only: a proven reduction
        (SELL with a tracked LONG) is not blocked by the external block
        when the quarantine callback reports nothing for the symbol."""
        quarantine = _QuarantineCallback(set())
        setup = _Setup(tmp_path, None, quarantine=quarantine)
        from app.core.broker import Position as _Pos
        setup.broker.positions.append(
            _Pos("AAPL.US", "LONG", Decimal("10"), Decimal("100.0")),
        )
        risk = RiskController()
        risk.raise_external_block("passive_recovery", "hard")
        status = setup.execution.execute(
            "SELL",
            "AAPL.US",
            _quote("AAPL.US", 100.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            expected_exit_price=Decimal("120.0"),
            allow_loss_exit=True,
        )
        assert setup.broker.submissions, "reduction was blocked by the guard"

    def test_legacy_path_unchanged_without_block_or_callbacks(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, None)
        risk = RiskController()
        status = setup.execution.execute(
            "BUY",
            "AAPL.US",
            _quote("AAPL.US", 100.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
        )
        assert status is not None and status.status in {
            "SUBMITTED", "FILLED",
        }
        assert len(setup.broker.submissions) == 1


# ---------------------------------------------------------------------------
# Authoritative final reduction quarantine
# ---------------------------------------------------------------------------


class TestReductionQuarantine:
    def _tracked_spy(self, setup: _Setup) -> None:
        setup.execution.load_tracked_entries(
            {PASSIVE_SYMBOL: (Decimal("8"), Decimal("4800.0"))},
        )

    def test_spy_reduction_quarantined_at_final_precheck(
        self, tmp_path: Path,
    ) -> None:
        quarantine = _QuarantineCallback({PASSIVE_SYMBOL})
        setup = _Setup(tmp_path, None, quarantine=quarantine)
        self._tracked_spy(setup)
        from app.core.broker import Position as _Pos
        setup.broker.positions.append(
            _Pos(PASSIVE_SYMBOL, "LONG", Decimal("8"), Decimal("600.0")),
        )
        risk = RiskController()
        status = setup.execution.execute(
            "SELL",
            PASSIVE_SYMBOL,
            _quote(PASSIVE_SYMBOL, 620.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            expected_exit_price=Decimal("620.0"),
            allow_loss_exit=True,
        )
        assert status is not None and status.status == "SKIPPED"
        reason = str(status.reason or "")
        assert "quarantin" in reason.lower()
        assert setup.broker.submissions == []
        assert PASSIVE_SYMBOL in quarantine.calls

    def test_unrelated_hk_reduction_not_blocked(
        self, tmp_path: Path,
    ) -> None:
        quarantine = _QuarantineCallback({PASSIVE_SYMBOL})
        setup = _Setup(tmp_path, None, quarantine=quarantine)
        from app.core.broker import Position as _Pos
        setup.broker.positions.append(
            _Pos("0700.HK", "LONG", Decimal("100"), Decimal("200.0")),
        )
        risk = RiskController()
        status = setup.execution.execute(
            "SELL",
            "0700.HK",
            _quote("0700.HK", 200.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            expected_exit_price=Decimal("220.0"),
            allow_loss_exit=True,
        )
        assert setup.broker.submissions, (
            "unrelated HK proven reduction was blocked by the quarantine"
        )

    def test_quarantine_callback_error_fails_closed(
        self, tmp_path: Path,
    ) -> None:
        quarantine = _QuarantineCallback(
            set(), error=RuntimeError("quarantine lookup down"),
        )
        setup = _Setup(tmp_path, None, quarantine=quarantine)
        self._tracked_spy(setup)
        from app.core.broker import Position as _Pos
        setup.broker.positions.append(
            _Pos(PASSIVE_SYMBOL, "LONG", Decimal("8"), Decimal("600.0")),
        )
        status = setup.execution.execute(
            "SELL",
            PASSIVE_SYMBOL,
            _quote(PASSIVE_SYMBOL, 600.0),
            setup.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            expected_exit_price=Decimal("620.0"),
            allow_loss_exit=True,
        )
        assert status is not None and status.status == "SKIPPED"
        assert setup.broker.submissions == []

    def test_final_check_consulted_even_if_early_allowed(
        self, tmp_path: Path,
    ) -> None:
        """The authoritative FINAL quarantine runs even when an earlier
        optional check (or the absence of one) suggested the reduction
        was fine — verified by the callback being consulted on the FINAL
        precheck path for a direct caller with no runner decision side."""
        calls: list[str] = []
        quarantine = _QuarantineCallback({PASSIVE_SYMBOL})
        setup = _Setup(tmp_path, None, quarantine=quarantine)
        self._tracked_spy(setup)
        from app.core.broker import Position as _Pos
        setup.broker.positions.append(
            _Pos(PASSIVE_SYMBOL, "LONG", Decimal("8"), Decimal("600.0")),
        )
        risk = RiskController()
        status = setup.execution.execute(
            "SELL",
            PASSIVE_SYMBOL,
            _quote(PASSIVE_SYMBOL, 620.0),
            setup.broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            expected_exit_price=Decimal("620.0"),
            allow_loss_exit=True,
        )
        assert status is not None and status.status == "SKIPPED"
        # The callback ran for the target symbol (fail-closed verdict).
        assert calls == []
        assert PASSIVE_SYMBOL in quarantine.calls


# ---------------------------------------------------------------------------
# Overfill accounting + uncertainty (P3 integration)
# ---------------------------------------------------------------------------


class TestImmediateOverfill:
    def test_overfill_books_actual_nine_and_escalates(
        self, tmp_path: Path,
    ) -> None:
        """Intent 8; broker fills 9 IMMEDIATELY. The ACTUAL 9 shares must
        be booked through the existing tracked/settlement path while the
        mandate escalates to uncertain + incident + sink; never an early
        UNCERTAIN return before accounting, never a trimmed 8-share
        booking, never a second submission."""
        sink = _RecordingSink()
        setup = _Setup(
            tmp_path,
            _mandate(),
            sink=sink,
            broker_kwargs=dict(
                submit_status="FILLED",
                submit_executed_quantity=Decimal("9"),
                submit_executed_price=Decimal("600.00"),
            ),
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
        # Exactly one broker mutation happened.
        assert len(setup.broker.submissions) == 1
        # The actual fill of 9 WAS booked in tracked positions.
        tracked = setup.execution.tracked_position(PASSIVE_SYMBOL)
        assert tracked is not None, "overfill booked no tracked position"
        assert tracked.quantity == Decimal("9"), (
            f"actual overfill must book 9, got {tracked.quantity}"
        )
        # The mandate escalated to uncertain.
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_broker_order_id == "w2-1"
        # Risk paused non-auto with the reconciliation-uncertain prefix.
        assert risk.paused is True
        assert (risk.pause_reason or "").startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:",
        )
        # Unresolved-reference incident + uncertainty sink fired.
        assert setup.hooks_counts.unresolved, "no incident recorded"
        assert sink.records, "passive_uncertainty_sink not notified"

    def test_delayed_overfill_books_actual_and_escalates(
        self, tmp_path: Path,
    ) -> None:
        """Intent 8; submit SUBMITTED; a later status poll reports FILLED
        with qty 9 — the delayed fill observation escalates identically
        while booking the actual 9."""
        sink = _RecordingSink()
        setup = _Setup(tmp_path, _mandate(), sink=sink)
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
        assert status is not None and status.status == "SUBMITTED"
        setup.broker.poll_status = "FILLED"
        setup.broker.poll_executed_quantity = Decimal("9")
        setup.broker.poll_executed_price = Decimal("600.00")
        pending = setup.execution.pending_order
        assert pending is not None
        import time as _time

        fresh = pending.__class__(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=None,
            submitted_at=_time.monotonic(),
            passive_owner_ref=pending.passive_owner_ref,
        )
        setup.execution._pending_orders_by_id[pending.broker_order_id] = fresh
        setup.execution._reconcile_pending_order(
            fresh, risk=risk, notifier=ServerChanNotifier(""),
        )
        tracked = setup.execution.tracked_position(PASSIVE_SYMBOL)
        assert tracked is not None and tracked.quantity == Decimal("9"), (
            f"delayed overfill must book actual 9, got {tracked}"
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert sink.records, "delayed overfill sink not notified"

    def test_normal_fill_books_without_uncertainty(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
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
        assert status is not None and status.status == "SUBMITTED"
        row = setup.row()
        assert row is not None
        assert row.submit_state == (
            passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        )
        assert risk.paused is False


# ---------------------------------------------------------------------------
# Uncertainty sink + pause-reason preservation (even lane flag OFF)
# ---------------------------------------------------------------------------


class TestUncertaintySinkWiring:
    def test_sink_fired_even_when_lane_flag_off(
        self, tmp_path: Path,
    ) -> None:
        """The sink is wired at startup by the runner — BEFORE any lane
        gating — so a delayed UNCERTAIN observation arriving with the lane
        flag OFF still reaches the sink (the flag never gates observation
        escalation)."""
        sink = _RecordingSink()
        setup = _Setup(tmp_path, _mandate(), sink=sink)
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
        assert status is not None and status.status == "SUBMITTED"
        pending = setup.execution.pending_order
        assert pending is not None
        import time as _time

        fresh = pending.__class__(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=None,
            submitted_at=_time.monotonic(),
            passive_owner_ref=pending.passive_owner_ref,
        )
        setup.execution._pending_orders_by_id[pending.broker_order_id] = fresh
        # Flag OFF after the submit: observation escalation still fires.
        setup.gate.enabled = False
        setup.broker.poll_status = "SOMETHING_ELSE"
        setup.execution._reconcile_pending_order(
            fresh, risk=RiskController(), notifier=ServerChanNotifier(""),
        )
        assert sink.records, (
            "uncertainty with the lane flag OFF did not reach the sink"
        )

    def test_existing_pause_reason_preserved_on_escalation(
        self, tmp_path: Path,
    ) -> None:
        sink = _RecordingSink()
        setup = _Setup(
            tmp_path,
            _mandate(),
            sink=sink,
            broker_kwargs=dict(submit_status="SOMETHING_ELSE"),
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        risk = RiskController()
        risk.pause(
            "pre-existing owner pause", auto_resumable=False,
        )
        setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=risk,
            notifier=ServerChanNotifier(""),
        )
        # The pre-existing pause reason SURVIVES the escalation.
        assert risk.paused is True
        assert (risk.pause_reason or "").startswith(
            "pre-existing owner pause",
        )


# ---------------------------------------------------------------------------
# Typed record_outcome forwarding (live wrapper)
# ---------------------------------------------------------------------------


class TestTypedOutcomeForwarding:
    def test_counting_wrapper_forwards_typed_result(self) -> None:
        """Live fake wrappers must FORWARD the W1 typed result; when W1's
        enum is present the wrapper's return must equal the inner's."""
        if not _has_enum():
            pytest.fail("W1 OutcomeWriteResult missing (behavioural RED)")
        # Constructed lazily: the wrapper must be a transparent forwarder.
        inner = SimpleNamespace(
            record_outcome=lambda owner, fact: OutcomeWriteResult.APPLIED,
        )
        wrapper = _CountingHooks(inner)
        result = wrapper.record_outcome("owner", "fact")
        assert result is OutcomeWriteResult.APPLIED
