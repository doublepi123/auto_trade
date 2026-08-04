from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import OrderRecord, TradeEvent
from app.services.daily_pnl_service import (
    DailyPnlService,
    PnlReplayIssue,
    PnlReplayIssueCode,
)
from app.services.historical_order_completeness_reader import (
    HistoricalCompletenessProof,
    HistoricalExecutionEvidence,
    HistoricalFilledOrderEvidence,
    HistoricalOrderPreview,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPE = "HISTORICAL_EXECUTION_IMPORTED"
_SOURCE = "HISTORICAL_LEDGER_IMPORT"
_SCHEMA_VERSION = 1
_DECIMAL_ABS_TOLERANCE = Decimal("0.000000001")
_DECIMAL_REL_TOLERANCE = Decimal("0.000000000001")


class HistoricalLedgerImportError(RuntimeError):
    """Base error for a historical ledger import that failed closed."""


class HistoricalLedgerAuthorizationError(HistoricalLedgerImportError):
    """The freshly fetched evidence does not match operator authorization."""


class HistoricalLedgerConflictError(HistoricalLedgerImportError):
    """Existing durable evidence conflicts with the broker snapshot."""


class HistoricalLedgerReplayError(HistoricalLedgerImportError):
    """The proposed rows introduce a new FIFO ledger issue."""


class HistoricalPreviewReader(Protocol):
    def preview(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        observed_at: datetime | None = None,
    ) -> HistoricalOrderPreview: ...


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class HistoricalLedgerImportPlan:
    proof: HistoricalCompletenessProof
    pending_order_ids: tuple[str, ...]
    existing_order_ids: tuple[str, ...]
    pending_execution_trade_ids: tuple[str, ...]
    existing_execution_trade_ids: tuple[str, ...]
    conflicts: tuple[str, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True)
class HistoricalLedgerApplyResult:
    proof: HistoricalCompletenessProof
    inserted_order_ids: tuple[str, ...]
    skipped_order_ids: tuple[str, ...]
    inserted_execution_trade_ids: tuple[str, ...]
    skipped_execution_trade_ids: tuple[str, ...]
    replay_issue_count_before: int
    replay_issue_count_after: int


@dataclass(frozen=True)
class _WritePlan:
    public: HistoricalLedgerImportPlan
    orders: tuple[HistoricalFilledOrderEvidence, ...]
    executions: tuple[
        tuple[HistoricalFilledOrderEvidence, HistoricalExecutionEvidence], ...
    ]


class HistoricalLedgerImportService:
    """Preview and atomically import complete historical broker evidence.

    This service has no broker mutation or runner dependency. ``apply`` always
    fetches a new complete snapshot, verifies both the preview digest and the
    broker-account fingerprint supplied by the operator, then persists all
    order and execution evidence in one database transaction. It never calls
    live reconciliation and never manufactures cost-basis fields.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        reader: HistoricalPreviewReader,
    ) -> None:
        self._session_factory = session_factory
        self._reader = reader

    def preview(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        observed_at: datetime | None = None,
    ) -> HistoricalLedgerImportPlan:
        snapshot = self._reader.preview(
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
            observed_at=observed_at,
        )
        _validate_snapshot(snapshot)
        with self._session_factory() as db:
            return _build_write_plan(db, snapshot).public

    def apply(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        expected_preview_digest: str,
        expected_broker_identity_fingerprint: str,
        observed_at: datetime | None = None,
    ) -> HistoricalLedgerApplyResult:
        expected_digest = _required_digest(
            expected_preview_digest,
            "expected_preview_digest",
        )
        expected_fingerprint = _required_digest(
            expected_broker_identity_fingerprint,
            "expected_broker_identity_fingerprint",
        )

        # This is intentionally a fresh transport read, never a cached preview.
        snapshot = self._reader.preview(
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
            observed_at=observed_at,
        )
        _validate_snapshot(snapshot)
        proof = snapshot.proof
        if proof.broker_identity_fingerprint != expected_fingerprint:
            raise HistoricalLedgerAuthorizationError(
                "fresh broker-account fingerprint does not match the preview"
            )
        if proof.preview_digest != expected_digest:
            raise HistoricalLedgerAuthorizationError(
                "fresh historical evidence does not match the preview digest"
            )

        try:
            with self._session_factory() as db:
                with db.begin():
                    write_plan = _build_write_plan(db, snapshot)
                    if write_plan.public.conflicts:
                        raise HistoricalLedgerConflictError(
                            "; ".join(write_plan.public.conflicts)
                        )
                    replay_before = DailyPnlService(
                        db
                    ).pair_round_trips_with_issues(
                        symbol=proof.symbol,
                        include_excursions=False,
                    )
                    for order in write_plan.orders:
                        db.add(_new_order(order, proof))
                    for order, execution in write_plan.executions:
                        db.add(_new_execution_event(order, execution, proof))
                    db.flush()
                    replay_after = DailyPnlService(
                        db
                    ).pair_round_trips_with_issues(
                        symbol=proof.symbol,
                        include_excursions=False,
                    )
                    new_issues = _new_replay_issues(
                        replay_before.issues,
                        replay_after.issues,
                    )
                    if new_issues:
                        rendered = ", ".join(
                            _render_replay_issue(issue)
                            for issue in new_issues
                        )
                        raise HistoricalLedgerReplayError(
                            "historical import would introduce ledger issues: "
                            f"{rendered}"
                        )
                    unresolved_full_exits = _unresolved_full_unmatched_exits(
                        replay_before.issues,
                        replay_after.issues,
                    )
                    if unresolved_full_exits:
                        rendered = ", ".join(
                            _render_replay_issue(issue)
                            for issue in unresolved_full_exits
                        )
                        raise HistoricalLedgerReplayError(
                            "historical import did not resolve existing "
                            f"FULL_UNMATCHED_EXIT issues: {rendered}"
                        )
                    result = HistoricalLedgerApplyResult(
                        proof=proof,
                        inserted_order_ids=tuple(
                            item.order_id for item in write_plan.orders
                        ),
                        skipped_order_ids=(
                            write_plan.public.existing_order_ids
                        ),
                        inserted_execution_trade_ids=tuple(
                            execution.trade_id
                            for _, execution in write_plan.executions
                        ),
                        skipped_execution_trade_ids=(
                            write_plan.public.existing_execution_trade_ids
                        ),
                        replay_issue_count_before=len(replay_before.issues),
                        replay_issue_count_after=len(replay_after.issues),
                    )
                return result
        except IntegrityError as exc:
            raise HistoricalLedgerConflictError(
                "historical ledger changed concurrently; transaction rolled back"
            ) from exc


def _build_write_plan(
    db: Session,
    snapshot: HistoricalOrderPreview,
) -> _WritePlan:
    proof = snapshot.proof
    order_by_id = {
        order.order_id: order
        for order in snapshot.filled_orders
    }
    order_ids = tuple(sorted(order_by_id))
    existing_orders = {
        row.broker_order_id: row
        for row in (
            db.query(OrderRecord)
            .filter(OrderRecord.broker_order_id.in_(order_ids))
            .all()
            if order_ids
            else []
        )
    }
    conflicts: list[str] = []
    pending_orders: list[HistoricalFilledOrderEvidence] = []
    skipped_order_ids: list[str] = []
    for order_id in order_ids:
        evidence = order_by_id[order_id]
        existing = existing_orders.get(order_id)
        if existing is None:
            pending_orders.append(evidence)
            continue
        mismatches = _order_mismatches(existing, evidence, proof)
        if mismatches:
            conflicts.append(
                f"broker_order_id {order_id} conflicts on "
                + ", ".join(mismatches)
            )
        else:
            skipped_order_ids.append(order_id)

    expected_events = {
        _source_event_key(proof, execution): (order, execution)
        for order in snapshot.filled_orders
        for execution in order.executions
    }
    desired_keys = tuple(sorted(expected_events))
    related_events = (
        db.query(TradeEvent)
        .filter(
            (TradeEvent.broker_order_id.in_(order_ids))
            | (TradeEvent.source_event_key.in_(desired_keys))
        )
        .all()
        if order_ids and desired_keys
        else []
    )
    _append_account_conflicts(
        conflicts,
        related_events,
        expected_fingerprint=proof.broker_identity_fingerprint,
        desired_order_ids=frozenset(order_ids),
    )
    historical_events = [
        event
        for event in related_events
        if str(event.event_type or "") == _EVENT_TYPE
    ]
    events_by_key: dict[str, list[TradeEvent]] = {}
    historical_by_order: dict[str, list[TradeEvent]] = {}
    for event in historical_events:
        key = str(event.source_event_key or "")
        events_by_key.setdefault(key, []).append(event)
        historical_by_order.setdefault(
            str(event.broker_order_id or ""), []
        ).append(event)

    pending_executions: list[
        tuple[HistoricalFilledOrderEvidence, HistoricalExecutionEvidence]
    ] = []
    skipped_trade_ids: list[str] = []
    for order_id in order_ids:
        order = order_by_id[order_id]
        expected_for_order = {
            _source_event_key(proof, execution): execution
            for execution in order.executions
        }
        existing_for_order = historical_by_order.get(order_id, [])
        existing_keys = {
            str(event.source_event_key or "")
            for event in existing_for_order
        }
        unexpected = existing_keys - set(expected_for_order)
        if unexpected:
            conflicts.append(
                f"broker_order_id {order_id} has unexpected historical "
                "execution evidence"
            )
            continue
        matching_count = sum(
            len(events_by_key.get(key, []))
            for key in expected_for_order
        )
        if matching_count not in {0, len(expected_for_order)}:
            conflicts.append(
                f"broker_order_id {order_id} has a partial historical "
                "execution event set"
            )
            continue
        if matching_count == 0:
            pending_executions.extend(
                (order, execution)
                for execution in order.executions
            )
            continue
        if order_id not in existing_orders:
            conflicts.append(
                f"broker_order_id {order_id} has execution events but no order"
            )
            continue
        for key, execution in expected_for_order.items():
            rows = events_by_key.get(key, [])
            if len(rows) != 1:
                conflicts.append(
                    f"trade_id {execution.trade_id} has duplicate historical "
                    "execution events"
                )
                continue
            event_mismatches = _execution_event_mismatches(
                rows[0],
                order,
                execution,
                proof,
            )
            if event_mismatches:
                conflicts.append(
                    f"trade_id {execution.trade_id} conflicts on "
                    + ", ".join(event_mismatches)
                )
            else:
                skipped_trade_ids.append(execution.trade_id)

    public = HistoricalLedgerImportPlan(
        proof=proof,
        pending_order_ids=tuple(
            item.order_id for item in pending_orders
        ),
        existing_order_ids=tuple(skipped_order_ids),
        pending_execution_trade_ids=tuple(
            execution.trade_id
            for _, execution in pending_executions
        ),
        existing_execution_trade_ids=tuple(skipped_trade_ids),
        conflicts=tuple(dict.fromkeys(conflicts)),
    )
    return _WritePlan(
        public=public,
        orders=tuple(pending_orders),
        executions=tuple(pending_executions),
    )


def _new_order(
    evidence: HistoricalFilledOrderEvidence,
    proof: HistoricalCompletenessProof,
) -> OrderRecord:
    actual_fee, fee_currency = _actual_fee(evidence.raw_json)
    return OrderRecord(
        broker_order_id=evidence.order_id,
        symbol=evidence.symbol,
        side=evidence.side,
        quantity=float(evidence.submitted_quantity),
        price=float(evidence.submitted_price or evidence.executed_price),
        executed_quantity=float(evidence.executed_quantity),
        executed_price=float(evidence.executed_price),
        status="FILLED",
        created_at=evidence.submitted_at,
        # A persisted FILLED aggregate becomes the full order quantity only at
        # its final execution. Using the first partial execution can reorder
        # FIFO lots when fills span distinct timestamps.
        filled_at=evidence.last_executed_at,
        raw_response=_order_raw_response(evidence, proof),
        broker_submitted_at=evidence.submitted_at,
        broker_updated_at=evidence.updated_at,
        actual_fee=(float(actual_fee) if actual_fee is not None else None),
        fee_currency=fee_currency,
        fee_source="ACTUAL" if actual_fee is not None else "UNKNOWN",
    )


def _new_execution_event(
    order: HistoricalFilledOrderEvidence,
    execution: HistoricalExecutionEvidence,
    proof: HistoricalCompletenessProof,
) -> TradeEvent:
    return TradeEvent(
        event_type=_EVENT_TYPE,
        symbol=execution.symbol,
        broker_order_id=execution.order_id,
        side=order.side,
        status="FILLED",
        message="historical broker execution imported",
        payload_json=_canonical_json(
            _execution_event_payload(order, execution, proof)
        ),
        source_event_key=_source_event_key(proof, execution),
        created_at=execution.trade_done_at,
    )


def _order_raw_response(
    order: HistoricalFilledOrderEvidence,
    proof: HistoricalCompletenessProof,
) -> str:
    return _canonical_json({
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE,
        "completeness_proof": _proof_payload(proof),
        "broker_order": _json_object(order.raw_json, "broker order"),
        "broker_executions": [
            _json_object(execution.raw_json, "broker execution")
            for execution in order.executions
        ],
    })


def _execution_event_payload(
    order: HistoricalFilledOrderEvidence,
    execution: HistoricalExecutionEvidence,
    proof: HistoricalCompletenessProof,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE,
        "provider": proof.provider,
        "broker_identity_fingerprint": proof.broker_identity_fingerprint,
        "preview_digest": proof.preview_digest,
        "trade_id": execution.trade_id,
        "order_id": execution.order_id,
        "symbol": execution.symbol,
        "side": order.side,
        "quantity": str(execution.quantity),
        "price": str(execution.price),
        "trade_done_at": execution.trade_done_at.isoformat(),
        "broker_order": _json_object(order.raw_json, "broker order"),
        "broker_execution": _json_object(
            execution.raw_json,
            "broker execution",
        ),
        "completeness_proof": _proof_payload(proof),
    }


def _proof_payload(proof: HistoricalCompletenessProof) -> dict[str, object]:
    return {
        "schema_version": proof.schema_version,
        "provider": proof.provider,
        "broker_identity_fingerprint": proof.broker_identity_fingerprint,
        "symbol": proof.symbol,
        "start_at": proof.start_at.isoformat(),
        "end_at": proof.end_at.isoformat(),
        "orders_has_more": proof.orders_has_more,
        "executions_has_more": proof.executions_has_more,
        "order_count": proof.order_count,
        "execution_count": proof.execution_count,
        "filled_order_count": proof.filled_order_count,
        "orders_response_digest": proof.orders_response_digest,
        "executions_response_digest": proof.executions_response_digest,
        "preview_digest": proof.preview_digest,
    }


def _source_event_key(
    proof: HistoricalCompletenessProof,
    execution: HistoricalExecutionEvidence,
) -> str:
    bound_identity = "\0".join((
        proof.provider,
        proof.broker_identity_fingerprint,
        execution.trade_id,
    ))
    return hashlib.sha256(bound_identity.encode("utf-8")).hexdigest()


def _order_mismatches(
    existing: OrderRecord,
    evidence: HistoricalFilledOrderEvidence,
    proof: HistoricalCompletenessProof,
) -> list[str]:
    mismatches: list[str] = []
    expected_price = evidence.submitted_price or evidence.executed_price
    if str(existing.symbol or "").upper() != evidence.symbol:
        mismatches.append("symbol")
    if str(existing.side or "").upper() != evidence.side:
        mismatches.append("side")
    if str(existing.status or "").upper() != "FILLED":
        mismatches.append("status")
    if not _number_matches(existing.quantity, evidence.submitted_quantity):
        mismatches.append("quantity")
    if not _number_matches(existing.price, expected_price):
        mismatches.append("price")
    if not _number_matches(
        existing.executed_quantity,
        evidence.executed_quantity,
    ):
        mismatches.append("executed_quantity")
    if not _number_matches(existing.executed_price, evidence.executed_price):
        mismatches.append("executed_price")
    if not _datetime_matches(existing.filled_at, evidence.last_executed_at):
        mismatches.append("filled_at")
    if not _datetime_matches(existing.created_at, evidence.submitted_at):
        mismatches.append("created_at")
    if not _datetime_matches(
        existing.broker_submitted_at,
        evidence.submitted_at,
    ):
        mismatches.append("broker_submitted_at")
    if not _datetime_matches(existing.broker_updated_at, evidence.updated_at):
        mismatches.append("broker_updated_at")
    actual_fee, fee_currency = _actual_fee(evidence.raw_json)
    if actual_fee is not None:
        if not _number_matches(existing.actual_fee, actual_fee):
            mismatches.append("actual_fee")
        if str(existing.fee_source or "").upper() != "ACTUAL":
            mismatches.append("fee_source")
        if str(existing.fee_currency or "").upper() != fee_currency:
            mismatches.append("fee_currency")
    mismatches.extend(
        _imported_order_raw_mismatches(existing.raw_response, evidence, proof)
    )
    return mismatches


def _imported_order_raw_mismatches(
    raw_response: str | None,
    evidence: HistoricalFilledOrderEvidence,
    proof: HistoricalCompletenessProof,
) -> list[str]:
    if not raw_response:
        return []
    try:
        persisted = _json_object(raw_response, "persisted order raw response")
    except HistoricalLedgerImportError:
        # Legacy/live order responses are outside this importer's evidence
        # namespace and are compared through the typed broker fields above.
        return []
    if persisted.get("source") != _SOURCE:
        return []
    completeness = persisted.get("completeness_proof")
    expected_orders = [
        _json_object(execution.raw_json, "broker execution")
        for execution in evidence.executions
    ]
    if (
        persisted.get("schema_version") != _SCHEMA_VERSION
        or persisted.get("broker_order")
        != _json_object(evidence.raw_json, "broker order")
        or persisted.get("broker_executions") != expected_orders
        or not isinstance(completeness, Mapping)
        or completeness.get("provider") != proof.provider
        or completeness.get("broker_identity_fingerprint")
        != proof.broker_identity_fingerprint
    ):
        return ["raw_response"]
    return []


def _execution_event_mismatches(
    event: TradeEvent,
    order: HistoricalFilledOrderEvidence,
    execution: HistoricalExecutionEvidence,
    proof: HistoricalCompletenessProof,
) -> list[str]:
    mismatches: list[str] = []
    if str(event.event_type or "") != _EVENT_TYPE:
        mismatches.append("event_type")
    if str(event.symbol or "").upper() != execution.symbol:
        mismatches.append("symbol")
    if str(event.broker_order_id or "") != execution.order_id:
        mismatches.append("broker_order_id")
    if str(event.side or "").upper() != order.side:
        mismatches.append("side")
    if str(event.status or "").upper() != "FILLED":
        mismatches.append("status")
    if not _datetime_matches(event.created_at, execution.trade_done_at):
        mismatches.append("created_at")
    try:
        payload = _json_object(event.payload_json, "historical execution event")
    except HistoricalLedgerImportError:
        return [*mismatches, "payload_json"]
    expected = _execution_event_payload(order, execution, proof)
    # A complete overlapping window can have a different preview proof. The
    # execution identity and raw broker evidence must still be byte-for-byte
    # canonical-equivalent, while original provenance remains immutable.
    for ignored in ("preview_digest", "completeness_proof"):
        payload.pop(ignored, None)
        expected.pop(ignored, None)
    if payload != expected:
        mismatches.append("payload_json")
    return mismatches


def _append_account_conflicts(
    conflicts: list[str],
    events: list[TradeEvent],
    *,
    expected_fingerprint: str,
    desired_order_ids: frozenset[str],
) -> None:
    for event in events:
        if str(event.broker_order_id or "") not in desired_order_ids:
            continue
        try:
            payload = _json_object(event.payload_json, "trade event")
        except HistoricalLedgerImportError:
            continue
        raw_fingerprint = payload.get("broker_identity_fingerprint")
        if (
            isinstance(raw_fingerprint, str)
            and raw_fingerprint.strip()
            and raw_fingerprint.strip().lower() != expected_fingerprint
        ):
            conflicts.append(
                f"broker_order_id {event.broker_order_id} is bound to a "
                "different broker account"
            )


def _validate_snapshot(snapshot: HistoricalOrderPreview) -> None:
    proof = snapshot.proof
    _required_digest(proof.preview_digest, "proof.preview_digest")
    _required_digest(
        proof.broker_identity_fingerprint,
        "proof.broker_identity_fingerprint",
    )
    _required_digest(
        proof.orders_response_digest,
        "proof.orders_response_digest",
    )
    _required_digest(
        proof.executions_response_digest,
        "proof.executions_response_digest",
    )
    if proof.orders_has_more or proof.executions_has_more:
        raise HistoricalLedgerImportError(
            "historical snapshot lacks explicit completeness"
        )
    if proof.start_at.tzinfo is None or proof.end_at.tzinfo is None:
        raise HistoricalLedgerImportError("proof timestamps must be timezone-aware")
    if proof.end_at <= proof.start_at:
        raise HistoricalLedgerImportError("proof window is invalid")
    if proof.filled_order_count != len(snapshot.filled_orders):
        raise HistoricalLedgerImportError("filled order count does not match proof")
    if proof.order_count < proof.filled_order_count:
        raise HistoricalLedgerImportError("order count does not match proof")
    seen_order_ids: set[str] = set()
    seen_trade_ids: set[str] = set()
    execution_count = 0
    for order in snapshot.filled_orders:
        if order.order_id in seen_order_ids:
            raise HistoricalLedgerImportError(
                f"duplicate broker_order_id {order.order_id}"
            )
        seen_order_ids.add(order.order_id)
        if order.symbol != proof.symbol or order.side not in {"BUY", "SELL"}:
            raise HistoricalLedgerImportError(
                f"order {order.order_id} conflicts with proof identity"
            )
        if not order.executions:
            raise HistoricalLedgerImportError(
                f"order {order.order_id} has no execution evidence"
            )
        if order.first_executed_at != order.executions[0].trade_done_at:
            raise HistoricalLedgerImportError(
                f"order {order.order_id} first execution timestamp mismatch"
            )
        if order.last_executed_at != order.executions[-1].trade_done_at:
            raise HistoricalLedgerImportError(
                f"order {order.order_id} last execution timestamp mismatch"
            )
        raw_order = _json_object(order.raw_json, "broker order")
        _validate_raw_order(raw_order, order)
        total_quantity = Decimal("0")
        total_value = Decimal("0")
        for execution in order.executions:
            execution_count += 1
            if execution.trade_id in seen_trade_ids:
                raise HistoricalLedgerImportError(
                    f"duplicate broker trade_id {execution.trade_id}"
                )
            seen_trade_ids.add(execution.trade_id)
            if (
                execution.order_id != order.order_id
                or execution.symbol != order.symbol
            ):
                raise HistoricalLedgerImportError(
                    f"execution {execution.trade_id} conflicts with its order"
                )
            if not (
                proof.start_at
                <= execution.trade_done_at
                <= proof.end_at
            ):
                raise HistoricalLedgerImportError(
                    f"execution {execution.trade_id} is outside the proof window"
                )
            raw_execution = _json_object(
                execution.raw_json,
                "broker execution",
            )
            _validate_raw_execution(raw_execution, execution)
            total_quantity += execution.quantity
            total_value += execution.quantity * execution.price
        if total_quantity != order.executed_quantity:
            raise HistoricalLedgerImportError(
                f"order {order.order_id} execution quantity mismatch"
            )
        weighted_price = total_value / total_quantity
        if not _decimal_matches(weighted_price, order.executed_price):
            raise HistoricalLedgerImportError(
                f"order {order.order_id} execution price mismatch"
            )
        _actual_fee(order.raw_json)
    if execution_count != proof.execution_count:
        raise HistoricalLedgerImportError(
            "execution count does not match completeness proof"
        )


def _validate_raw_order(
    raw: Mapping[str, object],
    order: HistoricalFilledOrderEvidence,
) -> None:
    expected_side = "Buy" if order.side == "BUY" else "Sell"
    checks = {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": expected_side,
        "status": "FilledStatus",
    }
    for key, expected in checks.items():
        if str(raw.get(key, "")) != expected:
            raise HistoricalLedgerImportError(
                f"broker order {order.order_id} raw {key} mismatch"
            )
    numeric_checks = {
        "quantity": order.submitted_quantity,
        "executed_quantity": order.executed_quantity,
        "executed_price": order.executed_price,
    }
    for key, expected in numeric_checks.items():
        if not _number_matches(raw.get(key), expected):
            raise HistoricalLedgerImportError(
                f"broker order {order.order_id} raw {key} mismatch"
            )


def _validate_raw_execution(
    raw: Mapping[str, object],
    execution: HistoricalExecutionEvidence,
) -> None:
    checks = {
        "order_id": execution.order_id,
        "trade_id": execution.trade_id,
        "symbol": execution.symbol,
    }
    for key, expected in checks.items():
        if str(raw.get(key, "")) != expected:
            raise HistoricalLedgerImportError(
                f"broker execution {execution.trade_id} raw {key} mismatch"
            )
    if not _number_matches(raw.get("quantity"), execution.quantity):
        raise HistoricalLedgerImportError(
            f"broker execution {execution.trade_id} raw quantity mismatch"
        )
    if not _number_matches(raw.get("price"), execution.price):
        raise HistoricalLedgerImportError(
            f"broker execution {execution.trade_id} raw price mismatch"
        )
    raw_done_at = raw.get("trade_done_at")
    try:
        raw_epoch = int(str(raw_done_at))
    except (TypeError, ValueError) as exc:
        raise HistoricalLedgerImportError(
            f"broker execution {execution.trade_id} raw timestamp is invalid"
        ) from exc
    if datetime.fromtimestamp(raw_epoch, tz=timezone.utc) != execution.trade_done_at:
        raise HistoricalLedgerImportError(
            f"broker execution {execution.trade_id} raw timestamp mismatch"
        )


def _actual_fee(raw_json: str) -> tuple[Decimal | None, str]:
    raw = _json_object(raw_json, "broker order")
    charge_detail = raw.get("charge_detail")
    if charge_detail is None:
        return None, ""
    if not isinstance(charge_detail, Mapping):
        raise HistoricalLedgerImportError(
            "broker order charge_detail must be an object"
        )
    raw_total = charge_detail.get("total_amount")
    raw_currency = charge_detail.get("currency")
    if raw_total is None or not isinstance(raw_currency, str):
        raise HistoricalLedgerImportError(
            "broker order charge_detail lacks total_amount or currency"
        )
    total = _decimal(raw_total, "charge_detail.total_amount", allow_zero=True)
    currency = raw_currency.strip().upper()
    if not currency or len(currency) > 10:
        raise HistoricalLedgerImportError(
            "broker order charge_detail currency is invalid"
        )
    return total, currency


def _new_replay_issues(
    before: list[PnlReplayIssue],
    after: list[PnlReplayIssue],
) -> list[PnlReplayIssue]:
    before_keys = {_replay_issue_key(issue) for issue in before}
    return [
        issue
        for issue in after
        if _replay_issue_key(issue) not in before_keys
    ]


def _unresolved_full_unmatched_exits(
    before: list[PnlReplayIssue],
    after: list[PnlReplayIssue],
) -> list[PnlReplayIssue]:
    after_keys = {_replay_issue_key(issue) for issue in after}
    return [
        issue
        for issue in before
        if issue.issue_code is PnlReplayIssueCode.FULL_UNMATCHED_EXIT
        and _replay_issue_key(issue) in after_keys
    ]


def _replay_issue_key(issue: PnlReplayIssue) -> tuple[object, ...]:
    return (
        issue.issue_code.value,
        issue.symbol,
        issue.side,
        issue.trade_day.isoformat(),
        _as_utc(issue.filled_at).isoformat(),
        issue.exit_broker_order_id,
        str(issue.filled_quantity),
        str(issue.matched_quantity),
        str(issue.unmatched_quantity),
    )


def _render_replay_issue(issue: PnlReplayIssue) -> str:
    return (
        f"{issue.issue_code.value}:{issue.exit_broker_order_id or issue.exit_order_id}"
    )


def _required_digest(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _DIGEST_RE.fullmatch(normalized):
        raise HistoricalLedgerAuthorizationError(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return normalized


def _json_object(raw_json: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalLedgerImportError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HistoricalLedgerImportError(f"{label} must be a JSON object")
    return {
        str(key): item
        for key, item in value.items()
    }


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HistoricalLedgerImportError(
            "historical evidence is not canonical JSON"
        ) from exc


def _number_matches(value: object, expected: Decimal) -> bool:
    try:
        actual = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return _decimal_matches(actual, expected)


def _decimal_matches(left: Decimal, right: Decimal) -> bool:
    if not left.is_finite() or not right.is_finite():
        return False
    tolerance = max(
        _DECIMAL_ABS_TOLERANCE,
        max(abs(left), abs(right)) * _DECIMAL_REL_TOLERANCE,
    )
    return abs(left - right) <= tolerance


def _decimal(value: object, label: str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise HistoricalLedgerImportError(f"{label} must be a decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HistoricalLedgerImportError(f"{label} must be a decimal") from exc
    if (
        not parsed.is_finite()
        or parsed < 0
        or (not allow_zero and parsed == 0)
    ):
        raise HistoricalLedgerImportError(f"{label} is invalid")
    return parsed


def _datetime_matches(value: datetime | None, expected: datetime) -> bool:
    if value is None:
        return False
    return _as_utc(value) == _as_utc(expected)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def import_plan_payload(plan: HistoricalLedgerImportPlan) -> dict[str, object]:
    """Return the stable, secret-free CLI representation of a preview."""
    return {
        "mode": "PREVIEW",
        "can_apply": plan.can_apply,
        "provider": plan.proof.provider,
        "broker_identity_fingerprint": (
            plan.proof.broker_identity_fingerprint
        ),
        "symbol": plan.proof.symbol,
        "start_at": plan.proof.start_at.isoformat(),
        "end_at": plan.proof.end_at.isoformat(),
        "preview_digest": plan.proof.preview_digest,
        "orders_response_digest": plan.proof.orders_response_digest,
        "executions_response_digest": plan.proof.executions_response_digest,
        "broker_order_count": plan.proof.order_count,
        "broker_execution_count": plan.proof.execution_count,
        "filled_order_count": plan.proof.filled_order_count,
        "pending_order_ids": list(plan.pending_order_ids),
        "existing_order_ids": list(plan.existing_order_ids),
        "pending_execution_trade_ids": list(
            plan.pending_execution_trade_ids
        ),
        "existing_execution_trade_ids": list(
            plan.existing_execution_trade_ids
        ),
        "conflicts": list(plan.conflicts),
        "database_mutated": False,
        "order_submission_allowed": False,
        "live_reconciliation_triggered": False,
        "cost_basis_inferred": False,
    }


def apply_result_payload(
    result: HistoricalLedgerApplyResult,
) -> dict[str, object]:
    """Return the stable, secret-free CLI representation of an apply."""
    return {
        "mode": "APPLIED",
        "provider": result.proof.provider,
        "broker_identity_fingerprint": (
            result.proof.broker_identity_fingerprint
        ),
        "symbol": result.proof.symbol,
        "start_at": result.proof.start_at.isoformat(),
        "end_at": result.proof.end_at.isoformat(),
        "preview_digest": result.proof.preview_digest,
        "inserted_order_ids": list(result.inserted_order_ids),
        "skipped_order_ids": list(result.skipped_order_ids),
        "inserted_execution_trade_ids": list(
            result.inserted_execution_trade_ids
        ),
        "skipped_execution_trade_ids": list(
            result.skipped_execution_trade_ids
        ),
        "replay_issue_count_before": result.replay_issue_count_before,
        "replay_issue_count_after": result.replay_issue_count_after,
        "database_mutated": bool(
            result.inserted_order_ids
            or result.inserted_execution_trade_ids
        ),
        "order_submission_allowed": False,
        "live_reconciliation_triggered": False,
        "cost_basis_inferred": False,
    }
