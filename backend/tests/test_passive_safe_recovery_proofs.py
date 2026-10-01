"""Safe recovery requires durable proof, not just an idempotent receipt."""
from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.domain.passive_allocation import protocol as pp
from app.domain.passive_allocation import recovery as rt
from app.models import OrderRecord, PassiveMandate
from tests.test_passive_recovery import (
    ORDER_ID, _LAST_CALLS, _Setup, _broker_fact, _holding, _intent_json, _known_row, _local_fact,
)
from tests.test_runner_passive_startup_integration import (
    _FakeStartupBroker, _assert_full_startup, _seed_known, _startup_environment,
)
from app.services.trade_execution_service import _passive_config_snapshot_json


@pytest.fixture(autouse=True)
def _isolate_shared_adapter_calls() -> Iterator[None]:
    before = list(_LAST_CALLS)
    try:
        yield
    finally:
        _LAST_CALLS[:] = before


def _conflict(service: Any, row: rt.MandateRowFacts) -> None:
    assert row.intent is not None and row.claim_token and row.execution_token
    owner = pp.PassiveOwner(
        ref=pp.PassiveAttemptRef(mandate_id=row.mandate_id, claim_token=row.claim_token),
        execution_token=row.execution_token, intent=row.intent,
    )
    # Terminal swap, or a backwards status for the live PARTIAL_FILLED row.
    status = "FILLED" if row.bound_status == "CANCELLED" else "SUBMITTED"
    result = service.observation_hooks().record_outcome(owner, pp.PassiveOutcomeFact(
        outcome=pp.SUBMIT_STATE_ORDER_KNOWN,
        broker_order_id=row.bound_broker_order_id or "", broker_status=status,
        executed_quantity=Decimal("1"), executed_price=Decimal("600"),
    ))
    assert result is pp.OutcomeWriteResult.ESCALATED_UNCERTAIN


@pytest.mark.parametrize("status,qty", [("CANCELLED", Decimal("0")), ("PARTIAL_FILLED", Decimal("1"))])
def test_stale_inventory_after_real_conflict_never_proves_safe(
    tmp_path: Path, status: str, qty: Decimal,
) -> None:
    price = Decimal("600") if qty else None
    setup = _Setup(tmp_path, [_known_row(
        bound_broker_status=status, bound_executed_quantity=qty, bound_executed_price=price,
    )])
    inv = setup.service.load_inventory()
    _conflict(setup.service, inv.rows[0])
    snap = setup.service.reconcile(inv, order_status=lambda oid: _broker_fact(status, qty, price),
                                   local_order=lambda oid: _local_fact(), holding=_holding())
    row = setup.row()
    assert row is not None and row.submit_state == "UNCERTAIN"
    assert snap.hard_reasons
    assert snap.decisions[0].cls == rt.RecoveryClass.HARD_UNCERTAIN
    assert snap.quarantined_symbols == frozenset({"SPY.US"})
    assert not snap.order_live
    assert setup.incidents() >= 1


@pytest.mark.parametrize("status", ["CANCELLED", "SUBMITTED", "FILLED"])
@pytest.mark.parametrize("fault", ["missing", "read", "id", "ref", "qty", "status", "price", "owner"])
def test_every_safe_class_requires_postwrite_durable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, fault: str,
) -> None:
    qty = Decimal("8") if status == "FILLED" else Decimal("0")
    price = Decimal("600") if qty else None
    setup = _Setup(tmp_path, [_known_row(
        bound_broker_status=status, bound_executed_quantity=qty, bound_executed_price=price,
    )])
    inv = setup.service.load_inventory()
    original = setup.service._record_guarded_observation

    def write_then_drift(row: rt.MandateRowFacts, fact: pp.PassiveOutcomeFact) -> pp.OutcomeWriteResult:
        result = original(row, fact)
        assert result is pp.OutcomeWriteResult.IDEMPOTENT
        if fault == "read":
            def fail_read() -> Any:
                raise OSError("post-write read failed")
            monkeypatch.setattr(setup.service, "load_inventory", fail_read)
        else:
            with setup.sessions() as db:
                durable = db.get(PassiveMandate, row.mandate_id)
                assert durable is not None
                if fault == "missing":
                    db.delete(durable)
                elif fault == "id":
                    durable.bound_broker_order_id = "different-id"
                elif fault == "ref":
                    durable.execution_token = "different-owner"
                elif fault == "qty":
                    durable.bound_executed_quantity = None
                elif fault == "status":
                    durable.bound_broker_status = "REJECTED"
                elif fault == "price":
                    durable.bound_executed_price = Decimal("601")
                elif fault == "owner":
                    durable.entry_authorisation_available = True
                db.commit()
        return result

    monkeypatch.setattr(setup.service, "_record_guarded_observation", write_then_drift)
    snap = setup.service.reconcile(inv, order_status=lambda oid: _broker_fact(status, qty, price),
                                   local_order=lambda oid: _local_fact(), holding=_holding(qty, qty, qty * 600))
    assert snap.hard_reasons
    assert snap.decisions[0].cls == rt.RecoveryClass.HARD_UNCERTAIN
    assert snap.quarantined_symbols == frozenset({"SPY.US"})
    assert setup.incidents() >= 1


@pytest.mark.parametrize("timing", ["before_epoch", "after_epoch"])
def test_full_manual_refresh_conflict_keeps_durable_uncertainty_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timing: str,
) -> None:
    broker = _FakeStartupBroker()
    broker.status, broker.qty = "CANCELLED", Decimal("0")
    with _startup_environment(tmp_path / "refresh.db", broker) as (constructor, sessions):
        _seed_known(sessions, status="CANCELLED", qty=Decimal("0"))
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        assert runner.risk.external_block() is None
        runner.risk.raise_external_block("passive_recovery", "transient read needs verification")
        service = runner._get_passive_recovery_service()
        read = service.load_inventory
        captured = read().rows[0]
        injected = False

        def inject_conflict() -> None:
            nonlocal injected
            if injected:
                return
            injected = True
            _conflict(service, captured)
            runner._passive_uncertainty_sink("actual conflicting receipt", "startup-1")

        if timing == "before_epoch":
            def read_then_conflict() -> Any:
                inventory = read()
                inject_conflict()
                return inventory
            monkeypatch.setattr(service, "load_inventory", read_then_conflict)
        else:
            broker_read = broker.get_order_status
            def order_then_conflict(order_id: str) -> Any:
                inject_conflict()
                return broker_read(order_id)
            monkeypatch.setattr(broker, "get_order_status", order_then_conflict)

        runner._refresh_passive_before_resume_eligibility()
        assert injected
        with sessions() as db:
            row = db.get(PassiveMandate, 1)
            assert row is not None and row.submit_state == "UNCERTAIN"
        assert runner.risk.external_block() is not None
        assert runner._passive_quarantined_symbols == frozenset({"SPY.US"})
        assert not runner.risk.resume_eligibility().approved
        assert runner.risk.pause_reason == "MANUAL: owner pause"
        assert broker.submitted == broker.cancelled == 0


@pytest.mark.parametrize("marker", ["range_fee_only", "missing_lane", "wrong_lane", "missing_protocol", "wrong_protocol", "valid"])
@pytest.mark.parametrize("status", ["CANCELLED", "SUBMITTED"])
def test_full_initialize_final_fact_requires_passive_lane_and_protocol(
    tmp_path: Path, marker: str, status: str,
) -> None:
    broker = _FakeStartupBroker()
    broker.status, broker.qty = status, Decimal("0")
    owner = pp.PassiveOwner(
        ref=pp.PassiveAttemptRef(mandate_id=1, claim_token="claim-r1"),
        execution_token="exec-r1", intent=pp.intent_from_json(_intent_json()),
    )
    config = json.loads(_passive_config_snapshot_json(owner))
    if marker == "range_fee_only":
        config = {"accounting_fee_model": "us-sec98-v1", "strategy_source": "RANGE"}
    elif marker == "missing_lane":
        del config["passive_lane"]
    elif marker == "wrong_lane":
        config["passive_lane"] = "RANGE"
    elif marker == "missing_protocol":
        del config["passive_protocol_version"]
    elif marker == "wrong_protocol":
        config["passive_protocol_version"] = "unrecognized-protocol"
    with _startup_environment(tmp_path / "lane.db", broker) as (constructor, sessions):
        _seed_known(sessions, status=status, qty=Decimal("0"))
        with sessions() as db:
            order = db.query(OrderRecord).filter_by(broker_order_id="startup-1").one()
            order.config_snapshot = json.dumps(config)
            db.commit()
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        if marker == "valid":
            assert runner._passive_recovery_hard_reasons == ()
            assert (runner.risk.external_block() is not None) == (status == "SUBMITTED")
        else:
            assert runner._passive_recovery_hard_reasons
            assert runner.risk.external_block() is not None
            assert runner._passive_quarantined_symbols == frozenset({"SPY.US"})
            assert not runner.risk.resume_eligibility().approved
        if status == "CANCELLED":
            assert runner.risk.pause_reason == "MANUAL: owner pause"
        else:
            # Existing live-order reconciliation owns this operational pause;
            # final passive proof must not clear it or misclassify the lane.
            assert runner.risk.paused
        assert broker.submitted == broker.cancelled == 0
