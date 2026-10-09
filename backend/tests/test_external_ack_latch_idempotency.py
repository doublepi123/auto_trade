"""Regression: an identical re-latch must not reset the ack safety generation.

Root cause (oracle53): the 15s today-order sync calls
``_latch_live_order_reconciliation`` with the *same* inventory and issues on
every cycle while the halt persists.  The latch revoked protective exits
unconditionally *before* its same-reason early return, and
``RiskController.revoke_protective_exits`` increments ``_safety_generation``
even when no protective permission was in effect.  The external
acknowledgement key is ``(pause_reason, safety_generation)``
(``_external_ack_local_key``), so the ~30s between two ACK HTTP attempts
always spanned a sync cycle, the generation drifted, and the second proof
could never match: ``PROOF_PENDING`` forever.

These tests run the *real* runner sync (``sync_today_orders_from_broker``),
the real ``RiskController`` and the real latch/ack paths against a fake broker
and fake historical transport (no network, no broker SDK import).

Expected behaviour pinned here:
- Re-latching the byte-identical reason with NO protective permission granted
  is a no-op: the safety generation must not change and a second
  acknowledgement proof must succeed (RED against the unfixed latch).
- The first latch and every *changed* latch still revoke first.
- A permission actually granted is still revoked by an identical re-latch.
- Kill switch / broker identity change / a new live order appearing between
  the two proofs still reject, and the 5s double-proof spacing plus the 300s
  first-proof TTL bounds are unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, TradeEvent
from app.runner import AppRunner
from app.core.broker import BrokerGateway, BrokerOrder
from app.services.external_order_acknowledgement_service import ACK_EVENT
from tests.test_external_order_acknowledgement import (
    BUY_AT,
    IDENTITY,
    SELL_AT,
    SELL_FILL,
    _facts,
    _leg,
    _record,
)


# ---------------------------------------------------------------------------
# Integration fixture: real sync + real latch + real ack on a fake broker.
# ---------------------------------------------------------------------------

@pytest.fixture
def case(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    buy = _leg("1292901539857346560", "BUY", "37.16", BUY_AT, BUY_AT + timedelta(seconds=118))
    sell = _leg("1293025124420681728", "SELL", "37.60", SELL_AT, SELL_FILL)
    runner = AppRunner()
    runner._broker_identity_fingerprint = IDENTITY
    runner._credential_parts_complete = True
    clock = [100.0]
    monkeypatch.setattr("app.runner.time.monotonic", lambda: clock[0])

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 9, 7, tzinfo=timezone.utc) + timedelta(seconds=clock[0] - 100)

    monkeypatch.setattr("app.runner.datetime", _FakeDateTime)

    @contextmanager
    def sessions():
        with Session(engine) as db:
            yield db

    runner._db_session = sessions

    @contextmanager
    def sessions_or(db):
        if db is not None:
            yield db
            return
        with Session(engine) as owned:
            yield owned

    runner._db_session_or = sessions_or

    class _FakeBroker:
        def __init__(self):
            self.orders = [sell]
            self.positions = []

        def get_today_orders(self):
            return list(self.orders)

        def get_positions(self):
            return list(self.positions)

    broker = _FakeBroker()
    runner.broker = cast(BrokerGateway, broker)

    def _reader_factory():
        def preview(**kwargs):
            evidence = []
            for row in (buy, sell):
                evidence.append(SimpleNamespace(order_id=row.broker_order_id, symbol=row.symbol,
                    side=row.side, submitted_quantity=row.quantity, submitted_price=row.price,
                    executed_quantity=row.executed_quantity, executed_price=row.executed_price,
                    submitted_at=row.created_at, first_executed_at=row.filled_at,
                    last_executed_at=row.filled_at))
            return SimpleNamespace(proof=SimpleNamespace(broker_identity_fingerprint=IDENTITY),
                                   filled_orders=evidence)
        return SimpleNamespace(preview=preview)

    monkeypatch.setattr(
        "app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env",
        _reader_factory)

    from app.services.external_order_acknowledgement_service import canonical_group, group_digest
    group = canonical_group(IDENTITY, _facts(buy), _facts(sell))
    request = dict(broker_identity_fingerprint=IDENTITY, buy=_facts(buy), sell=_facts(sell),
                   digest=group_digest(group), confirmation_reason="These completed orders were mine",
                   actor_hash="owner")

    def sync() -> int:
        return runner.sync_today_orders_from_broker(force=True)

    case = SimpleNamespace(engine=engine, runner=runner, broker=broker, buy=buy, sell=sell,
                           group=group, clock=clock, request=request, sync=sync)
    # The owner BUY is historical (yesterday); only the SELL is in today
    # orders, so the first real sync writes the sell row itself and latches
    # the operational pause through the real latch code path.
    with Session(engine) as db:
        _record(db, buy)
        db.commit()
    yield case
    engine.dispose()


def _ack_events(case) -> int:
    with Session(case.engine) as db:
        return db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count()


def _attempt(case) -> dict:
    try:
        return case.runner.acknowledge_external_round_trip(**case.request)
    except (ValueError, RuntimeError) as exc:
        return {"status": "REJECTED", "error": repr(exc)}


def test_real_sync_relatch_keeps_ack_key_so_second_proof_acknowledges(case):
    """The load-bearing regression: between the two ACK attempts a real 15s
    sync cycle re-latches the *identical* halt. That re-latch must not change
    the pause verification token, so the second proof ACKs instead of
    restarting PROOF_PENDING forever."""
    case.sync()
    assert case.runner.risk.paused
    pause = case.runner.risk.pause_verification_snapshot()
    assert pause[0].startswith("ORDER_RECONCILIATION_UNCERTAIN:")

    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"

    case.clock[0] += 15
    for _ in range(3):
        case.sync()
    # Identical re-latch: byte-identical reason and unchanged generation.
    assert case.runner.risk.pause_verification_snapshot() == pause
    assert case.runner.risk.paused

    case.clock[0] += 5
    outcome = _attempt(case)
    assert outcome["status"] == "ACKNOWLEDGED", outcome
    assert case.runner.risk.paused
    assert case.runner.risk.pause_verification_snapshot()[0] == pause[0]
    assert _ack_events(case) == 1


def test_real_sync_relatch_does_not_block_repeated_cycles(case):
    """Several consecutive identical sync cycles stay no-ops; only the pause
    latch from the first cycle ever exists."""
    case.sync()
    pause = case.runner.risk.pause_verification_snapshot()
    for _ in range(4):
        case.clock[0] += 15
        case.sync()
    assert case.runner.risk.pause_verification_snapshot() == pause
    assert case.runner.risk.protective_exit_permitted is False


# ---------------------------------------------------------------------------
# Latch unit behaviour (real RiskController, DB helpers neutralised).
# ---------------------------------------------------------------------------

def _bare_runner(monkeypatch) -> AppRunner:
    runner = AppRunner()
    monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda db=None: None)
    monkeypatch.setattr(runner, "_record_risk_event", lambda _reason, db=None: None)
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    return runner


def test_identical_relatch_without_permission_keeps_generation(monkeypatch):
    runner = _bare_runner(monkeypatch)
    issues = ["broker live or terminal order id=1 lacks local submission provenance"]
    assert runner._latch_live_order_reconciliation({}, list(issues)) is True
    first = runner.risk.pause_verification_snapshot()
    assert runner._latch_live_order_reconciliation({}, list(issues)) is True
    # The repeated latch is a no-op: same reason, no permission in effect.
    assert runner.risk.pause_verification_snapshot() == first
    assert runner.risk.protective_exit_permitted is False


def test_identical_relatch_with_granted_permission_still_revokes(monkeypatch):
    runner = _bare_runner(monkeypatch)
    issues = ["broker live or terminal order id=1 lacks local submission provenance"]
    assert runner._latch_live_order_reconciliation({}, list(issues)) is True
    assert runner.risk.permit_protective_exits() is True
    generation = runner.risk.pause_verification_snapshot()[1]
    assert runner._latch_live_order_reconciliation({}, list(issues)) is True
    assert runner.risk.protective_exit_permitted is False
    assert runner.risk.pause_verification_snapshot()[1] != generation


def test_changed_relatch_still_invalidates_and_records(monkeypatch):
    runner = _bare_runner(monkeypatch)
    issues = ["broker live or terminal order id=1 lacks local submission provenance"]
    assert runner._latch_live_order_reconciliation({}, list(issues)) is True
    first = runner.risk.pause_verification_snapshot()
    bigger = list(issues) + ["broker live or terminal order id=2 lacks local submission provenance"]
    assert runner._latch_live_order_reconciliation({"AAPL.US": ["live-2"]}, bigger) is True
    second = runner.risk.pause_verification_snapshot()
    assert second[0] != first[0]
    assert second[1] != first[1]


@pytest.mark.parametrize("mutation", ["clear", "replace"])
def test_pause_mutation_before_guard_acquisition_relatches_unsafe_issue(monkeypatch, mutation):
    runner = _bare_runner(monkeypatch)
    issues = ["broker live or terminal order id=1 lacks local submission provenance"]
    assert runner._latch_live_order_reconciliation({}, issues) is True
    original_reason = runner.risk.pause_reason
    original_guard = runner.risk.protective_permission_guard

    @contextmanager
    def mutate_before_acquisition():
        if mutation == "clear":
            runner.risk.resume()
        else:
            runner.risk.pause("POSITION_RECONCILIATION_UNCERTAIN: changed pause")
        with original_guard() as permitted:
            yield permitted

    monkeypatch.setattr(runner.risk, "protective_permission_guard", mutate_before_acquisition)
    assert runner._latch_live_order_reconciliation({}, issues) is True
    assert runner.risk.paused
    assert runner.risk.pause_reason == original_reason
    assert runner.risk.protective_exit_permitted is False


# ---------------------------------------------------------------------------
# Controls: independent invalidations and timing bounds are unchanged.
# ---------------------------------------------------------------------------

def test_kill_switch_between_proofs_still_rejects(case):
    case.sync()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    case.runner.risk.enable_kill_switch("manual halt")
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert _ack_events(case) == 0


def test_identity_change_between_proofs_still_rejects(case):
    case.sync()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    case.runner._broker_identity_fingerprint = "b" * 64
    with pytest.raises(ValueError, match="identity"):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert _ack_events(case) == 0


def test_new_live_order_during_ack_window_still_rejects(case):
    case.sync()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    case.broker.orders.append(BrokerOrder("live-during-proof", "NVDL.US", "BUY",
                                          Decimal(200), Decimal("37.50"), Decimal(0),
                                          Decimal(0), "SUBMITTED", SELL_AT, None))
    case.sync()
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert _ack_events(case) == 0


def test_first_proof_ttl_still_bounded_while_sync_keeps_relatching(case):
    case.sync()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 15
    case.sync()
    case.clock[0] += 400  # far beyond the 300s first-proof TTL
    case.sync()
    assert _attempt(case).get("status") != "ACKNOWLEDGED"
    assert _ack_events(case) == 0
