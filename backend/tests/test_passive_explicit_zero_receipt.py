"""Explicit first zero is information, never a default for missing quantity."""
from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.passive_allocation import protocol as pp
from app.models import PassiveMandate
from tests.test_passive_recovery import (
    ORDER_ID, _LAST_CALLS, _Setup, _broker_fact, _holding, _known_row, _local_fact, _reconcile,
)


@pytest.fixture(autouse=True)
def _isolate_shared_adapter_call_log() -> Iterator[None]:
    previous = list(_LAST_CALLS)
    try:
        yield
    finally:
        _LAST_CALLS[:] = previous


def _fact(status: str, qty: Decimal | None, price: Decimal | None = None) -> pp.PassiveOutcomeFact:
    return pp.PassiveOutcomeFact(
        outcome=pp.SUBMIT_STATE_ORDER_KNOWN, broker_order_id=ORDER_ID,
        broker_status=status, executed_quantity=qty, executed_price=price,
    )


def _owner(setup: _Setup) -> pp.PassiveOwner:
    row = setup.service.load_inventory().rows[0]
    assert row.intent is not None
    return pp.PassiveOwner(
        ref=pp.PassiveAttemptRef(mandate_id=row.mandate_id, claim_token="c1"),
        execution_token="e1", intent=row.intent,
    )


@pytest.mark.parametrize("status", ["SUBMITTED", "REJECTED", "CANCELLED"])
def test_first_explicit_zero_is_forward_and_duplicate_is_exact(status: str) -> None:
    fact = _fact(status, Decimal("0"))
    assert pp.compare_receipt_facts(
        fact=fact, bound_broker_status=status, bound_executed_quantity=None,
        bound_executed_price=None, intent_quantity=Decimal("8"),
    ) == "FORWARD"
    assert pp.compare_receipt_facts(
        fact=fact, bound_broker_status=status, bound_executed_quantity=Decimal("0"),
        bound_executed_price=None, intent_quantity=Decimal("8"),
    ) == "EXACT"


@pytest.mark.parametrize("status", ["SUBMITTED", "REJECTED", "CANCELLED"])
def test_real_hook_records_first_zero_then_idempotent_duplicate(tmp_path: Path, status: str) -> None:
    setup = _Setup(tmp_path, [_known_row(bound_broker_status=status)])
    hooks = setup.service.observation_hooks()
    owner, fact = _owner(setup), _fact(status, Decimal("0"))
    assert hooks.record_outcome(owner, fact) is pp.OutcomeWriteResult.APPLIED
    row = setup.row()
    assert row is not None and row.submit_state == "ORDER_KNOWN"
    assert row.bound_executed_quantity == Decimal("0")
    assert row.bound_broker_order_id == ORDER_ID
    assert hooks.record_outcome(owner, fact) is pp.OutcomeWriteResult.IDEMPOTENT


@pytest.mark.parametrize("status", ["SUBMITTED", "REJECTED", "CANCELLED"])
def test_real_recovery_publishes_only_after_first_zero_persisted(tmp_path: Path, status: str) -> None:
    setup = _Setup(tmp_path, [_known_row(bound_broker_status=status)])
    snap = _reconcile(setup, order=_broker_fact(status, Decimal("0")),
                      local=_local_fact(), holding=_holding())
    assert not snap.hard_reasons
    assert snap.order_live == (status == "SUBMITTED")
    row = setup.row()
    assert row is not None and row.submit_state == "ORDER_KNOWN"
    assert row.bound_executed_quantity == Decimal("0")


@pytest.mark.parametrize("old_status,new_status,old_qty,new_qty,old_price,new_price", [
    ("CANCELLED", "CANCELLED", Decimal("0"), None, None, None),
    ("CANCELLED", "CANCELLED", Decimal("5"), Decimal("0"), None, None),
    ("FILLED", "FILLED", None, Decimal("0"), None, None),
    ("PARTIAL_FILLED", "PARTIAL_FILLED", None, Decimal("0"), None, None),
    ("UNKNOWN", "UNKNOWN", None, Decimal("0"), None, None),
    ("", "", None, Decimal("0"), None, None),
    ("CANCELLED", "REJECTED", None, Decimal("0"), None, None),
    ("CANCELLED", "SUBMITTED", None, Decimal("0"), None, None),
    ("PARTIAL_FILLED", "SUBMITTED", None, Decimal("0"), None, None),
    ("CANCELLED", "CANCELLED", None, Decimal("0"), Decimal("600"), Decimal("600.01")),
    ("SUBMITTED", "SUBMITTED", None, Decimal("0"), Decimal("600"), None),
    ("SUBMITTED", "SUBMITTED", None, Decimal("9"), None, Decimal("600")),
])
def test_incoherent_or_regressive_observation_stays_conflict(
    tmp_path: Path, old_status: str, new_status: str,
    old_qty: Decimal | None, new_qty: Decimal | None,
    old_price: Decimal | None, new_price: Decimal | None,
) -> None:
    fact = _fact(new_status, new_qty, new_price)
    assert pp.compare_receipt_facts(
        fact=fact, bound_broker_status=old_status, bound_executed_quantity=old_qty,
        bound_executed_price=old_price, intent_quantity=Decimal("8"),
    ) == "BACKWARD"
    setup = _Setup(tmp_path, [_known_row(
        bound_broker_status=old_status, bound_executed_quantity=old_qty,
        bound_executed_price=old_price,
    )])
    result = setup.service.observation_hooks().record_outcome(_owner(setup), fact)
    assert result is pp.OutcomeWriteResult.ESCALATED_UNCERTAIN
    row = setup.row()
    assert row is not None and row.submit_state == "UNCERTAIN"
    assert row.bound_broker_order_id == ORDER_ID
    assert row.bound_executed_quantity == old_qty


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_missing_new_quantity_never_proves_terminal_no_fill(tmp_path: Path, status: str) -> None:
    setup = _Setup(tmp_path, [_known_row(bound_broker_status=status)])
    snap = _reconcile(setup, order=_broker_fact(status, None),
                      local=_local_fact(), holding=_holding())
    assert snap.hard_reasons
    row = setup.row()
    assert row is not None and row.bound_executed_quantity is None


def test_first_zero_never_clears_persisted_uncertainty(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, [_known_row(bound_broker_status="CANCELLED", submit_state="UNCERTAIN")])
    hooks = setup.service.observation_hooks()
    # The existing same-ID sticky-state contract returns IDEMPOTENT;
    # that is not permission to clear the durable UNCERTAIN state.
    assert hooks.record_outcome(_owner(setup), _fact("CANCELLED", Decimal("0"))) is pp.OutcomeWriteResult.IDEMPOTENT
    with setup.sessions() as db:
        row = db.get(PassiveMandate, 1)
        assert row is not None and row.submit_state == "UNCERTAIN"
    snap = _reconcile(setup, order=_broker_fact("CANCELLED", Decimal("0")),
                      local=_local_fact(), holding=_holding())
    assert snap.hard_reasons
