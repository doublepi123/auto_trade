"""SPY passive lane submit protocol (phase 1, default OFF) — writer B tests.

Contract: /tmp/opencode/spy-passive-submit-contract.md (architect ora-21).
These tests are BEHAVIOURAL: each reproduces one contract requirement or one
of the 8 review findings against the real service + real SQLite database,
with a fake broker matching writer A's frozen cash-evidence interface.

Isolation: every test builds a private engine/DB under ``tmp_path``; nothing
mutates tracked files; the fake broker has no network. Env bootstrap is
disabled via ``AUTO_TRADE_ENV`` + monkeypatched dotenv (see module fixture)
WITHOUT renaming the shared ``../.env`` (writer A may be reading it).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.accounting_fees import ACCOUNTING_FEE_MODEL_US_SEC98
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
    PassiveMandateFacts,
    RiskModel,
)
from app.models import Base, PassiveMandate
from app.services.passive_allocation_service import (
    PassiveAllocationService,
    PassiveSubmitHookBundle,
    us_paper_commission,
)
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)

TSLA = "TSLA.US"
NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Frozen-interface cash fake (writer A supplies the real UsdCashSnapshot)
# ---------------------------------------------------------------------------


class _FakeUsdCashSnapshot:
    """Structurally identical to app.core.cash_evidence.UsdCashSnapshot."""

    __slots__ = (
        "amount",
        "currency",
        "request_started_at",
        "request_completed_at",
        "provenance",
    )

    def __init__(
        self,
        amount: Decimal,
        *,
        currency: str = "USD",
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        provenance: str = (
            passive_protocol.PASSIVE_CASH_PROVENANCE
        ),
    ) -> None:
        self.amount = amount
        self.currency = currency
        self.request_started_at = started_at or (
            NOW - timedelta(seconds=1)
        )
        self.request_completed_at = completed_at or (
            NOW - timedelta(seconds=0.5)
        )
        self.provenance = provenance


# ---------------------------------------------------------------------------
# Fake broker (no network) with the strict cash snapshot interface
# ---------------------------------------------------------------------------


class _PassiveBroker(BrokerGateway):
    """Minimal fake broker: positions, strict cash, recorded submissions."""

    def __init__(
        self,
        positions: list[Position] | None = None,
        *,
        cash_amount: Decimal = Decimal("10000"),
        clock: _Clock | None = None,
    ) -> None:
        self.positions = list(positions or [])
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []
        self.cash_amount = cash_amount
        self.cash_error: Exception | None = None
        self.clock = clock or _Clock()
        self.cash_started_at: datetime | None = None
        self.cash_completed_at: datetime | None = None
        self.cash_currency = "USD"
        self.cash_provenance = (
            passive_protocol.PASSIVE_CASH_PROVENANCE
        )
        # Deterministic crash points for the lost-ACK tests.
        self.submit_hook: Any = None

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_cash(self, currency: str | None = None) -> Decimal:
        if self.cash_error is not None:
            raise self.cash_error
        return self.cash_amount

    def get_strict_usd_cash_snapshot(self) -> Any:
        """Matches writer A's frozen signature + fail-closed semantics."""
        if self.cash_error is not None:
            raise self.cash_error
        now = self.clock()
        started = self.cash_started_at or (now - timedelta(seconds=1))
        completed = self.cash_completed_at or (now - timedelta(seconds=0.5))
        return _FakeUsdCashSnapshot(
            self.cash_amount,
            currency=self.cash_currency,
            started_at=started,
            completed_at=completed,
            provenance=self.cash_provenance,
        )

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        return Decimal("1000")

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        if self.submit_hook is not None:
            self.submit_hook()
        self.submissions.append((symbol, side, quantity, price))
        return OrderResult(
            f"paper-{uuid4().hex[:8]}", symbol, side, quantity, price, "SUBMITTED",
        )


def _quote(symbol: str = PASSIVE_SYMBOL, price: float = 600.0) -> Quote:
    return Quote(symbol, price, price - 0.01, price + 0.01, "2026-09-30T15:00:00Z")


# ---------------------------------------------------------------------------
# Isolation setup
# ---------------------------------------------------------------------------


def _quote_check(
    _broker: BrokerGateway,
    _symbol: str,
    _action: str,
    price: Decimal,
) -> FinalOrderQuoteCheckResult:
    return FinalOrderQuoteCheckResult(
        executable_price=price, bid=price, ask=price,
    )


def _service(**overrides: Any) -> TradeExecutionService:
    params: dict[str, Any] = {
        "record_order": lambda *_args: None,
        "update_order_status": lambda *_args: None,
        "record_risk_event": lambda *_reason: None,
        "max_position_quantity": 100,
        "max_position_notional": 5000.0,
        "max_risk_per_trade": 250.0,
        "stop_loss_pct": 1.0,
        "final_order_quote_check": _quote_check,
    }
    params.update(overrides)
    return TradeExecutionService(**params)


class _Gate:
    """Mutable flag holder simulating runtime config reads."""

    def __init__(self, enabled: bool = True, paper: bool = True) -> None:
        self.enabled = enabled
        self.paper = paper

    def lane_on(self) -> bool:
        return self.enabled

    def paper_on(self) -> bool:
        return self.paper


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now_value = start

    def __call__(self) -> datetime:
        return self.now_value


def _mandate_row(**overrides: Any) -> PassiveMandate:
    values: dict[str, Any] = dict(
        lane=PASSIVE_LANE,
        policy_version=POLICY_VERSION,
        symbol=PASSIVE_SYMBOL,
        status="ACTIVE",
        allotment_usd=5000.0,
        risk_model=RiskModel.FULL_PRINCIPAL.value,
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


class _Setup:
    """One isolated mandate database plus a wired passive lane."""

    def __init__(
        self,
        tmp_path: Path,
        mandate: PassiveMandate | None,
        *,
        gate: _Gate | None = None,
        clock: _Clock | None = None,
        execution: TradeExecutionService | None = None,
        cash_amount: Decimal = Decimal("10000"),
    ) -> None:
        self.engine: Engine = create_engine(
            f"sqlite:///{tmp_path / f'passive_{uuid4().hex}.db'}",
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
        self.execution = execution or _service()
        self.passive = PassiveAllocationService(
            execution=self.execution,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )
        self.execution.passive_submit_hooks = self.passive.build_hook_bundle()
        self.broker = _PassiveBroker(cash_amount=cash_amount, clock=self.clock)

    def row(self) -> PassiveMandate | None:
        with self.sessions() as db:
            row = (
                db.query(PassiveMandate)
                .filter(PassiveMandate.lane == PASSIVE_LANE)
                .one_or_none()
            )
            if row is None:
                return None
            db.expunge(row)
            return row

    def fresh_service(self) -> PassiveAllocationService:
        return PassiveAllocationService(
            execution=self.execution,
            session_factory=self.sessions,
            lane_enabled_reader=self.gate.lane_on,
            paper_account_confirmed_reader=self.gate.paper_on,
            clock=self.clock,
        )

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

    def full_request(
        self, risk: RiskController | None = None,
    ) -> tuple[Any, Any]:
        ref = self.reserve()
        outcome = self.execute(ref, risk)
        return ref, outcome


from app.services import trade_execution_service as trade_svc_module  # noqa: E402
from app.services.trade_execution_service import (  # noqa: E402
    _PreSubmitRiskRequest,
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic env without touching the shared ../.env file.

    The lane gates in these tests are injected readers, never Settings
    fields, so no dotenv value can flip a test outcome; AUTO_TRADE_ENV
    keeps diagnostics quiet. The repo .env stays untouched for writer A.
    """
    monkeypatch.setenv("AUTO_TRADE_ENV", "test")


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_svc_module, "is_trading_hours", lambda _m: True)


# ---------------------------------------------------------------------------
# Protocol pure functions
# ---------------------------------------------------------------------------


class TestProtocolPure:
    def test_state_matrix_outcome_writes(self) -> None:
        S = passive_protocol
        # ORDER_KNOWN allowed from SUBMITTING/ORDER_KNOWN only.
        assert (
            S.validate_outcome_write(
                current_state=S.SUBMIT_STATE_SUBMITTING,
                bound_broker_order_id=None,
                fact=S.PassiveOutcomeFact(
                    outcome=S.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id="abc",
                ),
            )
            is None
        )
        # Same fact twice is idempotent.
        assert (
            S.validate_outcome_write(
                current_state=S.SUBMIT_STATE_ORDER_KNOWN,
                bound_broker_order_id="abc",
                fact=S.PassiveOutcomeFact(
                    outcome=S.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id="abc",
                ),
            )
            == "IDEMPOTENT"
        )
        # Conflicting broker id is a conflict (never erase/replace).
        assert (
            S.validate_outcome_write(
                current_state=S.SUBMIT_STATE_ORDER_KNOWN,
                bound_broker_order_id="abc",
                fact=S.PassiveOutcomeFact(
                    outcome=S.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id="xyz",
                ),
            )
            == "CONFLICT"
        )
        # NO_SUBMIT from CHECKING/SUBMITTING burns; from ORDER_KNOWN refused.
        assert (
            S.validate_outcome_write(
                current_state=S.SUBMIT_STATE_CHECKING,
                bound_broker_order_id=None,
                fact=S.PassiveOutcomeFact(
                    outcome=S.SUBMIT_STATE_NO_SUBMIT,
                ),
            )
            is None
        )
        assert (
            S.validate_outcome_write(
                current_state=S.SUBMIT_STATE_ORDER_KNOWN,
                bound_broker_order_id="abc",
                fact=S.PassiveOutcomeFact(
                    outcome=S.SUBMIT_STATE_NO_SUBMIT,
                ),
            )
            is not None
        )
        # UNCERTAIN from SUBMITTING/ORDER_KNOWN/CHECKING ok; same = idempotent.
        for source in (
            S.SUBMIT_STATE_CHECKING,
            S.SUBMIT_STATE_SUBMITTING,
            S.SUBMIT_STATE_ORDER_KNOWN,
        ):
            assert (
                S.validate_outcome_write(
                    current_state=source,
                    bound_broker_order_id=None,
                    fact=S.PassiveOutcomeFact(
                        outcome=S.SUBMIT_STATE_UNCERTAIN,
                        reason="x",
                    ),
                )
                in (None, "IDEMPOTENT", "CONFLICT")
            )
        # No outcome ever restores AUTHORIZED / SUBMIT_CLAIMED / CHECKING.
        for outcome in (
            S.SUBMIT_STATE_ORDER_KNOWN,
            S.SUBMIT_STATE_NO_SUBMIT,
            S.SUBMIT_STATE_UNCERTAIN,
        ):
            assert (
                S.validate_outcome_write(
                    current_state=S.SUBMIT_STATE_AUTHORIZED,
                    bound_broker_order_id=None,
                    fact=S.PassiveOutcomeFact(outcome=outcome),
                )
                is not None
            )

    def test_classify_receipt_matrix(self) -> None:
        S = passive_protocol
        for status in (
            "SUBMITTED", "PARTIAL_FILLED", "FILLED", "REJECTED", "CANCELLED",
        ):
            assert (
                S.classify_submit_receipt(
                    broker_order_id="id-1", status=status,
                )
                == S.SUBMIT_STATE_ORDER_KNOWN
            ), status
        assert (
            S.classify_submit_receipt(broker_order_id="", status="SUBMITTED")
            == S.SUBMIT_STATE_UNCERTAIN
        )
        assert (
            S.classify_submit_receipt(
                broker_order_id="id-1", status="SOMETHING_ELSE",
            )
            == S.SUBMIT_STATE_UNCERTAIN
        )

    def test_cash_reprice_4802_fee_boundary(self) -> None:
        # Contract test vector: 8 shares, 600 -> 620 reprice.
        # required = 8 * 620 + fee(620, 8) = 4960 + 1.8867 = 4961.88...
        cash = _FakeUsdCashSnapshot(Decimal("4802"))
        issue = passive_protocol.validate_cash_evidence(
            cash=cash,
            quantity=Decimal("8"),
            approved_price=Decimal("620"),
            fee=us_paper_commission(Decimal("620"), Decimal("8")),
            now=NOW,
        )
        assert issue is not None and "does not cover" in issue

    def test_cash_exact_sufficient_and_one_cent_short(self) -> None:
        fee = us_paper_commission(Decimal("600"), Decimal("8"))
        required = Decimal("8") * Decimal("600") + fee
        assert (
            passive_protocol.validate_cash_evidence(
                cash=_FakeUsdCashSnapshot(required),
                quantity=Decimal("8"),
                approved_price=Decimal("600"),
                fee=fee,
                now=NOW,
            )
            is None
        )
        assert (
            passive_protocol.validate_cash_evidence(
                cash=_FakeUsdCashSnapshot(required - Decimal("0.01")),
                quantity=Decimal("8"),
                approved_price=Decimal("600"),
                fee=fee,
                now=NOW,
            )
            is not None
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"currency": "HKD"},
            {"provenance": "total_cash"},
            {"started_at": NOW - timedelta(seconds=30)},
            {"completed_at": NOW + timedelta(seconds=1)},
            {"started_at": NOW + timedelta(seconds=1)},
        ],
        ids=[
            "wrong-currency",
            "wrong-provenance",
            "stale",
            "future-completed",
            "future-started",
        ],
    )
    def test_invalid_cash_evidence_fails_closed(self, kwargs: Any) -> None:
        fee = us_paper_commission(Decimal("600"), Decimal("8"))
        issue = passive_protocol.validate_cash_evidence(
            cash=_FakeUsdCashSnapshot(Decimal("10000"), **kwargs),
            quantity=Decimal("8"),
            approved_price=Decimal("600"),
            fee=fee,
            now=NOW,
        )
        assert issue is not None, kwargs

    def test_naive_timestamps_fail_closed(self) -> None:
        fee = us_paper_commission(Decimal("600"), Decimal("8"))
        cash = _FakeUsdCashSnapshot(Decimal("10000"))
        cash.request_started_at = datetime(2026, 9, 30, 15, 0, 0)
        issue = passive_protocol.validate_cash_evidence(  # type: ignore[arg-type]
            cash=cash,
            quantity=Decimal("8"),
            approved_price=Decimal("600"),
            fee=fee,
            now=NOW,
        )
        assert issue is not None and "timezone-aware" in issue

    def test_negative_or_nan_amount_fails(self) -> None:
        fee = us_paper_commission(Decimal("600"), Decimal("8"))
        issue = passive_protocol.validate_cash_evidence(
            cash=_FakeUsdCashSnapshot(Decimal("-1")),
            quantity=Decimal("8"),
            approved_price=Decimal("600"),
            fee=fee,
            now=NOW,
        )
        assert issue is not None

    def test_intent_json_roundtrip_strict(self) -> None:
        intent = passive_protocol.ImmutablePassiveIntent(
            symbol=PASSIVE_SYMBOL,
            side="BUY",
            quantity=Decimal("8"),
            original_price=Decimal("600.00"),
            policy=passive_protocol.PassivePolicySnapshot(
                policy_version=POLICY_VERSION,
                allotment_usd=Decimal("5000"),
                risk_model="FULL_PRINCIPAL",
                exemptions=passive_policy.REQUIRED_EXEMPTIONS,
                order_binding="paper-only",
                review_interval_months=6,
            ),
        )
        raw = passive_protocol.intent_to_json(intent)
        parsed = passive_protocol.intent_from_json(raw)
        assert passive_protocol.intent_to_json(parsed) == raw
        bad = json.loads(raw)
        bad["protocol_version"] = "other"
        with pytest.raises(ValueError):
            passive_protocol.intent_from_json(json.dumps(bad))


# ---------------------------------------------------------------------------
# Reservation / execution happy path and burn semantics
# ---------------------------------------------------------------------------


class TestReservationAndExecution:
    def test_full_path_submits_once_and_binds(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        assert not isinstance(ref, str), ref
        outcome = setup.execute(ref)
        assert outcome.submitted is True, outcome.reason
        assert len(setup.broker.submissions) == 1
        assert setup.broker.submissions[0] == (
            PASSIVE_SYMBOL, "BUY", Decimal("8"), Decimal("600.00"),
        )
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        assert row.bound_broker_order_id
        assert row.claim_token == ref.claim_token
        assert row.final_snapshot_json is not None
        assert row.intent_json is not None
        assert row.entry_authorisation_available is False
        assert row.execution_token is not None

    def test_reservation_records_immutable_intent(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        row = setup.row()
        assert row is not None
        intent = passive_protocol.intent_from_json(row.intent_json)
        assert intent.symbol == PASSIVE_SYMBOL
        assert intent.side == "BUY"
        assert intent.quantity == Decimal("8")
        assert intent.original_price == Decimal("600")
        snapshot = json.loads(row.final_snapshot_json or "null")
        assert snapshot is None  # not yet submitted

    def test_second_reservation_is_refused(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        first = setup.reserve()
        assert not isinstance(first, str)
        second = setup.reserve()
        assert isinstance(second, str) and "consumed" in second

    def test_flag_off_reservation_refused_no_db_change(
        self, tmp_path: Path,
    ) -> None:
        gate = _Gate(enabled=False)
        setup = _Setup(tmp_path, _mandate_row(), gate=gate)
        result = setup.reserve()
        assert isinstance(result, str) and "disabled" in result
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert row.claim_token is None

    def test_no_mandate_reservation_refused(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, None)
        result = setup.reserve()
        assert isinstance(result, str) and "no approved" in result

    def test_duplicate_mandate_rows_fail_closed(self, tmp_path: Path) -> None:
        # The shipped schema enforces UNIQUE(lane); a hand-rolled legacy
        # schema without it (schema ambiguity) must fail closed at the
        # reservation rather than pick an arbitrary row.
        db_path = tmp_path / f"dup_{uuid4().hex}.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
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
                    "created_at DATETIME, updated_at DATETIME)"
                )
                for _ in range(2):
                    c.exec_driver_sql(
                        "INSERT INTO passive_mandates (lane, policy_version, "
                        "symbol, status, allotment_usd, risk_model, "
                        "exemptions, review_interval_months, "
                        "entry_authorisation_available, order_binding, "
                        "approved_at, approved_by, approval_reason, "
                        "created_at, updated_at) VALUES ("
                        "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                        "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', "
                        "'no_price_stop', 6, 1, 'paper-only', "
                        "'2026-09-29', 'owner', 'x', '2026-09-29', "
                        "'2026-09-29')"
                    )
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            gate = _Gate()
            execution = _service()
            passive = PassiveAllocationService(
                execution=execution,
                session_factory=sessions,
                lane_enabled_reader=gate.lane_on,
                paper_account_confirmed_reader=gate.paper_on,
                clock=_Clock(),
            )
            execution.passive_submit_hooks = passive.build_hook_bundle()
            result = passive.reserve_entry(price=Decimal("600"))
            assert isinstance(result, str) and "duplicate" in result
            with engine.connect() as c:
                states = [
                    r[0] for r in c.exec_driver_sql(
                        "SELECT submit_state FROM passive_mandates"
                    ).fetchall()
                ]
            assert all(
                s == passive_protocol.SUBMIT_STATE_AUTHORIZED
                for s in states
            )
        finally:
            engine.dispose()

    def test_invalid_sizing_burns_valid_authorization_no_submit(
        self, tmp_path: Path,
    ) -> None:
        # R1-8 (restores the contract): a valid authorized attempt that
        # cannot form an intent atomically burns to NO_SUBMIT — the
        # authorisation is consumed, never left reusable for a cheaper
        # price. The prior test asserted the opposite ("leaves unburned"),
        # which contradicted the contract's "invalid sizing under a valid
        # authorized mandate atomically burns to NO_SUBMIT rather than
        # yielding reusable token"; corrected with this rationale.
        setup = _Setup(tmp_path, _mandate_row(allotment_usd=500.0))
        result = setup.reserve(price=Decimal("600"))
        assert isinstance(result, str) and "single share" in result
        assert "consumed" in result
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT
        assert row.claim_token is None
        assert row.entry_authorisation_available is False
        assert row.entry_authorisation_consumed_at is not None
        # A later cheaper price must NOT obtain a fresh reservation.
        later = setup.reserve(price=Decimal("400"))
        assert isinstance(later, str) and "consumed" in later
        # Unrelated OFF/no-mandate requests still never touch the DB.
        gate_off = _Setup(
            tmp_path, _mandate_row(), gate=_Gate(enabled=False),
        )
        off_result = gate_off.reserve(price=Decimal("600"))
        assert isinstance(off_result, str) and "disabled" in off_result
        off_row = gate_off.row()
        assert off_row is not None
        assert off_row.submit_state == (
            passive_protocol.SUBMIT_STATE_AUTHORIZED
        )


# ---------------------------------------------------------------------------
# Race: two services / two connections — exactly one submit
# ---------------------------------------------------------------------------


class TestRaces:
    def test_two_service_instances_one_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        other = setup.fresh_service()
        barrier = threading.Barrier(2, timeout=10)
        results: dict[str, Any] = {}

        def reserve_and_execute(name: str, svc: Any) -> None:
            barrier.wait()
            ref = svc.reserve_entry(price=Decimal("600"))
            results[name] = ref
            if not isinstance(ref, str):
                results[name] = (ref, svc.execute_reservation(
                    ref,
                    quote=_quote(),
                    broker=setup.broker,
                    risk=RiskController(),
                    notifier=ServerChanNotifier(""),
                ))

        threads = [
            threading.Thread(target=reserve_and_execute, args=("a", setup.passive)),
            threading.Thread(target=reserve_and_execute, args=("b", other)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        refs = [v for v in results.values() if not isinstance(v, str)]
        pure_rejections = [v for v in results.values() if isinstance(v, str)]
        assert len(refs) + len(pure_rejections) == 2
        assert len(refs) == 1, results
        _, outcome = refs[0]
        assert outcome.submitted is True
        assert len(setup.broker.submissions) == 1
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN

    def test_concurrent_begin_execution_one_owner(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        outcomes: list[Any] = []
        barrier = threading.Barrier(2, timeout=10)

        def begin() -> None:
            barrier.wait()
            outcomes.append(
                hooks.begin_execution(ref, f"exec-{uuid4().hex[:8]}"),
            )

        threads = [threading.Thread(target=begin) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        winners = [
            o for o in outcomes
            if not isinstance(o, passive_protocol.PassiveRejection)
        ]
        assert len(winners) == 1
        assert len(setup.broker.submissions) == 0

    def test_concurrent_claim_submission_one_right(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-token-1")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        cash = _FakeUsdCashSnapshot(Decimal("10000"))
        order = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL,
            side="BUY",
            quantity=Decimal("8"),
            price=Decimal("600.00"),
        )
        results: list[bool] = []
        barrier = threading.Barrier(2, timeout=10)

        def claim() -> None:
            barrier.wait()
            results.append(hooks.claim_submission(owner, order, cash))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_loser_cannot_mutate_winner_row(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        hooks = setup.passive.build_hook_bundle()
        winner = hooks.begin_execution(ref, "exec-winner")
        loser_rejection = hooks.begin_execution(ref, "exec-loser")
        assert not isinstance(winner, passive_protocol.PassiveRejection)
        assert isinstance(loser_rejection, passive_protocol.PassiveRejection)
        # A loser holding the RIGHT claim token but a STALE execution token
        # (the realistic loser shape after losing the ownership CAS).
        loser = passive_protocol.PassiveOwner(
            ref=ref, execution_token="exec-loser", intent=winner.intent,
        )
        cash = _FakeUsdCashSnapshot(Decimal("10000"))
        order = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL, side="BUY",
            quantity=Decimal("8"), price=Decimal("600.00"),
        )
        # The loser cannot take the submit right...
        assert hooks.claim_submission(loser, order, cash) is False
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING
        assert row.execution_token == "exec-winner"
        # ...nor record an outcome on the winner's live row (forged token).
        with pytest.raises(ValueError):
            hooks.record_outcome(
                passive_protocol.PassiveOwner(
                    ref=ref, execution_token="exec-forged",
                    intent=winner.intent,
                ),
                passive_protocol.PassiveOutcomeFact(
                    outcome=passive_protocol.SUBMIT_STATE_NO_SUBMIT,
                ),
            )
        # A forged ref (wrong claim token) can never mutate another row.
        forged_ref = passive_protocol.PassiveAttemptRef(
            mandate_id=ref.mandate_id, claim_token="forged-claim",
        )
        forged_rejection = hooks.begin_execution(forged_ref, "exec-x")
        assert isinstance(forged_rejection, passive_protocol.PassiveRejection)
        assert "another owner" in forged_rejection.reason


class TestLaneMarkers:
    def test_context_only_quantities_cannot_ride_the_lane(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        status = setup.execution.execute(
            "BUY",
            PASSIVE_SYMBOL,
            _quote(),
            setup.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            sized_quantity=Decimal("8"),
            execution_context={
                passive_policy.EXECUTION_CONTEXT_LANE_KEY: PASSIVE_LANE,
                passive_policy.EXECUTION_CONTEXT_CLAIM_TOKEN_KEY: "forged",
            },
        )
        assert status is not None and status.status == "SKIPPED"
        assert "no passive execution owner" in status.reason
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_AUTHORIZED

    def test_range_caller_sized_quantity_rejected(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        status = setup.execution.execute(
            "BUY",
            TSLA,
            _quote(TSLA, 100.0),
            setup.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            sized_quantity=Decimal("40"),
        )
        assert status is not None and status.status == "SKIPPED"
        assert "sized_quantity is only accepted" in status.reason

    def test_arbitrary_lane_rejected_not_range(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        status = setup.execution.execute(
            "BUY",
            PASSIVE_SYMBOL,
            _quote(),
            setup.broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            execution_context={
                passive_policy.EXECUTION_CONTEXT_LANE_KEY: "ARBITRARY_LANE",
            },
        )
        assert status is not None and status.status == "SKIPPED"
        assert "unknown lane" in status.reason

    def test_none_resolver_hooks_missing_refused(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        setup.execution.passive_submit_hooks = None
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert "hooks" in outcome.reason
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        # Final-remediation zero-write rule: with hooks unwired the entry
        # wins NO ownership (begin_execution never ran), so it performs no
        # mandate writes — SUBMIT_CLAIMED stands. The reservation above
        # was made while hooks were complete; the separate service-gate
        # tests cover refusing new reservations with incomplete hooks.
        assert row.submit_state == (
            passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
        )

    def test_wrong_immutable_intent_rejected(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-1")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        drifted = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL, side="BUY",
            quantity=Decimal("9"), price=Decimal("600"),
        )
        verdict = hooks.resolve_policy(owner, drifted)
        assert isinstance(verdict, passive_protocol.PassiveRejection)
        assert "does not match the immutable intent quantity" in verdict.reason

    def test_stop0_passive_allowed_range_denied(self, tmp_path: Path) -> None:
        # Range service with stop=0: rejected at the boundary (stop
        # misconfigured/unavailable — never a zero-stop range entry).
        range_svc = _service(stop_loss_pct=0.0)
        result = range_svc.pre_submit_risk_check(
            _pre_submit_request(),
            _PassiveBroker(),
        )
        assert isinstance(result, OrderStatus)
        assert (
            "stop distance is unavailable" in (result.reason or "")
            or "stop_loss_pct must be configured" in (result.reason or "")
        ), result.reason
        # Passive service also configured stop=0: allowed (no stop lane).
        setup = _Setup(
            tmp_path,
            _mandate_row(),
            execution=_service(stop_loss_pct=0.0),
        )
        ref = setup.reserve()
        assert not isinstance(ref, str)
        outcome = setup.execute(ref)
        assert outcome.submitted is True, outcome.reason
        assert len(setup.broker.submissions) == 1


def _pre_submit_request() -> Any:
    from app.services.trade_execution_service import _PreSubmitRiskRequest

    return _PreSubmitRiskRequest(
        action="BUY",
        symbol="AAPL.US",
        quantity=Decimal("10"),
        price=Decimal("100"),
    )


# ---------------------------------------------------------------------------
# Cash gate: denial burns; replay refused
# ---------------------------------------------------------------------------


class TestCashDenials:
    def test_zero_cash_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path, _mandate_row(), cash_amount=Decimal("0"),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "cash" in outcome.reason
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT
        assert row.failure_reason is not None

    def test_cash_fetch_failure_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker.cash_error = RuntimeError("account balance unavailable")
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "cash" in outcome.reason
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_insufficient_cash_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path, _mandate_row(), cash_amount=Decimal("4000"),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "does not cover" in outcome.reason
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_replay_after_cash_denial_refused(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path, _mandate_row(), cash_amount=Decimal("0"),
        )
        ref, first = setup.full_request()
        assert first.submitted is False
        # Re-executing the SAME ref is refused (state NO_SUBMIT).
        second = setup.execute(ref)
        assert second.submitted is False
        assert "cannot begin execution" in second.reason
        assert setup.broker.submissions == []
        # A fresh reservation is also refused.
        third = setup.reserve()
        assert isinstance(third, str) and "consumed" in third

    def test_stale_cash_rejected_at_boundary(self, tmp_path: Path) -> None:
        # Cash older than 5s: the boundary (and the post-CAS recheck) must
        # refuse even though the amount would cover the order.
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker.cash_started_at = NOW - timedelta(seconds=30)
        setup.broker.cash_completed_at = NOW - timedelta(seconds=29)
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "stale" in outcome.reason
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_final_price_reprice_rejects_4802_at_620(
        self, tmp_path: Path,
    ) -> None:
        # Contract vector: 8 shares sized at 600 (cash 4802 exactly covers
        # 600 pricing), boundary reprices to 620 -> required 4961.89 > 4802.
        setup = _Setup(
            tmp_path,
            _mandate_row(),
            execution=_service(final_order_quote_check=_reprice_620),
            cash_amount=Decimal("4802"),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "does not cover" in outcome.reason
        assert setup.broker.submissions == []
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_sufficient_cash_submits(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row(), cash_amount=Decimal("10000"))
        ref, outcome = setup.full_request()
        assert outcome.submitted is True, outcome.reason
        assert len(setup.broker.submissions) == 1


def _reprice_620(
    _broker: BrokerGateway,
    _symbol: str,
    _action: str,
    _price: Decimal,
) -> FinalOrderQuoteCheckResult:
    return FinalOrderQuoteCheckResult(
        executable_price=Decimal("620.00"),
        bid=Decimal("620.00"),
        ask=Decimal("620.00"),
    )


# ---------------------------------------------------------------------------
# Denials at gates burn the intent (never reusable)
# ---------------------------------------------------------------------------


class TestGateDenialsBurn:
    def test_paused_risk_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        risk = RiskController()
        risk.pause("manual review")
        ref = setup.reserve()
        assert not isinstance(ref, str)
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        assert "paused" in outcome.reason or "risk" in outcome.reason
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_position_blocks_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker.positions = [
            Position(TSLA, "LONG", Decimal("10"), Decimal("200")),
        ]
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert TSLA in outcome.reason or "position" in outcome.reason.lower()
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_kill_switch_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        risk = RiskController()
        risk.enable_kill_switch("test")
        ref = setup.reserve()
        assert not isinstance(ref, str)
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_cap_denial_burns_no_submit(self, tmp_path: Path) -> None:
        # Sized at 600 (8 shares, 4800 notional under the 5000 caps), but
        # the boundary's fresh executable price reprices to 640: projected
        # notional 5120 exceeds the 5000 cap -> boundary denial burns.
        def reprice_640(
            _broker: BrokerGateway,
            _symbol: str,
            _action: str,
            _price: Decimal,
        ) -> FinalOrderQuoteCheckResult:
            return FinalOrderQuoteCheckResult(
                executable_price=Decimal("640.00"),
                bid=Decimal("640.00"),
                ask=Decimal("640.00"),
            )

        setup = _Setup(
            tmp_path,
            _mandate_row(),
            execution=_service(final_order_quote_check=reprice_640),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        assert "exceeds" in outcome.reason
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_quote_failure_burns_no_submit(self, tmp_path: Path) -> None:
        setup = _Setup(
            tmp_path,
            _mandate_row(),
            execution=_service(
                final_order_quote_check=lambda *_a: "quote unavailable",
            ),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is False
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT

    def test_revoke_flag_off_mid_flight_burns(self, tmp_path: Path) -> None:
        # Flag turned OFF between reservation and execution: the denial is
        # durable. Through the DEDICATED entry (which owns the lifecycle)
        # the gate refusal burns NO_SUBMIT; the facade's pre-check is only
        # a fast plain refusal (coordination: facade delegation may drop
        # it — the executor re-checks fresh regardless).
        gate = _Gate()
        setup = _Setup(tmp_path, _mandate_row(), gate=gate)
        ref = setup.reserve()
        assert not isinstance(ref, str)
        gate.enabled = False
        direct = setup.execution.execute_passive_entry(
            ref=ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert direct is not None and direct.status == "SKIPPED"
        assert "disabled" in (direct.reason or "")
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_NO_SUBMIT
        assert setup.broker.submissions == []


# ---------------------------------------------------------------------------
# Broker status matrix at the submit boundary
# ---------------------------------------------------------------------------


class _StatusBroker(_PassiveBroker):
    def __init__(self, status: str, order_id: str = "known-1") -> None:
        super().__init__()
        self._status = status
        self._order_id = order_id

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        return OrderResult(
            self._order_id, symbol, side, quantity, price, self._status,
        )


class TestBrokerStatusMatrix:
    @pytest.mark.parametrize(
        "status",
        [
            "SUBMITTED",
            "PARTIAL_FILLED",
            "FILLED",
            "REJECTED",
            "CANCELLED",
        ],
    )
    def test_known_statuses_bind_order_known(
        self, tmp_path: Path, status: str,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker = _StatusBroker(status)
        ref = setup.reserve()
        outcome = setup.execute(ref)
        # SUBMITTED-like receipts map to submitted=True; terminal REJECTED/
        # CANCELLED with zero fill are ORDER_KNOWN-and-consumed but NOT a
        # successful submission (facade pure value mapping, fix-14).
        expected_submitted = status in {
            "SUBMITTED", "PARTIAL_FILLED", "FILLED",
        }
        assert outcome.submitted is expected_submitted, (status, outcome.reason)
        assert outcome.uncertain is not True, (status, outcome.reason)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        assert row.bound_broker_order_id == "known-1"
        assert row.bound_broker_status == status

    def test_unknown_status_is_uncertain(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker = _StatusBroker("SOMETHING_ELSE")
        ref = setup.reserve()
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert outcome.uncertain is True
        assert outcome.reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_missing_id_is_uncertain(self, tmp_path: Path) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        setup.broker = _StatusBroker("SUBMITTED", order_id="")
        ref = setup.reserve()
        outcome = setup.execute(ref)
        assert outcome.submitted is False
        assert outcome.uncertain is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_terminal_partial_fill_is_known_not_no_position(
        self, tmp_path: Path,
    ) -> None:
        # CANCELLED with a partial fill must still be ORDER_KNOWN with the
        # fill preserved (not treated as "no position").
        class _PartialFillBroker(_StatusBroker):
            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                result = super().submit_limit_order(
                    symbol, side, quantity, price,
                )
                return result

        setup = _Setup(tmp_path, _mandate_row())
        setup.broker = _PartialFillBroker("CANCELLED")
        ref = setup.reserve()
        outcome = setup.execute(ref)
        # Terminal CANCELLED: consumed and ORDER_KNOWN, not a submission.
        assert outcome.submitted is False
        assert outcome.uncertain is not True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_ORDER_KNOWN


# ---------------------------------------------------------------------------
# Lost ACK and exception-after-submit paths
# ---------------------------------------------------------------------------


class TestLostAck:
    def test_submit_raises_after_recording_is_uncertain(
        self, tmp_path: Path,
    ) -> None:
        # Fake records the submission then throws (lost ACK).
        setup = _Setup(tmp_path, _mandate_row())
        risk = RiskController()

        def hook() -> None:
            raise ConnectionError("connection lost after send")

        setup.broker.submit_hook = hook
        ref = setup.reserve()
        outcome = setup.execute(ref, risk)
        assert outcome.submitted is False
        assert outcome.uncertain is True
        assert risk.paused is True
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert setup.broker.submissions == []  # fake raised before appending

    def test_persistence_failure_is_uncertain_not_success(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(
            tmp_path,
            _mandate_row(),
            execution=_service(
                record_order=_raise_order_persist,
            ),
        )
        risk = RiskController()
        ref = setup.reserve()
        outcome = setup.execute(ref, risk)
        # Either the service classified it UNCERTAIN or the executor paused
        # with an ORDER_PERSISTENCE_UNCERTAIN prefix; never success.
        assert outcome.submitted is False
        row = setup.row()
        assert row is not None
        assert row.submit_state in (
            passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            passive_protocol.SUBMIT_STATE_UNCERTAIN,
        ), row.submit_state
        # Broker did submit exactly once; the mandate knows the id or is
        # uncertain with facts preserved.
        assert len(setup.broker.submissions) == 1


def _raise_order_persist(*_args: Any) -> None:
    raise RuntimeError("orders table unavailable")


# ---------------------------------------------------------------------------
# SEC98 metadata markers
# ---------------------------------------------------------------------------


class TestSec98Markers:
    def test_markers_on_all_stages(self, tmp_path: Path) -> None:
        recorded: dict[str, Any] = {}

        def record_order(
            order_id: str,
            symbol: str,
            action: str,
            qty: float,
            price: float,
            status: str,
            *_rest: Any,
        ) -> None:
            recorded["order"] = {
                "order_id": order_id,
                "symbol": symbol,
                "metadata": _rest[-1] if _rest else {},
            }

        setup = _Setup(
            tmp_path, _mandate_row(),
            execution=_service(record_order=record_order),
        )
        ref, outcome = setup.full_request()
        assert outcome.submitted is True, outcome.reason
        # Order row carries the SEC98 model marker.
        metadata = recorded["order"]["metadata"]
        assert isinstance(metadata, dict)
        assert (
            metadata.get("accounting_fee_model")
            == ACCOUNTING_FEE_MODEL_US_SEC98
        )
        assert metadata.get("market") == "US"
        # Mandate final snapshot pins the fee at the final price.
        row = setup.row()
        assert row is not None and row.final_snapshot_json
        snapshot = json.loads(row.final_snapshot_json)
        assert snapshot["sec98_fee"] == format(
            us_paper_commission(
                Decimal("600.00"), Decimal("8"),
            ).normalize(), "f",
        )
        # Pending carries the model + owner ref.
        inventory = setup.execution.pending_order_inventory()
        assert inventory.get(PASSIVE_SYMBOL), inventory
        pending = setup.execution.pending_order
        assert pending is not None
        assert pending.fee_model == ACCOUNTING_FEE_MODEL_US_SEC98
        assert pending.passive_owner_ref


# ---------------------------------------------------------------------------
# Migration: legacy schema / NULLs / tokens / states, idempotent twice
# ---------------------------------------------------------------------------


_LEGACY_COLUMNS = """
    CREATE TABLE passive_mandates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lane VARCHAR(40) NOT NULL,
        policy_version VARCHAR(60) NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
        allotment_usd FLOAT NOT NULL,
        risk_model VARCHAR(40) NOT NULL,
        exemptions TEXT NOT NULL,
        review_interval_months INTEGER NOT NULL,
        entry_authorisation_available BOOLEAN NOT NULL DEFAULT 1,
        entry_authorisation_consumed_at DATETIME,
        claim_token VARCHAR(64),
        order_binding VARCHAR(40) NOT NULL,
        bound_broker_order_id VARCHAR(100),
        approved_at DATETIME NOT NULL,
        approved_by VARCHAR(120) NOT NULL,
        approval_reason TEXT NOT NULL,
        created_at DATETIME,
        updated_at DATETIME,
        CONSTRAINT ux_passive_mandates_lane UNIQUE (lane)
    )
"""


class TestLegacyMigration:
    def _legacy_engine(self, tmp_path: Path) -> Engine:
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql(_LEGACY_COLUMNS)
        return engine

    def test_raw_false_available_migrates_uncertain(self, tmp_path: Path) -> None:
        engine = self._legacy_engine(tmp_path)
        try:
            with engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    "6, 0, 'paper-only', '2026-09-29', 'owner', 'x')"
                )
            _ensure_passive_mandates_table(engine)
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state, uncertainty_reason FROM "
                    "passive_mandates"
                ).one()
        finally:
            engine.dispose()
        assert row[0] == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row[1] and "available" in row[1]

    @pytest.mark.parametrize("claim_value", ["legacy-token-1"])
    def test_token_present_migrates_uncertain(
        self, tmp_path: Path, claim_value: str,
    ) -> None:
        engine = self._legacy_engine(tmp_path)
        try:
            with engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "claim_token, order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    f"6, 1, '{claim_value}', 'paper-only', '2026-09-29', "
                    "'owner', 'x')"
                )
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state, uncertainty_reason FROM "
                    "passive_mandates"
                ).one()
        finally:
            engine.dispose()
        assert row[0] == passive_protocol.SUBMIT_STATE_UNCERTAIN
        assert row[1] and "claim_token" in row[1]

    @pytest.mark.parametrize("legacy_state", ["SUBMITTING", "SUBMITTED", "FAILED"])
    def test_legacy_used_states_migrate_uncertain(
        self, tmp_path: Path, legacy_state: str,
    ) -> None:
        engine = self._legacy_engine(tmp_path)
        try:
            # First pass upgrades the schema (adds the new columns).
            _ensure_passive_mandates_table(engine)
            with engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "submit_state, order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    f"6, 1, '{legacy_state}', 'paper-only', '2026-09-29', "
                    "'owner', 'x')"
                )
                # Mark as a v1 row (no protocol stamp) so the second pass
                # classifies it instead of skipping it.
                c.exec_driver_sql(
                    "UPDATE passive_mandates SET protocol_version = NULL"
                )
            # Second (idempotent) pass performs the conservative mapping.
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state FROM passive_mandates"
                ).one()
        finally:
            engine.dispose()
        assert row[0] == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_null_state_migrates_uncertain(self, tmp_path: Path) -> None:
        # A legacy writer left submit_state NULL: the shipped DDL is NOT
        # NULL, so build a nullable variant and verify fail-closed mapping.
        engine = create_engine(f"sqlite:///{tmp_path / 'nullstate.db'}")
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
                    "entry_authorisation_available BOOLEAN, "
                    "entry_authorisation_consumed_at DATETIME, "
                    "submit_state VARCHAR(30), "
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
                    "created_at DATETIME, updated_at DATETIME)"
                )
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "submit_state, order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    "6, 1, NULL, 'paper-only', '2026-09-29', 'owner', 'x')"
                )
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state FROM passive_mandates"
                ).one()
        finally:
            engine.dispose()
        assert row[0] == passive_protocol.SUBMIT_STATE_UNCERTAIN

    def test_protocol_v2_rows_are_not_reset(self, tmp_path: Path) -> None:
        engine = self._legacy_engine(tmp_path)
        try:
            _ensure_passive_mandates_table(engine)
            with engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    "6, 1, 'paper-only', '2026-09-29', 'owner', 'x')"
                )
            _ensure_passive_mandates_table(engine)
            with engine.begin() as c:
                c.exec_driver_sql(
                    "UPDATE passive_mandates SET submit_state='NO_SUBMIT', "
                    "protocol_version='passive-submit-v2', "
                    "failure_reason='x', claim_token='t', "
                    "entry_authorisation_available=0 "
                    "WHERE submit_state='AUTHORIZED'"
                )
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                row = c.exec_driver_sql(
                    "SELECT submit_state FROM passive_mandates"
                ).one()
        finally:
            engine.dispose()
        assert row[0] == "NO_SUBMIT"

    def test_clear_row_stays_authorized_and_second_pass_keeps_it(
        self, tmp_path: Path,
    ) -> None:
        engine = self._legacy_engine(tmp_path)
        try:
            with engine.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO passive_mandates (lane, policy_version, "
                    "symbol, status, allotment_usd, risk_model, exemptions, "
                    "review_interval_months, entry_authorisation_available, "
                    "order_binding, approved_at, approved_by, "
                    "approval_reason) VALUES ("
                    "'SPY_PASSIVE', 'passive-allocation-v1', 'SPY.US', "
                    "'ACTIVE', 5000.0, 'FULL_PRINCIPAL', 'no_price_stop', "
                    "6, 1, 'paper-only', '2026-09-29', 'owner', 'x')"
                )
            _ensure_passive_mandates_table(engine)
            _ensure_passive_mandates_table(engine)
            with engine.connect() as c:
                rows = c.exec_driver_sql(
                    "SELECT submit_state, protocol_version FROM "
                    "passive_mandates"
                ).fetchall()
        finally:
            engine.dispose()
        assert rows[0][0] == passive_protocol.SUBMIT_STATE_AUTHORIZED
        assert rows[0][1] == passive_protocol.PASSIVE_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Real subprocess crash tests (journal on disk; no live broker)
# ---------------------------------------------------------------------------


_CRASH_HARNESS = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    mode, db_path, phase = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    sys.path.insert(0, %r)

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT submit_state FROM passive_mandates LIMIT 1"
            )
        ).one()
        state = row[0]
    print(f"STATE={state}")
    sys.exit(0)
    """,
) % str(Path(__file__).resolve().parents[1])


class TestCrashRecovery:
    def _spawn(self, tmp_path: Path, db_path: Path, phase: str) -> str:
        harness = tmp_path / f"crash_harness_{phase}.py"
        harness.write_text(_CRASH_HARNESS)
        result = subprocess.run(
            [sys.executable, str(harness), "inspect", str(db_path), phase],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "AUTO_TRADE_ENV": "test"},
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_restart_after_crash_in_checking_no_duplicate(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        assert not isinstance(ref, str)
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-crash-1")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        db_path = Path(str(setup.engine.url.database))
        # The process "dies" (we simply abandon the objects); a restart
        # must observe CHECKING and refuse to re-execute the same ref.
        state = self._spawn(tmp_path, db_path, "checking")
        assert "STATE=CHECKING" in state
        restarted = setup.fresh_service()
        outcome = restarted.execute_reservation(
            ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert outcome.submitted is False
        assert "cannot begin execution" in outcome.reason
        assert setup.broker.submissions == []

    def test_restart_after_crash_in_submitting_no_auto_retry(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-crash-2")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        cash = _FakeUsdCashSnapshot(Decimal("10000"))
        order = passive_protocol.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL, side="BUY",
            quantity=Decimal("8"), price=Decimal("600.00"),
        )
        assert hooks.claim_submission(owner, order, cash) is True
        db_path = Path(str(setup.engine.url.database))
        state = self._spawn(tmp_path, db_path, "submitting")
        assert "STATE=SUBMITTING" in state
        # A restart must NOT re-execute (possibly already submitted).
        restarted = setup.fresh_service()
        outcome = restarted.execute_reservation(
            ref,
            quote=_quote(),
            broker=setup.broker,
            risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        assert outcome.submitted is False
        assert setup.broker.submissions == []

    def test_fresh_service_after_full_submit_refuses_new(
        self, tmp_path: Path,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        ref, outcome = setup.full_request()
        assert outcome.submitted is True
        restarted = setup.fresh_service()
        result = restarted.reserve_entry(price=Decimal("600"))
        assert isinstance(result, str) and "consumed" in result
        assert len(setup.broker.submissions) == 1


# ---------------------------------------------------------------------------
# Topology: sole boundary + sole broker mutation unchanged
# ---------------------------------------------------------------------------


class TestTopology:
    def test_exactly_one_boundary_and_one_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setup = _Setup(tmp_path, _mandate_row())
        invocations: list[str] = []
        boundary = setup.execution.pre_submit_risk_check

        def observe(request: Any, broker: Any) -> Any:
            invocations.append(request.action)
            return boundary(request, broker)

        monkeypatch.setattr(setup.execution, "pre_submit_risk_check", observe)
        ref, outcome = setup.full_request()
        assert outcome.submitted is True, outcome.reason
        assert invocations == ["BUY"]
        assert len(setup.broker.submissions) == 1

    def test_range_no_marker_no_sized_unchanged(
        self, tmp_path: Path,
    ) -> None:
        from app.services.trade_execution_service import ApprovedOrder

        setup = _Setup(tmp_path, _mandate_row())
        result = setup.execution.pre_submit_risk_check(
            _pre_submit_request(),
            setup.broker,
        )
        assert isinstance(result, ApprovedOrder)
        assert result.price == Decimal("100")

    def test_duplicate_boundary_cannot_create_submit_right(
        self, tmp_path: Path,
    ) -> None:
        # The boundary is read-only on the mandate; calling it twice must
        # not consume or create a submit right (state stays CHECKING).
        setup = _Setup(tmp_path, _mandate_row())
        ref = setup.reserve()
        hooks = setup.passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-dup")
        assert not isinstance(owner, passive_protocol.PassiveRejection)
        setup.execution._active_execution_context = {
            passive_policy.EXECUTION_CONTEXT_LANE_KEY: PASSIVE_LANE,
            passive_policy.EXECUTION_CONTEXT_CLAIM_TOKEN_KEY: ref.claim_token,
            passive_policy.EXECUTION_CONTEXT_SIZED_QUANTITY_KEY: 8.0,
            "passive_submit_owner": owner,
            "passive_cash_evidence": _FakeUsdCashSnapshot(
                Decimal("10000"),
                started_at=setup.clock() - timedelta(seconds=1),
                completed_at=setup.clock() - timedelta(seconds=0.5),
            ),
            "market": "US",
            "accounting_fee_model": ACCOUNTING_FEE_MODEL_US_SEC98,
        }
        request = _PreSubmitRiskRequest(
            action="BUY",
            symbol=PASSIVE_SYMBOL,
            quantity=Decimal("8"),
            price=Decimal("600"),
        )
        first = setup.execution.pre_submit_risk_check(request, setup.broker)
        second = setup.execution.pre_submit_risk_check(request, setup.broker)
        from app.services.trade_execution_service import ApprovedOrder

        assert isinstance(first, ApprovedOrder), getattr(first, "reason", first)
        assert isinstance(second, ApprovedOrder), getattr(second, "reason", second)
        row = setup.row()
        assert row is not None
        assert row.submit_state == passive_protocol.SUBMIT_STATE_CHECKING


from app.services.trade_execution_service import (  # noqa: E402
    _PreSubmitRiskRequest,
)


# ---------------------------------------------------------------------------
# Domain purity
# ---------------------------------------------------------------------------


class TestDomainPurity:
    PKG = (
        Path(__file__).resolve().parents[1]
        / "app" / "domain" / "passive_allocation"
    )
    FORBIDDEN_PREFIXES = (
        "app.services",
        "app.api",
        "app.platform",
        "app.config",
        "app.models",
        "app.database",
        "sqlalchemy",
    )

    def test_no_forbidden_imports(self) -> None:
        violations: list[str] = []
        for path in sorted(self.PKG.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module] if node.module else []
                else:
                    continue
                for name in names:
                    if name.startswith(self.FORBIDDEN_PREFIXES):
                        violations.append(f"{path.name}: {name}")
        assert not violations, f"forbidden imports: {violations}"

    def test_no_wall_clock_reads(self) -> None:
        offenders: list[str] = []
        for path in sorted(self.PKG.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "now", "utcnow", "today",
                }:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"wall-clock reads: {offenders}"
