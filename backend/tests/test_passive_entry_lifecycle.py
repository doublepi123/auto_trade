# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Writer X final-remediation regressions: review2 five findings (attempt 3).

Every test here drives the REAL public surfaces only:

* ``TradeExecutionService.execute_passive_entry(*, ref, quote, broker,
  risk, notifier)`` — the NEW dedicated ref-entry (no owner/cash kwargs);
* ``TradeExecutionService.execute(...)`` — the generic entry (whose
  ``_trusted_passive_owner``/``_trusted_passive_cash`` kwargs must be
  GONE, so passive authority can never ride generic kwargs);
* the pending reconcile path (``_reconcile_pending_order`` — same method
  the runner loop drives) for late-callback failures.

RED phase: these run against the frozen review2 source where the public
API still is ``execute_passive_entry(*, owner, ..., cash)`` and the
generic kwargs channel exists. Where the OLD public call still works, the
tests reproduce the ACTUAL unsafe mutation (e.g. the $5001.8885 budget
overshoot, the missing pause) so the RED is behavioural, never a mere
TypeError. Thin per-version invocation adapters are confined to
``_drive_dedicated()`` and ``_drive_kwargs_exploit()``; every acceptance
assertion is version-independent.
"""
from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))
from services_fail_hooks import (  # noqa: E402
    _FailClaimCommitHooks,
    _FailOwnerIntentHooks,
    _FailRecordOutcomeHooks,
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
from app.services import trade_execution_service as tes
from app.services.passive_allocation_service import PassiveAllocationService
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
TSLA = "TSLA.US"
SEC98_FIXED = Decimal("1.568")
SEC98_RATE = Decimal("0.0000641")

import inspect  # noqa: E402

_SIG = inspect.signature(TradeExecutionService.execute_passive_entry)
IS_NEW = "ref" in _SIG.parameters and "owner" not in _SIG.parameters
HAS_TRUSTED_KWARGS = any(
    p.name.startswith("_trusted_passive_")
    for p in inspect.signature(
        TradeExecutionService.execute,
    ).parameters.values()
)


# ---------------------------------------------------------------------------
# Fakes (durable journal, margin-read counter, fail-injection hooks)
# ---------------------------------------------------------------------------


class _Cash:
    __slots__ = (
        "amount", "currency", "request_started_at",
        "request_completed_at", "provenance",
    )

    def __init__(
        self,
        amount: Decimal,
        *,
        started: datetime | None = None,
        completed: datetime | None = None,
    ) -> None:
        self.amount = amount
        self.currency = "USD"
        self.request_started_at = started or (NOW - timedelta(seconds=2))
        self.request_completed_at = completed or (NOW - timedelta(seconds=1))
        self.provenance = passive_protocol.PASSIVE_CASH_PROVENANCE


class _Broker(BrokerGateway):
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
        self.next_order_id = "probe-x1"
        self.get_status_calls = 0
        self.submit_hook: Any = None

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_cash(self, currency: str | None = None) -> Decimal:
        return self.cash_amount

    def get_strict_usd_cash_snapshot(self) -> Any:
        now = self.clock()
        return _Cash(
            self.cash_amount,
            started=now - timedelta(seconds=2),
            completed=now - timedelta(seconds=1),
        )

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
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"submit|{symbol}|{side}|{quantity}|{price}"
                    f"|{self.next_order_id}\n",
                )
                fh.flush()
                import os as _os

                _os.fsync(fh.fileno())
        if self.submit_hook is not None:
            self.submit_hook()
        return OrderResult(
            self.next_order_id, symbol, side, quantity, price,
            self.next_status,
        )

    def get_order_status(self, order_id: str) -> Any:
        self.get_status_calls += 1
        from app.core.broker import OrderStatusResult

        return OrderStatusResult(
            broker_order_id=order_id,
            status=self.next_status,
            executed_quantity=Decimal("8"),
            executed_price=Decimal("600.00"),
        )


def _quote(price: float = 600.0) -> Quote:
    return Quote(
        PASSIVE_SYMBOL, price, price - 0.01, price + 0.01,
        "2026-09-30T15:00:00Z",
    )


def _pass_through_quote_check(
    _broker: BrokerGateway, _symbol: str, _action: str, price: Decimal,
) -> FinalOrderQuoteCheckResult:
    return FinalOrderQuoteCheckResult(
        executable_price=price, bid=price, ask=price,
    )


def _reprice_check(final_price: str):
    def check(
        _b: BrokerGateway, _s: str, _a: str, _p: Decimal,
    ) -> FinalOrderQuoteCheckResult:
        p = Decimal(final_price)
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
        "final_order_quote_check": _pass_through_quote_check,
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


class _Setup:
    def __init__(
        self,
        tmp: Path,
        mandate: PassiveMandate | None,
        *,
        cash: Decimal = Decimal("10000"),
        next_status: str = "SUBMITTED",
    ) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.engine: Engine = create_engine(
            f"sqlite:///{tmp / f'px_{uuid4().hex}.db'}",
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
        self.execution.passive_submit_hooks = self.passive.build_hook_bundle()
        self.broker = _Broker(
            cash=cash, clock=self.clock, journal=tmp / "broker.journal",
            next_status=next_status,
        )
        # Fresh-quote gate: pass the order price through by default. Tests
        # repricing (finding 1's 600 -> 625 arithmetic) override this.
        self.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _pass_through_quote_check
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

    # -- thin per-version invocation adapters (assertions identical) -------

    def begin_owner(self, ref: Any, token: str = "exec-x") -> Any:
        hooks = self.passive.build_hook_bundle()
        return hooks.begin_execution(ref, token)

    def drive_dedicated(
        self,
        ref: Any,
        *,
        risk: RiskController | None = None,
    ) -> OrderStatus | None:
        """The one trusted passive entry, driven directly (no facade)."""
        risk = risk if risk is not None else RiskController()
        entry = self.execution.execute_passive_entry
        if IS_NEW:
            return entry(
                ref=ref,
                quote=_quote(),
                broker=self.broker,
                risk=risk,
                notifier=ServerChanNotifier(""),
            )
        # OLD public API: begin + capture cash, then the owner-entry. The
        # facade is NOT used, so no facade rescue can mask the target.
        owner = self.begin_owner(ref)
        assert not isinstance(owner, passive_protocol.PassiveRejection), owner
        legacy_kwargs: dict[str, Any] = dict(
            owner=owner,
            quote=_quote(),
            broker=self.broker,
            risk=risk,
            notifier=ServerChanNotifier(""),
            cash=self.broker.get_strict_usd_cash_snapshot(),
        )
        return cast(Any, entry)(**legacy_kwargs)

    def drive_kwargs_exploit(self, owner: Any) -> OrderStatus | None:
        """Finding 1's exact old exploitation: generic kwargs authority.

        A valid owner installed through the generic ``_trusted_passive_*
        kwargs — NO lane marker, NO sized_quantity — rides the plain
        margin-sizing path: original 600 repriced to 625, margin sizing
        yields 8 shares, and the order SUBMITS with a passive budget of
        8*625 + 1.8885 SEC98 = 5001.8885 > the 5000 allotment, having
        both bypassed the passive allotment check and read margin.
        """
        exploit_kwargs: dict[str, Any] = dict(
            execution_context={},
            _trusted_passive_owner=owner,
            _trusted_passive_cash=_Cash(Decimal("10000")),
        )
        return cast(Any, self.execution.execute)(
            "BUY",
            PASSIVE_SYMBOL,
            _quote(),
            self.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            **exploit_kwargs,
        )


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tes, "is_trading_hours", lambda _m: True)


# ---------------------------------------------------------------------------
# Finding 1 (P0): generic kwargs authority bypass
# ---------------------------------------------------------------------------


class TestFindingOneKwargsAuthorityBypass:
    def test_exploit_via_old_kwargs_is_gone(self, tmp_path: Path) -> None:
        """The exact known exploitation: valid owner installed via the
        generic kwargs channel (no lane, no sized), original 600 repriced
        to 625 — margin sizing submits 8 shares with a passive budget of
        8*625 + SEC98 1.8885 = 5001.8885 > the 5000 allotment, and the
        margin endpoint was read on a passive order.

        OLD (RED): the exploit SUBMITS (behavioral, not a TypeError).
        NEW (GREEN): the kwargs channel no longer exists as an authority
        path — the exploit call cannot submit.
        """
        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _reprice_check("625")
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        owner = setup.begin_owner(ref)
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        submitted = False
        breach_details = ""
        if HAS_TRUSTED_KWARGS:
            status = setup.drive_kwargs_exploit(owner)
            submitted = (
                status is not None
                and status.status in {"SUBMITTED", "FILLED", "PARTIAL_FILLED"}
            )
            if submitted:
                qty = setup.broker.submissions[0][2]
                price = setup.broker.submissions[0][3]
                fee = SEC98_FIXED + SEC98_RATE * price * qty
                breach_details = (
                    f"exploit submitted {qty} x {price} = passive budget "
                    f"{qty * price + fee} > 5000 allotment; margin_reads="
                    f"{setup.broker.margin_reads}"
                )
        # Acceptance (version-independent): no kwargs channel may exist.
        assert not HAS_TRUSTED_KWARGS, (
            "generic execute() still exposes _trusted_passive_* kwargs: "
            f"public kwargs authority channel must be removed ({breach_details})"
        )
        assert submitted is False, breach_details
        assert setup.broker.submissions == [], breach_details

    def test_generic_context_smuggle_still_denied_zero_margin(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        owner = setup.begin_owner(ref)
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
                "passive_submit_owner": owner,
                "passive_cash_evidence": _Cash(Decimal("10000")),
                "passive_submit_right_won": True,
            },
        )
        assert status is not None and status.status == "SKIPPED"
        assert setup.broker.submissions == []
        assert setup.broker.margin_reads == 0

    def test_dedicated_ref_entry_accepts_without_lane_metadata(
        self, tmp_path: Path,
    ) -> None:
        """The dedicated ref-entry ALWAYS uses the validated passive
        policy/intent even if caller metadata omits the lane: the facade
        normally supplies context, but the dedicated path must not DEPEND
        on it (finding 1 + handoff 'context is output audit only')."""
        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _reprice_check("620")
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        status = setup.drive_dedicated(ref)
        assert status is not None and status.status == "SUBMITTED", (
            getattr(status, "reason", None)
        )
        assert setup.broker.submissions == [
            (PASSIVE_SYMBOL, "BUY", Decimal("8"), Decimal("620.00")),
        ]
        assert setup.broker.margin_reads == 0
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN


# ---------------------------------------------------------------------------
# Finding 2 (P0): dedicated ref-entry owns the WHOLE lifecycle
# ---------------------------------------------------------------------------


class TestFindingTwoDedicatedLifecycle:
    def test_zero_cash_burns_and_ref_replay_denied(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate(), cash=Decimal("0"))
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        first = setup.drive_dedicated(ref)
        assert first is not None and first.status not in {
            "SUBMITTED", "FILLED", "PARTIAL_FILLED",
        }
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        # A definite pre-broker refusal under a WON owner must burn
        # NO_SUBMIT — never leave CHECKING replayable.
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT, (
            f"zero-cash denial left state {row.submit_state!r}"
        )
        # Replay of the SAME ref may never submit.
        setup.broker.cash_amount = Decimal("10000")
        second = setup.drive_dedicated(ref)
        assert second is None or second.status not in {
            "SUBMITTED", "FILLED", "PARTIAL_FILLED",
        }
        assert setup.broker.submissions == []

    def test_unknown_receipt_pauses_with_real_risk(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path, _mandate(), next_status="SOMETHING_ELSE",
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        risk = RiskController()
        status = setup.drive_dedicated(ref, risk=risk)
        assert status is not None and status.status == "UNCERTAIN"
        # Finding 2's exact complaint: the direct UNKNOWN path used
        # risk=None; the pause must actually happen.
        assert risk.paused is True, (
            "direct UNKNOWN receipt returned UNCERTAIN without pausing"
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_submit_cas_commit_error_is_uncertain_paused(
        self, tmp_path: Path,
    ) -> None:
        """Submit CAS commit fails AFTER the CAS rowcount: no broker call,
        durability unproven -> UNCERTAIN + non-auto pause (finding 2's
        'submit CAS commit error returns uncertain no pause')."""
        setup = _Setup(tmp_path, _mandate())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        base_bundle = setup.passive.build_hook_bundle()

        failing = _FailClaimCommitHooks(base_bundle)
        setup.execution.passive_submit_hooks = failing  # type: ignore[assignment]
        risk = RiskController()
        status = setup.drive_dedicated(ref, risk=risk)
        assert status is not None and status.status == "UNCERTAIN"
        assert risk.paused is True, (
            "submit-CAS commit error returned UNCERTAIN without pause"
        )
        assert setup.broker.submissions == []

    def test_denial_receipt_record_failure_is_uncertain_paused(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate(), cash=Decimal("0"))
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        setup.execution.passive_submit_hooks = (  # type: ignore[assignment]
            _FailRecordOutcomeHooks(
                setup.passive.build_hook_bundle(),
                fail_outcomes={passive_protocol.SUBMIT_STATE_NO_SUBMIT},
            )
        )
        risk = RiskController()
        status = setup.drive_dedicated(ref, risk=risk)
        assert status is not None and status.status == "UNCERTAIN"
        assert risk.paused is True
        assert setup.broker.submissions == []

    def test_post_broker_processing_failure_preserves_id(
        self, tmp_path: Path,
    ) -> None:
        def boom_record(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("orders table down")

        setup = _Setup(tmp_path, _mandate())
        setup.execution._record_order = boom_record  # type: ignore[method-assign]
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        risk = RiskController()
        status = setup.drive_dedicated(ref, risk=risk)
        assert status is not None and status.status == "UNCERTAIN"
        assert risk.paused is True
        assert setup.broker.submissions, "the broker mutation happened"
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row.bound_broker_order_id == "probe-x1"


# ---------------------------------------------------------------------------
# Finding 4 (P1): pending critical persistence failures escalate even when
# the owner cannot be resolved
# ---------------------------------------------------------------------------


class TestFindingFourPendingCriticalFailures:
    def _reconcile(self, setup: _Setup, risk: RiskController) -> None:
        pending = setup.execution.pending_order
        assert pending is not None
        setup.execution._reconcile_pending_order(  # type: ignore[reportPrivateUsage]
            pending, risk=risk, notifier=ServerChanNotifier(""),
        )

    def test_progress_callback_failure_pauses_even_without_owner(
        self, tmp_path: Path,
    ) -> None:
        """FILLED arrives; the passive progress callback escalates because
        the owner ref cannot be restored (malformed). Even without an
        owner, the failure must pause: the pending/reconciliation proof
        (order id, memory quantity) is kept and an unresolved-reference
        incident is recorded."""
        import time as _time

        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _pass_through_quote_check
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        outcome_status = setup.drive_dedicated(ref)
        assert outcome_status is not None
        assert outcome_status.status in {"SUBMITTED"}, outcome_status.reason
        pending = setup.execution.pending_order
        assert pending is not None
        # Corrupt the durable ref: the owner lookup must fail.
        broken = pending.__class__(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=None,
            submitted_at=_time.monotonic(),
            passive_owner_ref="not:a-valid-ref",
        )
        setup.execution._pending_orders_by_id[pending.broker_order_id] = broken
        setup.broker.next_status = "FILLED"
        risk = RiskController()
        self._reconcile(setup, risk)
        assert risk.paused is True, (
            "pending progress-callback failure with an unresolvable owner "
            "did not pause"
        )
        row = setup.row()
        assert row is not None
        assert row.bound_broker_order_id == "probe-x1"

    def test_owner_lookup_db_error_escalates_not_debug(
        self, tmp_path: Path,
    ) -> None:
        """The pending ref is VALID but the owner lookup raises (DB error
        propagates, not catch->None): the escalation must still pause."""
        import time as _time

        setup = _Setup(tmp_path, _mandate())
        setup.execution._final_order_quote_check = (  # type: ignore[method-assign]
            _pass_through_quote_check
        )
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        status = setup.drive_dedicated(ref)
        assert status is not None and status.status == "SUBMITTED"
        setup.execution.passive_submit_hooks = (  # type: ignore[assignment]
            _FailOwnerIntentHooks(setup.passive.build_hook_bundle())
        )
        pending = setup.execution.pending_order
        assert pending is not None
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
        setup.broker.next_status = "FILLED"
        risk = RiskController()
        self._reconcile(setup, risk)
        assert risk.paused is True


# ---------------------------------------------------------------------------
# Evidence cleanup: real killed-child with a REAL record_order writer and
# os._exit(9) BEFORE ordinary order recording
# ---------------------------------------------------------------------------


_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {backend_root!r})
    mode, db_path, journal_path, phase = (
        sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    )
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("AUTO_TRADE_ENV", "test")

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    import json
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.notify import ServerChanNotifier
    from app.core.risk import RiskController
    from app.database import _ensure_passive_mandates_table
    from app.domain.passive_allocation import protocol as passive_protocol
    from app.models import Base, OrderRecord, PassiveMandate
    from app.domain.passive_allocation import policy as passive_policy
    from app.domain.passive_allocation.model import (
        PASSIVE_LANE, PASSIVE_SYMBOL, POLICY_VERSION,
    )
    from app.services.passive_allocation_service import (
        PassiveAllocationService,
    )
    from app.services import trade_execution_service as _tes
    _tes.is_trading_hours = lambda _m: True
    from app.services.trade_execution_service import (
        FinalOrderQuoteCheckResult, TradeExecutionService,
    )
    from app.core.broker import Quote

    NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)

    class Cash:
        def __init__(self, amount):
            self.amount = amount
            self.currency = "USD"
            self.request_started_at = NOW - timedelta(seconds=2)
            self.request_completed_at = NOW - timedelta(seconds=1)
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
            from app.core.broker import OrderResult
            self.submitted += 1
            order_id = f"crash-x{{self.submitted}}"
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps({{
                    "event": "submit", "symbol": symbol, "side": side,
                    "quantity": str(quantity), "price": str(price),
                    "order_id": order_id,
                }}) + "\\n")
                fh.flush()
                os.fsync(fh.fileno())
            return OrderResult(order_id, symbol, side, quantity, price, "SUBMITTED")

    def qchk(_b, _s, _a, price):
        return FinalOrderQuoteCheckResult(executable_price=price, bid=price, ask=price)

    def real_record_order(
        order_id, symbol, side, qty, price, status="SUBMITTED",
        filled_at=None, executed_quantity=None, executed_price=None,
        metadata=None,
    ):
        # REAL orders-table write on the isolated DB, then fsync-journal it
        # so the parent can prove the record predates the kill.
        with _sessions() as db:
            db.add(OrderRecord(
                broker_order_id=order_id, symbol=symbol, side=side,
                quantity=qty, price=price, status=status,
                config_snapshot=json.dumps((metadata or {{}}).get("config_snapshot", "")),
            ))
            db.commit()
        with open(journal_path, "a") as fh:
            fh.write(json.dumps({{"event": "order_recorded", "order_id": order_id}}) + "\\n")
            fh.flush()
            os.fsync(fh.fileno())

    def record_and_crash(*a, **k):
        # Crash point for phase="crash-before-record": by this moment the
        # broker mutation is journaled AND the mandate ORDER_KNOWN bind is
        # committed (see _record_passive_receipt running before
        # _process_submitted_order); the ordinary orders-table record has
        # NOT run. Die hard so no finally/except can complete it.
        if phase == "crash-before-record":
            with open(journal_path, "a") as fh:
                fh.write(json.dumps({{"event": "crash_before_record"}}) + "\\n")
                fh.flush()
                os.fsync(fh.fileno())
            os._exit(9)
        return real_record_order(*a, **k)

    engine = create_engine(f"sqlite:///{{db_path}}", connect_args={{"timeout": 30}})
    _sessions = sessionmaker(bind=engine, expire_on_commit=False)

    execution = TradeExecutionService(
        record_order=record_and_crash,
        update_order_status=lambda *a, **k: None,
        record_risk_event=lambda *a, **k: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        final_order_quote_check=qchk,
    )
    passive = PassiveAllocationService(
        execution=execution, session_factory=_sessions,
        lane_enabled_reader=lambda: True,
        paper_account_confirmed_reader=lambda: True, clock=lambda: NOW,
    )
    execution.passive_submit_hooks = passive.build_hook_bundle()

    class CrashBroker(Broker):
        def submit_limit_order(self, symbol, side, quantity, price):
            result = super().submit_limit_order(symbol, side, quantity, price)
            if phase == "accepted-before-return":
                os._exit(9)   # mutation journaled; die BEFORE return/bind
            return result

    broker = CrashBroker(journal_path)
    quote = Quote("SPY.US", 600.0, 599.99, 600.01, "2026-09-30T15:00:00Z")

    if mode == "init":
        Base.metadata.create_all(engine)
        _ensure_passive_mandates_table(engine)
        with _sessions() as db:
            db.add(PassiveMandate(
                lane=PASSIVE_LANE, policy_version=POLICY_VERSION,
                symbol=PASSIVE_SYMBOL, status="ACTIVE", allotment_usd=5000.0,
                risk_model="FULL_PRINCIPAL",
                exemptions=",".join(passive_policy.REQUIRED_EXEMPTIONS),
                review_interval_months=6,
                entry_authorisation_available=True,
                approved_at=NOW, approved_by="owner",
                approval_reason="owner approval", order_binding="paper-only",
            ))
            db.commit()
        print("INIT OK", flush=True)
        os._exit(0)

    if mode == "full":
        ref = passive.reserve_entry(price=Decimal("600"))
        if isinstance(ref, str):
            print("DONE refused", ref[:60], 0, flush=True)
            os._exit(0)
        if IS_NEW:
            status = execution.execute_passive_entry(
                ref=ref, quote=quote, broker=broker,
                risk=RiskController(), notifier=ServerChanNotifier(""),
            )
        else:
            hooks = passive.build_hook_bundle()
            owner = hooks.begin_execution(ref, "exec-child")
            status = execution.execute_passive_entry(
                owner=owner, quote=quote, broker=broker,
                risk=RiskController(), notifier=ServerChanNotifier(""),
                cash=Cash(Decimal("10000")),
            )
        print(
            "DONE", getattr(status, "status", None), broker.submitted,
            (getattr(status, "reason", "") or "")[:80], flush=True,
        )
        os._exit(0)
    """,
)


def _drive_child(backend_root: str, tmp: Path, mode: str, phase: str = "") -> str:
    import os
    import subprocess
    import sys

    db_path = tmp / "crash.db"
    journal = tmp / "broker.journal"
    harness = tmp / f"child_{mode.replace('/', '_')}_{phase or 'none'}.py"
    body = _CHILD.format(backend_root=backend_root)
    body = body.replace(
        'if IS_NEW:', f'if {IS_NEW}:',
    )
    harness.write_text(body)
    result = subprocess.run(
        [
            sys.executable, str(harness), mode, str(db_path),
            str(journal), phase,
        ],
        capture_output=True, text=True, timeout=90,
        env={**os.environ, "AUTO_TRADE_ENV": "test"},
        cwd=backend_root,
    )
    if result.returncode not in (0, 9):
        raise AssertionError(
            f"child {mode}/{phase} rc={result.returncode}: "
            f"{result.stderr[-2000:]}"
        )
    return result.stdout


def _journal_events(journal: Path, event: str) -> int:
    if not journal.exists():
        return 0
    return sum(
        1
        for line in journal.read_text().splitlines()
        if f'"event": "{event}"' in line
    )


class TestRealCrashBeforeOrdinaryRecord:
    def test_exit9_after_real_bind_before_record_no_replay(
        self, tmp_path: Path,
    ) -> None:
        """Real child: the broker mutation is fsync-journaled and the
        mandate ORDER_KNOWN bind is committed, then os._exit(9) fires
        BEFORE the ordinary orders-table record_order completes. A NEW
        instance over the same durable DB/journal must not replay."""
        backend_root = str(Path(__file__).resolve().parents[1])
        out = _drive_child(backend_root, tmp_path, "init")
        assert "INIT OK" in out
        out = _drive_child(
            backend_root, tmp_path, "full", "crash-before-record",
        )
        journal = tmp_path / "broker.journal"
        assert _journal_events(journal, "submit") == 1, out
        # The crash marker proves we died INSIDE record_order, before the
        # orders-table write could complete...
        assert _journal_events(journal, "crash_before_record") == 1
        assert _journal_events(journal, "order_recorded") == 0
        # ...while the mandate bind (ORDER_KNOWN with the broker id) had
        # already committed.
        from sqlalchemy import text

        engine = create_engine(f"sqlite:///{tmp_path / 'crash.db'}")
        with engine.connect() as c:
            state, bound = c.execute(
                text(
                    "SELECT submit_state, bound_broker_order_id "
                    "FROM passive_mandates",
                ),
            ).one()
        assert state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN, state
        assert bound and bound.startswith("crash-x"), bound
        # NEW instance over the same durable DB/journal: no replay.
        restart = _drive_child(backend_root, tmp_path, "full")
        assert "DONE" in restart
        assert _journal_events(journal, "submit") == 1  # NO duplicate

    def test_normal_full_child_records_order(self, tmp_path: Path) -> None:
        backend_root = str(Path(__file__).resolve().parents[1])
        out = _drive_child(backend_root, tmp_path, "init")
        assert "INIT OK" in out
        out = _drive_child(backend_root, tmp_path, "full")
        assert "DONE" in out
        journal = tmp_path / "broker.journal"
        assert _journal_events(journal, "submit") == 1
        assert _journal_events(journal, "order_recorded") == 1
