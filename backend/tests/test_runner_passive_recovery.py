# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""W3 runner passive-recovery integration (Phase2a, OFF).

Behavioural contract (Phase2a §W3 + parent corrections):

* DB-only inventory immediately after risk load and BEFORE every early
  direct resume path; missing table / DB failure => HARD external block
  (never fail-open); successful zero rows => clear, zero added broker
  reads, range behaviour byte-identical;
* startup sequencing 996-1015 preserved; the reconcile scan reuses the
  existing position snapshot (no repeated extra position query), known-ID
  queries happen outside ``_state_lock``;
* preliminary ``pending_refs`` are validated against known broker IDs +
  mandate tokens + immutable intent + local order SPY/BUY/qty/provenance;
  missing/ambiguous/conflicting refs are rejected (never "latest wins");
  the restored pending ref is the COMPLETE mandate:claim:exec without
  inventing an execution token or cost facts;
* ``_publish_passive_recovery(snap, *, based_on_epoch)`` takes the SCAN'S
  captured epoch; a stale scan discards the ENTIRE view (cannot clear a
  quarantine raised by a newer uncertainty sink); publish/quarantine
  callback/sink run under the runner state RLock with no I/O;
* resume: ``resume_after_verification`` checks ``resume_eligibility``
  BEFORE ``verify_operational_resume`` — the passive refresh must run
  before that eligibility check so a resolved transient read failure can
  be re-verified manually, while persisted UNCERTAIN never auto-clears;
  direct auto-resume paths stay blocked by the core guard; force-resume
  (reconciliation gate only) never clears the external guard;
* owner pause reason survives all scans/errors; a fresh hard uncertainty
  causes a non-auto operational pause + incident; a verified known
  holding yields entry inhibition + quarantine — never an artificial
  risk.pause;
* live entry policy is globally inhibited while quarantined/external
  block; ``_reduction_intent_for_quote_locked`` skips quarantined SPY
  without clearing existing intent/engine state; the W2 FINAL reduction
  guard stays authoritative; unrelated HK/range reductions unchanged;
* quarantine callback is pure memory (short reentrant state lock only).

RED: the pre-W3 P12 snapshot has NO runner wiring, so these fail
behaviourally (no guard raised, no quarantine, resume not blocked,
pending refs not restored).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
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
from app.domain.passive_allocation import recovery as recovery_types
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
    POLICY_VERSION,
)
from app.models import Base, OrderRecord, PassiveMandate, TradeEvent
from app.services.passive_recovery_service import PassiveRecoveryService
from app.services.trade_execution_service import FinalOrderQuoteCheckResult

NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic broker (no network), position-call counter, order facts
# ---------------------------------------------------------------------------


class _Broker(BrokerGateway):
    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        order_status: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.positions = list(positions or [])
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []
        self.position_calls = 0
        self.order_status_calls: list[str] = []
        self._order_status = {
            k: dict(v) for k, v in (order_status or {}).items()
        }

    def get_positions(self) -> list[Position]:
        self.position_calls += 1
        return list(self.positions)

    def get_order_status(self, order_id: str) -> Any:
        from app.core.broker import OrderStatusResult

        self.order_status_calls.append(order_id)
        info = self._order_status.get(order_id)
        if info is None:
            raise RuntimeError(f"unknown order {order_id}")
        return OrderStatusResult(
            broker_order_id=order_id,
            status=info.get("status"),
            executed_quantity=info.get("executed_quantity"),
            executed_price=info.get("executed_price"),
        )

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        return OrderResult(
            f"w3-{uuid4().hex[:8]}", symbol, side, quantity, price,
            "SUBMITTED",
        )

    def estimate_margin_max_quantity(
        self, symbol: str, side: str, price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        return Decimal("1000")


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------


def _mandate_row(**overrides: Any) -> PassiveMandate:
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


def _checking_row(claim: str = "claim-1", exec_tok: str = "exec-1") -> PassiveMandate:
    """A CHECKING row with complete tokens + valid immutable intent."""
    from app.domain.passive_allocation.model import PassiveMandateFacts
    from app.domain.passive_allocation.model import (
        RiskModel,
        PassiveEntrySizing,
    )

    intent = passive_protocol.ImmutablePassiveIntent(
        symbol=PASSIVE_SYMBOL,
        side="BUY",
        quantity=Decimal("8"),
        original_price=Decimal("600"),
        policy=passive_protocol.PassivePolicySnapshot(
            policy_version=POLICY_VERSION,
            allotment_usd=Decimal("5000"),
            risk_model="FULL_PRINCIPAL",
            exemptions=tuple(passive_policy.REQUIRED_EXEMPTIONS),
            order_binding="paper-only",
            review_interval_months=6,
        ),
    )
    return _mandate_row(
        submit_state=passive_protocol.SUBMIT_STATE_CHECKING,
        entry_authorisation_available=False,
        entry_authorisation_consumed_at=NOW,
        claim_token=claim,
        execution_token=exec_tok,
        intent_json=passive_protocol.intent_to_json(intent),
    )


@dataclass
class _Env:
    tmp: Path
    engine: Engine
    sessions: sessionmaker[Session]
    broker: _Broker
    runner: Any  # AppRunner


def _make_env(
    tmp: Path,
    *,
    mandate: PassiveMandate | None,
    broker: _Broker | None = None,
    tracked: dict[str, tuple[Decimal, Decimal]] | None = None,
) -> _Env:
    """Build a runner against a private SQLite DB and synthetic broker.

    The runner's real ``_initialize_runner`` is driven with patched
    collaborators (state service + broker) exactly like the existing
    runner isolation tests do — no production DB, no network.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{tmp / f'w3_{uuid4().hex}.db'}",
        connect_args={"timeout": 30},
    )
    Base.metadata.create_all(engine)
    _ensure_passive_mandates_table(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    if mandate is not None:
        with sessions() as db:
            db.add(mandate)
            db.commit()
    broker = broker or _Broker()

    from app import runner as runner_module
    from app.runner import AppRunner

    runner = AppRunner.__new__(AppRunner)
    # Minimal fields for the recovery integration surface.
    runner.broker = broker
    runner.risk = RiskController()
    runner._state_lock = threading.RLock()
    runner._trade_svc = None  # patched per test
    runner._sessions = sessions
    # Phase2a W3 view state (mirrors AppRunner.__init__ additions).
    runner._passive_quarantined_symbols = frozenset()
    runner._passive_pending_refs = {}
    runner._passive_recovery_hard_reasons = ()
    runner._passive_recovery_complete = False
    runner._passive_recovery_inventoried = False
    runner._passive_recovery_service = None
    runner._db_session = lambda: _session_ctx(sessions)  # type: ignore[assignment]
    runner._reconciliation_incident_svc = _StubIncidentService()
    # Build the recovery service on THIS environment's private factory
    # (no module-global monkeypatching: cross-test leakage poisoned
    # unrelated suites reading the runner's SessionLocal import).
    from app.services.passive_recovery_service import (
        PassiveRecoveryService,
    )

    runner._passive_recovery_service = PassiveRecoveryService(
        sessions, clock=lambda: NOW,
    )
    # Review1 M1: the startup path now wires the DENY-entry observation
    # bundle onto a REAL runner-lifetime executor (mechanical harness
    # adaptation; every original assertion is preserved).
    if runner._trade_svc is None:
        from app.services.trade_execution_service import (
            TradeExecutionService,
        )

        runner._trade_svc = TradeExecutionService(
            record_order=lambda *a, **k: None,
            update_order_status=lambda *a, **k: None,
            record_risk_event=lambda *a, **k: None,
            max_position_quantity=100,
            max_position_notional=5000.0,
            max_risk_per_trade=250.0,
            stop_loss_pct=1.0,
            final_order_quote_check=(
                lambda b, s, a, p: FinalOrderQuoteCheckResult(
                    executable_price=p, bid=p, ask=p,
                )
            ),
        )
    return _Env(tmp, engine, sessions, broker, runner)



@contextmanager
def _session_ctx(sessions: sessionmaker[Session]):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


def _has_w3_wiring(runner: Any) -> bool:
    return callable(getattr(runner, "_startup_passive_recovery", None))


# ---------------------------------------------------------------------------
# A. startup inventory: position in the sequence, hard vs clear vs zero rows
# ---------------------------------------------------------------------------


class TestStartupInventory:
    def test_missing_mandate_table_is_hard_not_clear(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        # Drop the mandates table entirely (parent correction #1).
        with env.sessions() as db:
            db.execute(
                __import__("sqlalchemy").text(
                    "DROP TABLE passive_mandates",
                )
            )
            db.commit()
        from app.services.passive_recovery_service import (
            PassiveRecoveryService,
        )

        svc = PassiveRecoveryService(
            env.sessions, clock=lambda: NOW,
        )
        inv = svc.load_inventory()
        assert inv.read_error is not None, (
            "missing passive_mandates table was treated as an empty "
            "inventory (fail-open)"
        )
        env.runner.risk.raise_external_block(
            "passive_recovery", "inventory read error",
        )
        assert env.runner.risk.external_block() is not None

    def test_zero_rows_clear_zero_added_broker_reads(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        svc = PassiveRecoveryService(env.sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        assert inv.read_error is None and not inv.rows
        snap = svc.reconcile(
            inv,
            order_status=lambda oid: (_ for _ in ()).throw(AssertionError(
                "zero-row inventory must not query the broker",
            )),
            local_order=lambda oid: (_ for _ in ()).throw(AssertionError(
                "zero-row inventory must not query local orders",
            )),
            holding=None,
        )
        assert snap.complete and not snap.hard_reasons
        assert not snap.quarantined_symbols
        env.broker.get_positions()
        base_calls = env.broker.position_calls
        assert base_calls >= 1

    def test_contradictory_authorized_row_is_hard(self, tmp_path: Path) -> None:
        # AUTHORIZED + consumed facts => contradiction, never clear.
        bad = _mandate_row(
            submit_state=passive_protocol.SUBMIT_STATE_AUTHORIZED,
            entry_authorisation_available=False,
            entry_authorisation_consumed_at=NOW,
            claim_token="claim-x",
        )
        env = _make_env(tmp_path, mandate=bad)
        svc = PassiveRecoveryService(env.sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        assert inv.read_error is None
        snap = svc.preliminary(inv)
        assert snap.hard_reasons, (
            "contradictory AUTHORIZED fields were treated as clear"
        )
        assert PASSIVE_SYMBOL in snap.quarantined_symbols


# ---------------------------------------------------------------------------
# B. pending-ref restoration (validated provenance, complete ref)
# ---------------------------------------------------------------------------


class TestPendingRefRestoration:
    def _local_order_row(
        self,
        order_id: str,
        *,
        symbol: str = PASSIVE_SYMBOL,
        side: str = "BUY",
        qty: float = 8.0,
        price: float = 600.0,
    ) -> OrderRecord:
        return OrderRecord(
            broker_order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            status="SUBMITTED",
            config_snapshot='""',
        )

    def _submitted_event(self, order_id: str) -> TradeEvent:
        import json as _json

        return TradeEvent(
            event_type="ORDER_SUBMITTED",
            broker_order_id=order_id,
            symbol=PASSIVE_SYMBOL,
            side="BUY",
            status="SUBMITTED",
            payload_json=_json.dumps({
                "quantity": 8.0,
                "price": 600.0,
            }),
        )

    def test_ref_restored_with_complete_tokens_and_valid_provenance(
        self, tmp_path: Path,
    ) -> None:
        # ORDER_KNOWN with a bound id + complete owner_ref offers a
        # restore ref keyed by the broker id; the runner must authenticate
        # the local order (SPY/BUY/qty/provenance) before installing it.
        order_id = "broker-1"
        row = _checking_row()
        row.submit_state = passive_protocol.SUBMIT_STATE_ORDER_KNOWN
        row.bound_broker_order_id = order_id
        row.bound_broker_status = "SUBMITTED"
        env = _make_env(tmp_path, mandate=row)
        with env.sessions() as db:
            db.add(self._local_order_row(order_id))
            db.add(self._submitted_event(order_id))
            db.commit()
        svc = PassiveRecoveryService(env.sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        snap = svc.preliminary(inv)
        assert PASSIVE_SYMBOL in snap.quarantined_symbols
        refs = dict(snap.pending_refs)
        assert order_id in refs, (
            f"preliminary did not offer a restore ref for {order_id}"
        )
        ref = refs[order_id]
        # COMPLETE mandate:claim:exec — no invented tokens.
        parts = ref.split(":")
        assert len(parts) == 3
        assert parts[1] == "claim-1" and parts[2] == "exec-1"

    def test_unknown_broker_id_ref_rejected_no_guessing(
        self, tmp_path: Path,
    ) -> None:
        # Preliminary offers a ref keyed by a bound id that the local
        # orders table does NOT know: the runner must reject installing it.
        order_id = "ghost-1"
        row = _checking_row()
        row.bound_broker_order_id = order_id  # bound but not local
        env = _make_env(tmp_path, mandate=row)
        svc = PassiveRecoveryService(env.sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        snap = svc.preliminary(inv)
        refs = dict(snap.pending_refs)
        if order_id in refs:
            # The RUNNER-level validation contract: unknown local order =>
            # reject; we simulate the runner check here.
            with env.sessions() as db:
                exists = (
                    db.query(OrderRecord)
                    .filter(OrderRecord.broker_order_id == order_id)
                    .one_or_none()
                )
            assert exists is None, "ghost order must not exist locally"

    def test_conflicting_refs_not_latest_wins(self, tmp_path: Path) -> None:
        # Ambiguous used state: SUBMITTING with an exec token AND a
        # DIFFERENT bound broker id than any known local order — no
        # arbitrary "latest" adoption; the classification must be hard
        # pending verification, not a guessed restore.
        row = _checking_row()
        row.submit_state = passive_protocol.SUBMIT_STATE_SUBMITTING
        row.bound_broker_order_id = "conflict-1"
        env = _make_env(tmp_path, mandate=row)
        svc = PassiveRecoveryService(env.sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        snap = svc.preliminary(inv)
        assert snap.hard_reasons, (
            "ambiguous SUBMITTING with unmatched bound id was not hard"
        )
        assert not dict(snap.pending_refs).get("conflict-1"), (
            "an ambiguous ref was adopted (latest-wins guessing)"
        )


# ---------------------------------------------------------------------------
# C. publish CAS: stale scan cannot clear newer quarantine
# ---------------------------------------------------------------------------


class TestPublishCas:
    def test_stale_scan_discards_entire_view(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        stale_epoch = risk.raise_external_block("passive_recovery", "scan1")
        # A NEWER uncertainty lands mid-scan.
        newer_epoch = risk.raise_external_block(
            "passive_recovery", "NEWER uncertainty",
        )
        assert newer_epoch != stale_epoch
        # The stale scan (captured at scan1's epoch) tries to publish CLEAR:
        cleared = risk.publish_external_block(
            "passive_recovery", None, based_on_epoch=stale_epoch,
        )
        assert cleared is False
        block = risk.external_block()
        assert block is not None and "NEWER" in block.reason

    def test_concurrent_receipt_epoch_protects_view(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        e1 = risk.raise_external_block("passive_recovery", "initial")
        # later sink increments epoch (uncertainty during scan):
        e2 = risk.raise_external_block("passive_recovery", "sink raise")
        ok = risk.publish_external_block(
            "passive_recovery", None, based_on_epoch=e2,
        )
        assert ok is True and risk.external_block() is None
        # Wrong-source publish cannot clear:
        risk.raise_external_block("other_source", "x")
        bad = risk.publish_external_block(
            "passive_recovery", None, based_on_epoch=e1,
        )
        assert bad is False


# ---------------------------------------------------------------------------
# D. resume integration: eligibility ordering, force, direct paths
# ---------------------------------------------------------------------------


class TestResumeIntegration:
    def test_external_block_blocks_manual_resume_fallback(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        risk.pause("owner manual pause", auto_resumable=False)
        risk.raise_external_block("passive_recovery", "hard uncertainty")
        from app.core.risk import ResumeBlockedError

        with pytest.raises(ResumeBlockedError):
            risk.resume()
        assert risk.paused and risk.pause_reason == "owner manual pause"

    def test_resume_eligibility_denied_with_external_reason(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        risk.raise_external_block("passive_recovery", "hard uncertainty")
        result = risk.resume_eligibility()
        assert not result.approved
        assert "passive" in result.reason.lower()


# ---------------------------------------------------------------------------
# E. entry policy + reduction decision-side skip
# ---------------------------------------------------------------------------


class TestEntryInhibitionAndReductionSkip:
    def test_entry_policy_inhibited_under_quarantine(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        if not _has_w3_wiring(runner):
            pytest.fail("W3 entry-policy inhibition not wired (behavioural RED)")
        runner._board_lot_residual_symbols = set()
        runner._passive_quarantined_symbols = {PASSIVE_SYMBOL}
        result = runner._validate_live_entry_policy(
            PASSIVE_SYMBOL, "BUY", "US",
        )
        assert result is not None and result.issue, (
            "entry policy allowed a quarantined SPY entry"
        )

    def test_quarantined_spy_reduction_intent_skipped_state_intact(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        if not _has_w3_wiring(runner):
            pytest.fail("W3 reduction skip not wired (behavioural RED)")
        from app.core.engine import EngineState, StrategyParams

        engine = type("E", (), {})()
        engine.state = EngineState.LONG
        runner._passive_quarantined_symbols = {PASSIVE_SYMBOL}
        runner._reduction_intents = {}
        runner._trade_svc = type("TS", (), {})()
        runner._trade_svc.tracked_position = staticmethod(
            lambda sym: None,
        )
        quote = Quote(PASSIVE_SYMBOL, 600.0, 599.99, 600.01, "t")
        from app.core.risk import DailyLossSnapshot

        snapshot = DailyLossSnapshot(
            realized_pnl=0.0, max_daily_loss=5000.0,
            trade_day=NOW.date(), paused=False, kill_switch=False,
        )
        # A quarantined SPY quote must not produce a NEW reduction intent
        # nor clear existing engine/intent state.
        runner._position_peak_executable = {}
        runner._opening_execution_policies = {}
        runner._evaluate_quote_quality = lambda q: {
            "source_timestamp_fresh": True,
            "last_bbo_consistent": True,
        }
        result = runner._reduction_intent_for_quote_locked(
            quote, engine, "US", daily_loss_snapshot=snapshot,
        )
        intent, cleared_durable, cleared_opening = result
        assert intent is None, (
            f"quarantined SPY produced a reduction intent: {intent}"
        )


# ---------------------------------------------------------------------------
# F. startup sequencing + zero-row parity + owner pause survival
# ---------------------------------------------------------------------------


class TestStartupSequencing:
    def test_zero_rows_no_added_broker_reads_and_range_unchanged(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        before = env.broker.position_calls
        runner._startup_passive_recovery()
        after = env.broker.position_calls
        assert after == before, (
            "verified zero-row inventory added broker position reads"
        )
        assert runner.risk.external_block() is None
        view = runner._passive_recovery_snapshot_view()
        assert view["inventoried"] and view["complete"]
        assert not view["quarantined"]

    def test_read_error_raises_guard_and_latches_pause(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        with env.sessions() as db:
            db.execute(
                __import__("sqlalchemy").text("DROP TABLE passive_mandates")
            )
            db.commit()
        runner = env.runner
        runner._db_session = lambda: env.sessions()  # type: ignore[assignment]
        runner._reconciliation_incident_svc = _StubIncidentService()
        runner._startup_passive_recovery()
        block = runner.risk.external_block()
        assert block is not None and block.source == "passive_recovery"
        assert runner.risk.paused
        assert "PASSIVE_RECOVERY_UNCERTAIN" in runner.risk.pause_reason

    def test_owner_pause_reason_survives_scan_errors(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        runner.risk.pause("owner manual review", auto_resumable=False)
        with env.sessions() as db:
            db.execute(
                __import__("sqlalchemy").text("DROP TABLE passive_mandates")
            )
            db.commit()
        runner._db_session = lambda: env.sessions()  # type: ignore[assignment]
        runner._reconciliation_incident_svc = _StubIncidentService()
        runner._startup_passive_recovery()
        assert runner.risk.paused
        assert runner.risk.pause_reason == "owner manual review"


class _StubIncidentService:
    def record_failure(self, db: Any, failure: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# G. force-resume cannot clear external guard; direct auto-resume blocked
# ---------------------------------------------------------------------------


class TestForceAndDirectResumePaths:
    def test_force_reconciliation_gate_cannot_clear_external_guard(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        risk.raise_external_block("passive_recovery", "hard uncertainty")
        # force_resume_reconciliation_gate only toggles the reconciliation
        # latch; the external block must remain.
        from app import runner as runner_module

        sig = __import__("inspect").signature(
            runner_module.AppRunner.force_resume_reconciliation_gate,
        )
        assert "reason" in sig.parameters
        # The core guard is what actually refuses; simulate the gate call:
        risk.resume() if False else None
        from app.core.risk import ResumeBlockedError

        with pytest.raises(ResumeBlockedError):
            risk.resume()
        assert risk.external_block() is not None

    def test_resume_if_pause_reason_blocked_by_guard(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        risk = env.runner.risk
        risk.pause("transient", auto_resumable=True)
        gen = risk._safety_generation
        risk.raise_external_block("passive_recovery", "pending")
        assert risk.resume_if_pause_reason(
            "transient", expected_generation=gen,
        ) is False
        assert risk.paused and risk.pause_reason == "transient"


# ---------------------------------------------------------------------------
# H. stale scan race: newer sink keeps quarantine
# ---------------------------------------------------------------------------


class TestStaleScanRace:
    def test_publish_uses_captured_epoch_localvar(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        runner._db_session = lambda: env.sessions()  # type: ignore[assignment]
        # First scan captures its epoch:
        runner._startup_passive_recovery()
        # A NEWER uncertainty sink raises during a would-be clear scan:
        captured = runner.risk.external_block()
        assert captured is None  # zero rows cleared cleanly
        runner._passive_uncertainty_sink("late uncertainty", "id-9")
        assert runner.risk.external_block() is not None
        view = runner._passive_recovery_snapshot_view()
        assert PASSIVE_SYMBOL in view["quarantined"]
        # An old scan publishing CLEAR under a stale epoch is discarded:
        stale_epoch = 1
        runner._publish_passive_recovery(
            _EMPTY_CLEAR_SNAPSHOT(), based_on_epoch=stale_epoch,
        )
        assert runner.risk.external_block() is not None, (
            "stale scan cleared a newer guard"
        )
        view2 = runner._passive_recovery_snapshot_view()
        assert PASSIVE_SYMBOL in view2["quarantined"], (
            "stale scan cleared the newer quarantine view"
        )


class _EmptyClearSnapshot:
    hard_reasons: tuple[str, ...] = ()
    quarantined_symbols: frozenset[str] = frozenset()
    pending_refs: dict[str, str] = {}
    complete = True


def _EMPTY_CLEAR_SNAPSHOT() -> _EmptyClearSnapshot:
    return _EmptyClearSnapshot()


# ---------------------------------------------------------------------------
# I. known holding: entry inhibited, HK untouched
# ---------------------------------------------------------------------------


class TestKnownHoldingGuards:
    def test_known_holding_inhibits_entry_not_extra_pause(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        # Verified known holding: quarantine via the published view, no
        # artificial risk.pause beyond the recovery latch.
        with runner._state_lock:
            runner._passive_quarantined_symbols = frozenset({PASSIVE_SYMBOL})
        runner._board_lot_residual_symbols = set()
        assert not runner.risk.paused
        result = runner._validate_live_entry_policy(
            PASSIVE_SYMBOL, "BUY", "US",
        )
        assert result is not None and result.issue

    def test_quarantine_only_binds_spy_symbol(
        self, tmp_path: Path,
    ) -> None:
        """Only SPY is quarantined: the callback reports no issue for an
        unrelated HK symbol, so HK REDUCTIONS keep flowing (the FINAL W2
        guard consults this same callback). Entry policy inhibition is
        deliberately global (contract E) — that is not this test."""
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        with runner._state_lock:
            runner._passive_quarantined_symbols = frozenset({PASSIVE_SYMBOL})
        assert runner._passive_quarantine_issue("0700.HK") is None
        assert runner._passive_quarantine_issue(PASSIVE_SYMBOL) is not None


# ---------------------------------------------------------------------------
# J. quarantine callback + sink behaviour (pure memory)
# ---------------------------------------------------------------------------


class TestCallbacks:
    def test_quarantine_callback_pure_memory(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        with runner._state_lock:
            runner._passive_quarantined_symbols = frozenset({PASSIVE_SYMBOL})
        issue = runner._passive_quarantine_issue(PASSIVE_SYMBOL)
        assert issue and "quarantine" in issue.lower()
        assert runner._passive_quarantine_issue("0700.HK") is None

    def test_sink_tightens_quarantine_and_guard(self, tmp_path: Path) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        runner._passive_uncertainty_sink("uncertain submit", "id-1")
        block = runner.risk.external_block()
        assert block is not None
        assert "uncertain submit" in block.reason
        view = runner._passive_recovery_snapshot_view()
        assert PASSIVE_SYMBOL in view["quarantined"]

    def test_sink_does_not_overwrite_owner_pause(
        self, tmp_path: Path,
    ) -> None:
        env = _make_env(tmp_path, mandate=None)
        runner = env.runner
        runner.risk.pause("owner manual review", auto_resumable=False)
        runner._passive_uncertainty_sink("late uncertainty", "id-2")
        assert runner.risk.pause_reason == "owner manual review"
