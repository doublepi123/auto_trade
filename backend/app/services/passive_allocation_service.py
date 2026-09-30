"""SPY passive buy-and-hold submit protocol service (phase 1, default OFF).

Implements writer-B scope of the architect contract
``spy-passive-submit-contract.md`` (ora-21). The lane is OFF by default and
nothing in the runner calls this service; this module only repairs the
protocol so that WHEN phase 2 wires it, at most ONE broker submission can
exist per owner authorisation.

Protocol (three compare-and-swaps, each verified by affected-row count AND
commit; never a read-back of the target state as ownership proof):

1. **Reservation** ``AUTHORIZED -> SUBMIT_CLAIMED``: random claim token,
   immutable intent snapshot, available=False, consumed_at. A racing loser
   sees zero affected rows and must not touch the winner's row.
2. **Execution ownership** ``SUBMIT_CLAIMED -> CHECKING``: mandate id +
   claim token + a FRESH execution token per execute call.
3. **Submit right** ``CHECKING -> SUBMITTING``: both tokens + ACTIVE
   status + unchanged policy/intent; persists the final order/cash/fee
   snapshot atomically with the transition.

Every outcome write re-checks owner tokens and allowed source states; the
same fact is idempotent, a conflicting fact escalates to UNCERTAIN. A
certain refusal before any broker mutation lands ``NO_SUBMIT`` and the
intent stays burned. ``UNCERTAIN`` means possibly-submitted: non
auto-resumable risk pause, durable risk event, reconciliation incident,
and an explicit uncertain outcome — never success.

Frozen hook signatures (see ``PassiveSubmitHooks`` in
``app.domain.passive_allocation.protocol``)::

    begin_execution(ref, execution_token) -> PassiveOwner | PassiveRejection
    resolve_policy(owner, request) -> ValidatedPassiveIntent | PassiveRejection  [READ ONLY]
    claim_submission(owner, final_order, cash) -> bool
    record_outcome(owner, fact) -> None

All hooks provided or all absent — a partial bundle refuses every passive
marker. Gate readers (lane flag / PAPER attestation) are fresh per call,
never cached constructor booleans.

Phase-2 enable blockers that remain (deliberately not weakened here): real
PAPER account identity + no-borrowing verification bound from the broker,
startup SUBMITTING scan, position ownership/exit exemptions.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.accounting_fees import (
    SEC98_FIXED_USD,
    SEC98_NOTIONAL_RATE,
)
from app.domain.passive_allocation import policy as passive_policy
from app.domain.passive_allocation import protocol as passive_protocol
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
    PassiveEntrySizing,
    PassiveMandateFacts,
)
from app.models import PassiveMandate

if TYPE_CHECKING:
    from app.core.broker import BrokerGateway, Quote
    from app.core.notify import NotifierInterface
    from app.core.risk import RiskController
    from app.services.trade_execution_service import OrderStatus

logger = logging.getLogger("auto_trade.services.passive_allocation_service")

PASSIVE_MARKET = "US"
PASSIVE_CASH_CURRENCY = "USD"
PASSIVE_BIND_UNCERTAIN_PREFIX = "ORDER_RECONCILIATION_UNCERTAIN:"
PASSIVE_INCIDENT_SOURCE = "passive_allocation"
PASSIVE_INCIDENT_CATEGORY = "MANDATE_SUBMIT_UNCERTAIN"

# Reason prefixes that make any returned OrderStatus UNCERTAIN for the lane.
_UNCERTAIN_REASON_PREFIXES = (
    "ORDER_RECONCILIATION_UNCERTAIN:",
    "ORDER_PERSISTENCE_UNCERTAIN:",
    "ORDER_STATUS_PERSISTENCE_UNCERTAIN:",
    "ORDER_SUBMISSION_UNCERTAIN:",
)

GateIssueReader = Callable[[], "str | None"]
Clock = Callable[[], datetime]


def us_paper_commission(price: Decimal, quantity: Decimal) -> Decimal:
    """§9.8 measured US commission model — the ONLY model the lane uses.

    Trusted constant from ``app.core.accounting_fees``; caller context can
    never lower the fee used at the boundary.
    """
    if quantity <= 0:
        return Decimal("0")
    return SEC98_FIXED_USD + SEC98_NOTIONAL_RATE * price * quantity


class _CasCommitFailed(RuntimeError):
    """The UPDATE matched but the COMMIT failed (durability unproven)."""


class PassivePersistenceUncertain(RuntimeError):
    """A durable protocol write could not be proven committed.

    Raised by the burn/quarantine helpers when the UPDATE matched zero
    rows or the COMMIT failed, so a caller can NEVER mistake the row for
    consumed. Callers must surface an explicit uncertainty (incident +
    blocked automatic retry in this process); a total DB outage cannot
    prove durability here or across a restart — the row is NOT claimed
    consumed in that case.
    """

    def __init__(self, scope: str, detail: str) -> None:
        self.scope = scope
        self.detail = detail
        super().__init__(
            f"{scope}: durable outcome unproven ({detail}); the row is NOT "
            "confirmed consumed — treat as uncertainty requiring review"
        )


def _cas_rowcount(execution_result: Any) -> int:
    """Affected-row count of a Core UPDATE (1 = this call won the CAS)."""
    rowcount = getattr(execution_result, "rowcount", None)
    try:
        return int(rowcount) if rowcount is not None else 0
    except (TypeError, ValueError):
        return 0


def _facts_from_row(row: PassiveMandate) -> PassiveMandateFacts:
    from app.domain.passive_allocation.model import RiskModel

    try:
        risk_model = RiskModel(row.risk_model)
    except ValueError as exc:
        raise ValueError(
            f"mandate risk model {row.risk_model!r} is not recognised"
        ) from exc
    return PassiveMandateFacts(
        lane=row.lane,
        policy_version=row.policy_version,
        symbol=row.symbol,
        status=row.status,
        allotment_usd=Decimal(str(row.allotment_usd)),
        risk_model=risk_model,
        exemptions=tuple(
            part.strip() for part in row.exemptions.split(",") if part.strip()
        ),
        review_interval_months=row.review_interval_months,
        order_binding=row.order_binding,
    )


def _policy_snapshot_from_row(
    facts: PassiveMandateFacts,
) -> passive_protocol.PassivePolicySnapshot:
    return passive_protocol.PassivePolicySnapshot(
        policy_version=facts.policy_version,
        allotment_usd=facts.allotment_usd,
        risk_model=facts.risk_model.value,
        exemptions=facts.exemptions,
        order_binding=facts.order_binding,
        review_interval_months=facts.review_interval_months,
    )


def _row_intent_or_issue(
    row: PassiveMandate,
) -> passive_protocol.ImmutablePassiveIntent | str:
    if not row.intent_json:
        return "mandate row carries no immutable intent snapshot"
    try:
        return passive_protocol.intent_from_json(row.intent_json)
    except ValueError as exc:
        return f"stored immutable intent snapshot is unusable: {exc}"


# ---------------------------------------------------------------------------
# The frozen hook bundle
# ---------------------------------------------------------------------------


class PassiveSubmitHookBundle:
    """All-or-nothing DB hooks injected into ``TradeExecutionService``.

    The service uses ONLY a reservation it just won. Every hook re-checks
    the owner tokens against the row before mutating, so a CAS loser (or a
    forged ref) can never change the winner's outcome.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        current_gate_issue: GateIssueReader,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._current_gate_issue = current_gate_issue
        self._clock = clock

    def inject_session_factory_for_tests(self, factory: Any) -> None:
        """TEST-ONLY fault-injection seam (see the service's twin)."""
        self._session_factory = factory

    # -- fresh readers ------------------------------------------------------

    def current_gate_issue(self) -> str | None:
        return self._current_gate_issue()

    def now(self) -> datetime:
        return self._clock()

    def owner_intent_for(
        self,
        mandate_id: int,
        claim_token: str,
    ) -> passive_protocol.ImmutablePassiveIntent | None:
        """READ-ONLY intent lookup for durable pending-owner refs.

        Returns ``None`` ONLY when the row is genuinely absent (no such
        mandate, wrong lane, claim-token mismatch, or no stored intent).
        Database errors PROPAGATE — a caller must treat an error as
        uncertainty, never as "no intent found".
        """
        with self._session_factory() as db:
            row = db.get(PassiveMandate, mandate_id)
            if row is None or row.lane != PASSIVE_LANE:
                return None
            if row.claim_token != claim_token:
                return None
            if not row.intent_json:
                return None
            return passive_protocol.intent_from_json(row.intent_json)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        """Persist reconciliation-incident evidence for an ownerless ref.

        fix-14 handshake: lets execution record a durable incident when a
        nonempty pending owner reference cannot be parsed/restored or an
        outcome write fails, WITHOUT inventing authority — a malformed or
        unauthenticated reference never mutates a mandate row. Raises on
        persistence failure (no swallowing); the REAL risk pause is the
        caller's responsibility regardless of this outcome.
        """
        from app.services.reconciliation_incident_service import (
            ReconciliationFailure,
            ReconciliationIncidentService,
        )

        safe_reference = str(reference or "")[:80]
        message = (
            f"{PASSIVE_LANE} unresolved reference {safe_reference!r}: "
            f"{reason}"
        )
        if broker_order_id:
            message += f"; broker order {broker_order_id}"
        with self._session_factory() as db:
            service = ReconciliationIncidentService(
                first_reminder_seconds=0.0,
            )
            service.record_failure(
                db,
                ReconciliationFailure(
                    source=PASSIVE_INCIDENT_SOURCE,
                    category="UNRESOLVED_PASSIVE_REFERENCE",
                    symbols=(PASSIVE_SYMBOL,),
                    message=message[:1000],
                    error_type="PassiveUnresolvedReference",
                ),
            )
            db.commit()

    # -- hook 1: execution ownership ---------------------------------------

    def begin_execution(
        self,
        ref: passive_protocol.PassiveAttemptRef,
        execution_token: str,
    ) -> passive_protocol.PassiveOwner | passive_protocol.PassiveRejection:
        ref_issue = ref.validate()
        if ref_issue is not None:
            return passive_protocol.PassiveRejection(reason=ref_issue)
        if not execution_token:
            return passive_protocol.PassiveRejection(
                reason="execution token is required to begin execution",
            )
        with self._session_factory() as db:
            row = db.get(PassiveMandate, ref.mandate_id)
            if row is None or row.lane != PASSIVE_LANE:
                return passive_protocol.PassiveRejection(
                    reason=(
                        f"no {PASSIVE_LANE} mandate row matches this attempt ref"
                    ),
                )
            if row.claim_token != ref.claim_token:
                return passive_protocol.PassiveRejection(
                    reason=(
                        "attempt ref claim token does not match the mandate; "
                        "another owner holds this reservation"
                    ),
                )
            if row.submit_state != passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED:
                return passive_protocol.PassiveRejection(
                    reason=(
                        f"{PASSIVE_LANE} authorisation state "
                        f"{row.submit_state!r} cannot begin execution"
                    ),
                )
            intent = _row_intent_or_issue(row)
            if isinstance(intent, str):
                return passive_protocol.PassiveRejection(reason=intent)
            owner = passive_protocol.PassiveOwner(
                ref=ref,
                execution_token=execution_token,
                intent=intent,
            )
            intent_issue = owner.intent.validate()
            if intent_issue is not None:
                return passive_protocol.PassiveRejection(reason=intent_issue)
            won = _cas_rowcount(
                db.execute(
                    update(PassiveMandate)
                    .where(
                        PassiveMandate.id == ref.mandate_id,
                        PassiveMandate.lane == PASSIVE_LANE,
                        PassiveMandate.claim_token == ref.claim_token,
                        PassiveMandate.submit_state
                        == passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED,
                    )
                    .values(
                        submit_state=passive_protocol.SUBMIT_STATE_CHECKING,
                        execution_token=execution_token,
                    ),
                )
            ) == 1
            if not won:
                db.rollback()
                return passive_protocol.PassiveRejection(
                    reason=(
                        f"{PASSIVE_LANE} execution ownership was lost to a "
                        "concurrent attempt"
                    ),
                )
            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                raise _CasCommitFailed(
                    f"execution-ownership CAS commit failed: {type(exc).__name__}",
                ) from exc
            return owner

    # -- hook 2: resolve policy (READ ONLY) ---------------------------------

    def resolve_policy(
        self,
        owner: passive_protocol.PassiveOwner,
        request: passive_protocol.PassiveOrderSpec,
    ) -> (
        passive_protocol.ValidatedPassiveIntent
        | passive_protocol.PassiveRejection
    ):
        owner_issue = owner.validate()
        if owner_issue is not None:
            return passive_protocol.PassiveRejection(reason=owner_issue)
        with self._session_factory() as db:
            row = db.get(PassiveMandate, owner.ref.mandate_id)
            if row is None or row.lane != PASSIVE_LANE:
                return passive_protocol.PassiveRejection(
                    reason=f"no {PASSIVE_LANE} mandate row backs this owner",
                )
            if row.claim_token != owner.ref.claim_token:
                return passive_protocol.PassiveRejection(
                    reason="owner claim token no longer matches the mandate row",
                )
            if row.execution_token != owner.execution_token:
                return passive_protocol.PassiveRejection(
                    reason=(
                        "owner execution token no longer matches the mandate "
                        "row; execution ownership was superseded"
                    ),
                )
            if row.submit_state != passive_protocol.SUBMIT_STATE_CHECKING:
                return passive_protocol.PassiveRejection(
                    reason=(
                        f"{PASSIVE_LANE} authorisation state "
                        f"{row.submit_state!r} is not executing"
                    ),
                )
            drift = owner.intent.matches_request(request)
            if drift is not None:
                return passive_protocol.PassiveRejection(reason=drift)
            if row.symbol.upper() != request.symbol.upper():
                return passive_protocol.PassiveRejection(
                    reason=(
                        f"mandate symbol {row.symbol!r} does not match the "
                        f"request symbol {request.symbol!r}"
                    ),
                )
            try:
                facts = _facts_from_row(row)
            except ValueError as exc:
                return passive_protocol.PassiveRejection(reason=str(exc))
            mandate_issue = passive_policy.validate_mandate_for_entry(facts)
            if mandate_issue is not None:
                return passive_protocol.PassiveRejection(reason=mandate_issue)
            stored = _row_intent_or_issue(row)
            if isinstance(stored, str):
                return passive_protocol.PassiveRejection(reason=stored)
            if (
                passive_protocol.intent_to_json(stored)
                != passive_protocol.intent_to_json(owner.intent)
            ):
                return passive_protocol.PassiveRejection(
                    reason=(
                        "stored immutable intent no longer matches the "
                        "owner intent"
                    ),
                )
            return passive_protocol.ValidatedPassiveIntent(
                owner=owner,
                order=request,
                allotment_usd=facts.allotment_usd,
            )

    # -- hook 3: submit right ------------------------------------------------

    def claim_submission(
        self,
        owner: passive_protocol.PassiveOwner,
        final_order: passive_protocol.PassiveOrderSpec,
        cash: passive_protocol.UsdCashEvidence,
    ) -> bool:
        owner_issue = owner.validate()
        if owner_issue is not None:
            return False
        # R1-6: the FINAL approved price legitimately differs from the
        # original request price; symbol/side/quantity remain immutable.
        drift = owner.intent.matches_final_order(final_order)
        if drift is not None:
            return False
        intent_json = passive_protocol.intent_to_json(owner.intent)
        exemptions_csv = ",".join(owner.intent.policy.exemptions)
        with self._session_factory() as db:
            row = db.get(PassiveMandate, owner.ref.mandate_id)
            if row is None or row.lane != PASSIVE_LANE:
                return False
            if row.claim_token != owner.ref.claim_token:
                return False
            if row.execution_token != owner.execution_token:
                return False
            if row.submit_state != passive_protocol.SUBMIT_STATE_CHECKING:
                return False
            if row.status != "ACTIVE":
                return False
            if row.intent_json != intent_json:
                return False
            won = _cas_rowcount(
                db.execute(
                    update(PassiveMandate)
                    .where(
                        PassiveMandate.id == owner.ref.mandate_id,
                        PassiveMandate.lane == PASSIVE_LANE,
                        PassiveMandate.claim_token == owner.ref.claim_token,
                        PassiveMandate.execution_token == owner.execution_token,
                        PassiveMandate.status == "ACTIVE",
                        PassiveMandate.submit_state
                        == passive_protocol.SUBMIT_STATE_CHECKING,
                        PassiveMandate.intent_json == intent_json,
                        # R1-2: the mandate POLICY must be unchanged since
                        # the reservation snapshot — enforced INSIDE the
                        # conditional UPDATE, not only a prior SELECT, so a
                        # drift committed between the boundary read and
                        # this CAS cannot submit.
                        PassiveMandate.symbol == owner.intent.symbol,
                        PassiveMandate.policy_version
                        == owner.intent.policy.policy_version,
                        PassiveMandate.risk_model
                        == owner.intent.policy.risk_model,
                        PassiveMandate.order_binding
                        == owner.intent.policy.order_binding,
                        PassiveMandate.review_interval_months
                        == owner.intent.policy.review_interval_months,
                        PassiveMandate.exemptions == exemptions_csv,
                        PassiveMandate.allotment_usd == float(
                            owner.intent.policy.allotment_usd
                        ),
                    )
                    .values(
                        submit_state=passive_protocol.SUBMIT_STATE_SUBMITTING,
                        final_snapshot_json=(
                            passive_protocol.final_snapshot_to_json(
                                quantity=final_order.quantity,
                                approved_price=final_order.price,
                                fee=us_paper_commission(
                                    final_order.price, final_order.quantity,
                                ),
                                cash=cash,
                            )
                        ),
                    ),
                )
            ) == 1
            if not won:
                db.rollback()
                return False
            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                raise _CasCommitFailed(
                    f"submit-right CAS commit failed: {type(exc).__name__}",
                ) from exc
            return True

    # -- hook 4: outcome ------------------------------------------------------

    def record_outcome(
        self,
        owner: passive_protocol.PassiveOwner,
        fact: passive_protocol.PassiveOutcomeFact,
    ) -> None:
        owner_issue = owner.validate()
        if owner_issue is not None:
            raise ValueError(
                f"refusing to record an outcome for an invalid owner: {owner_issue}",
            )
        with self._session_factory() as db:
            row = db.get(PassiveMandate, owner.ref.mandate_id)
            if row is None or row.lane != PASSIVE_LANE:
                raise ValueError(
                    "refusing to record an outcome: no mandate row matches "
                    "the owner tokens",
                )
            if row.claim_token != owner.ref.claim_token:
                raise ValueError(
                    "refusing to record an outcome: owner tokens do not "
                    "match the mandate row",
                )
            # R1-1: EVERY outcome branch — including terminal and conflict
            # branches — conditions on the owner's execution token. A wrong
            # token (even with the right claim token) performs ZERO writes.
            if row.execution_token != owner.execution_token:
                raise ValueError(
                    "refusing to record an outcome: execution token does "
                    "not match this owner (the row belongs to another "
                    "execution or a later token)",
                )
            prior_state = row.submit_state
            prior_id = row.bound_broker_order_id
            prior_status = row.bound_broker_status
            prior_qty = row.bound_executed_quantity
            prior_price = row.bound_executed_price
            verdict = passive_protocol.validate_outcome_write(
                current_state=prior_state,
                bound_broker_order_id=prior_id,
                fact=fact,
                bound_broker_status=prior_status,
                bound_executed_quantity=prior_qty,
                bound_executed_price=prior_price,
                # B5: the cumulative fill can never exceed the immutable
                # owner intent quantity — enforced at the service where the
                # owner (and therefore the intent) is authenticated.
                intent_quantity=owner.intent.quantity,
            )
            if verdict == "IDEMPOTENT":
                db.rollback()
                return
            # Shared ownership/token predicate for every mutating branch:
            # both owner tokens + the exact old state (null-safe).
            ownership = (
                PassiveMandate.id == row.id,
                PassiveMandate.claim_token == owner.ref.claim_token,
                PassiveMandate.execution_token == owner.execution_token,
                PassiveMandate.submit_state == prior_state,
            )
            if verdict == "CONFLICT":
                # Contradictory/stale/backwards fact: escalate to UNCERTAIN
                # and RECORD the contradiction — every recorded fact and the
                # bound id are preserved (never erased or replaced). The
                # UPDATE re-checks the old receipt facts so a concurrent
                # writer cannot be silently clobbered.
                updated = _cas_rowcount(
                    db.execute(
                        update(PassiveMandate)
                        .where(
                            *ownership,
                            PassiveMandate.bound_broker_order_id.is_(prior_id),
                            PassiveMandate.bound_broker_status.is_(prior_status),
                            PassiveMandate.bound_executed_quantity.is_(
                                prior_qty,
                            ),
                            PassiveMandate.bound_executed_price.is_(prior_price),
                        )
                        .values(
                            submit_state=(
                                passive_protocol.SUBMIT_STATE_UNCERTAIN
                            ),
                            uncertainty_reason=(
                                f"conflicting outcome fact {fact.outcome} "
                                f"(broker id {fact.broker_order_id!r}, "
                                f"status {fact.broker_status!r}, fills "
                                f"{fact.executed_quantity!r}@"
                                f"{fact.executed_price!r}) against recorded "
                                f"{prior_status!r} "
                                f"{prior_qty!r}@{prior_price!r} from state "
                                f"{prior_state}; bound id "
                                f"{prior_id!r} retained"
                            )[:500],
                        ),
                    )
                )
                if updated != 1:
                    db.rollback()
                    raise ValueError(
                        "conflict escalation lost the fact race; outcome is "
                        "uncertain and was NOT recorded"
                    )
                db.commit()
                return
            if verdict == "PROGRESS":
                # Genuine FORWARD same-id progress: CAS against the exact
                # prior receipt facts the decision was made on, so a stale
                # concurrent writer cannot be silently clobbered. A 0-row
                # result means the row advanced concurrently — RE-DECIDE on
                # fresh facts inside the same transaction: a still-forward
                # fact is applied against the current facts, a backward one
                # escalates to CONFLICT handling. Never drop a legitimate
                # forward fact to a smaller concurrent fill.
                def _progress_update(
                    exp_status: str | None,
                    exp_qty: Decimal | None,
                    exp_price: Decimal | None,
                ) -> int:
                    return _cas_rowcount(
                        db.execute(
                            update(PassiveMandate)
                            .where(
                                *ownership,
                                PassiveMandate.bound_broker_order_id
                                == fact.broker_order_id,
                                PassiveMandate.bound_broker_status.is_(
                                    exp_status,
                                ),
                                PassiveMandate.bound_executed_quantity.is_(
                                    exp_qty,
                                ),
                                PassiveMandate.bound_executed_price.is_(
                                    exp_price,
                                ),
                            )
                            .values(
                                bound_broker_status=fact.broker_status or None,
                                bound_executed_quantity=(
                                    fact.executed_quantity
                                    if fact.executed_quantity is not None
                                    else exp_qty
                                ),
                                bound_executed_price=(
                                    fact.executed_price
                                    if fact.executed_price is not None
                                    else exp_price
                                ),
                            ),
                        )
                    )

                updated = _progress_update(prior_status, prior_qty, prior_price)
                if updated != 1:
                    fresh = db.get(PassiveMandate, row.id)
                    if fresh is None:
                        db.rollback()
                        raise ValueError(
                            "progress write lost the row; outcome is "
                            "uncertain and was NOT recorded"
                        )
                    db.rollback()
                    fresh_comparison = (
                        passive_protocol.compare_receipt_facts(
                            fact=fact,
                            bound_broker_status=fresh.bound_broker_status,
                            bound_executed_quantity=(
                                fresh.bound_executed_quantity
                            ),
                            bound_executed_price=fresh.bound_executed_price,
                        )
                    )
                    if fresh_comparison == "EXACT":
                        return  # another writer recorded the same facts
                    if fresh_comparison == "FORWARD":
                        updated = _progress_update(
                            fresh.bound_broker_status,
                            fresh.bound_executed_quantity,
                            fresh.bound_executed_price,
                        )
                        if updated != 1:
                            db.rollback()
                            raise ValueError(
                                "progress write lost the expected-prior-fact "
                                "race twice; outcome is uncertain and was "
                                "NOT recorded"
                            )
                        db.commit()
                        return
                    # BACKWARD against fresh facts: fall through to the
                    # conflict escalation below with the FRESH facts.
                    conflict_values = {
                        "submit_state": (
                            passive_protocol.SUBMIT_STATE_UNCERTAIN
                        ),
                        "uncertainty_reason": (
                            f"conflicting outcome fact {fact.outcome} "
                            f"(broker id {fact.broker_order_id!r}, "
                            f"status {fact.broker_status!r}, fills "
                            f"{fact.executed_quantity!r}@"
                            f"{fact.executed_price!r}) against recorded "
                            f"{fresh.bound_broker_status!r} "
                            f"{fresh.bound_executed_quantity!r}@"
                            f"{fresh.bound_executed_price!r} from state "
                            f"{fresh.submit_state}; bound id "
                            f"{fresh.bound_broker_order_id!r} retained"
                        )[:500],
                    }
                    updated = _cas_rowcount(
                        db.execute(
                            update(PassiveMandate)
                            .where(
                                PassiveMandate.id == fresh.id,
                                PassiveMandate.claim_token
                                == owner.ref.claim_token,
                                PassiveMandate.execution_token
                                == owner.execution_token,
                                PassiveMandate.submit_state
                                == fresh.submit_state,
                                PassiveMandate.bound_broker_order_id.is_(
                                    fresh.bound_broker_order_id,
                                ),
                                PassiveMandate.bound_broker_status.is_(
                                    fresh.bound_broker_status,
                                ),
                                PassiveMandate.bound_executed_quantity.is_(
                                    fresh.bound_executed_quantity,
                                ),
                                PassiveMandate.bound_executed_price.is_(
                                    fresh.bound_executed_price,
                                ),
                            )
                            .values(**conflict_values),
                        )
                    )
                    if updated != 1:
                        db.rollback()
                        raise ValueError(
                            "conflict escalation lost the fact race; "
                            "outcome is uncertain and was NOT recorded"
                        )
                    db.commit()
                    return
                db.commit()
                return
            if verdict is not None:
                raise ValueError(verdict)
            values: dict[str, object] = {
                "submit_state": fact.outcome,
            }
            if fact.outcome == passive_protocol.SUBMIT_STATE_ORDER_KNOWN:
                values["bound_broker_order_id"] = fact.broker_order_id
                values["bound_broker_status"] = fact.broker_status or None
                values["bound_executed_quantity"] = fact.executed_quantity
                values["bound_executed_price"] = fact.executed_price
                values["failure_reason"] = None
            elif fact.outcome == passive_protocol.SUBMIT_STATE_NO_SUBMIT:
                values["failure_reason"] = (fact.reason or "no submit")[:500]
            else:
                values["uncertainty_reason"] = (
                    fact.reason or fact.outcome
                )[:500]
                # UNCERTAIN with a known id: PRESERVE the already-bound id,
                # never overwrite it with a different one (R1-1d).
                if (
                    fact.broker_order_id
                    and not row.bound_broker_order_id
                ):
                    values["bound_broker_order_id"] = fact.broker_order_id
                    values["bound_broker_status"] = fact.broker_status or None
                    values["bound_executed_quantity"] = fact.executed_quantity
                    values["bound_executed_price"] = fact.executed_price
            updated = _cas_rowcount(
                db.execute(
                    update(PassiveMandate)
                    .where(
                        PassiveMandate.id == row.id,
                        PassiveMandate.submit_state == prior_state,
                        PassiveMandate.claim_token == owner.ref.claim_token,
                        PassiveMandate.execution_token == owner.execution_token,
                    )
                    .values(**values),
                )
            )
            if updated != 1:
                db.rollback()
                raise ValueError(
                    "outcome write lost the state race; outcome is uncertain"
                )
            db.commit()


# ---------------------------------------------------------------------------
# Outcome facade
# ---------------------------------------------------------------------------


class PassiveEntryOutcome:
    """Result of one passive execution attempt (values only).

    ``uncertain`` means a broker order may exist but the mandate binding
    could not be proven; the system is paused for manual reconciliation and
    the caller must NOT treat the entry as done.
    """

    __slots__ = ("submitted", "reason", "status", "intent", "uncertain")

    def __init__(
        self,
        *,
        submitted: bool,
        reason: str = "",
        status: OrderStatus | None = None,
        intent: Any | None = None,
        uncertain: bool = False,
    ) -> None:
        self.submitted = submitted
        self.reason = reason
        self.status = status
        self.intent = intent
        self.uncertain = uncertain


# ---------------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------------


class PassiveAllocationService:
    """Reservation + execution orchestration over the existing entry path.

    ``execution`` is the runner-lifetime ``TradeExecutionService`` instance.
    The execution layer owns the submit right and calls back into the SAME
    bundle it was wired with (``passive_submit_hooks``), so the service and
    the boundary always agree on one protocol instance.
    """

    def __init__(
        self,
        *,
        execution: Any,
        session_factory: sessionmaker[Session],
        lane_enabled_reader: Callable[[], bool],
        paper_account_confirmed_reader: Callable[[], bool],
        clock: Clock | None = None,
    ) -> None:
        self._execution = execution
        self._session_factory = session_factory
        self._lane_enabled_reader = lane_enabled_reader
        self._paper_reader = paper_account_confirmed_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Reduces same-process contention ONLY. Correctness (exactly one
        # submit per authorisation, even across processes or restarts) rests
        # on the database CAS transitions.
        self._claim_gate = threading.RLock()

    # -- gate helpers ---------------------------------------------------------

    def _gate_issue(self) -> str | None:
        if not self._lane_enabled_reader():
            return (
                "SPY passive lane is disabled "
                "(AUTO_TRADE_SPY_PASSIVE_ENABLED=false)"
            )
        if not self._paper_reader():
            return (
                "SPY passive lane requires a confirmed PAPER account "
                "(AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED=true)"
            )
        return None

    def hooks(self) -> PassiveSubmitHookBundle:
        bundle = getattr(self._execution, "passive_submit_hooks", None)
        if bundle is None or not passive_protocol.passive_hooks_complete(
            bundle,
        ):
            raise RuntimeError(
                "passive submit hooks are not fully wired to the execution "
                "service; passive entries are refused",
            )
        return bundle

    def build_hook_bundle(self) -> PassiveSubmitHookBundle:
        """Construct the bundle the runner/tests wire into the executor."""
        return PassiveSubmitHookBundle(
            self._session_factory,
            current_gate_issue=self._gate_issue,
            clock=self._clock,
        )

    def inject_session_factory_for_tests(
        self,
        factory: Any,
    ) -> None:
        """TEST-ONLY fault injection seam for durable-write failures.

        Replaces the session factory this service (and newly built hook
        bundles) use, so tests can fault UPDATEs/COMMITs. Production code
        never calls this; the attribute stays private precisely because
        tests need a documented seam instead of ad-hoc private patching.
        """
        self._session_factory = factory  # type: ignore[assignment]
        self._injected_test_factory = factory

    # -- sizing --------------------------------------------------------------

    def _sizing_caps(self) -> tuple[Decimal, Decimal]:
        max_quantity = self._execution.max_position_quantity
        max_notional = self._execution.max_position_notional
        quantity_cap = (
            Decimal(max_quantity)
            if isinstance(max_quantity, int) and max_quantity > 0
            else Decimal("100")
        )
        notional_cap = (
            Decimal(str(max_notional))
            if isinstance(max_notional, (int, float)) and max_notional > 0
            else Decimal("5000")
        )
        return quantity_cap, notional_cap

    def size_intent(
        self,
        *,
        price: Decimal,
        allotment_usd: Decimal,
    ) -> PassiveEntrySizing:
        quantity_cap, notional_cap = self._sizing_caps()
        return passive_policy.size_passive_entry(
            price=price,
            allotment_usd=allotment_usd,
            max_quantity=quantity_cap,
            max_notional=notional_cap,
            commission=us_paper_commission,
        )

    # -- reservation ----------------------------------------------------------

    def reserve_entry(
        self,
        *,
        price: Decimal,
    ) -> passive_protocol.PassiveAttemptRef | str:
        """CAS-reserve the mandate's authorisation; returns the ref or reason.

        The reservation commits the immutable intent snapshot (symbol, BUY,
        integer quantity from the allotment sizing, original price and
        policy snapshot). Failures detected BEFORE the CAS (invalid price,
        gate off, invalid mandate, duplicate rows) leave the row AUTHORIZED
        — the authorisation is only burned by the CAS itself.

        fix-14 finding 5: an invalid-sizing burn whose durable write cannot
        be proven (0-row UPDATE or failed COMMIT) raises
        ``PassivePersistenceUncertain`` — NEVER a normal refusal string
        claiming the row consumed — and blocks automatic retry on this
        service instance until reviewed. A total DB outage cannot prove
        durability here or across a restart; the row is honestly left
        unclaimed-consumed in that case.
        """
        if price is None or not price.is_finite() or price <= 0:
            return "passive entry price must be finite and greater than zero"
        # Hook completeness is a FRESH read off the executor (never a
        # cached constructor boolean). An incomplete bundle must refuse
        # BEFORE any write: the dedicated entry could not finalise the
        # attempt, so creating a reservation would strand a dead
        # SUBMIT_CLAIMED row. The row stays clearly AUTHORIZED.
        wired = getattr(self._execution, "passive_submit_hooks", None)
        if not passive_protocol.passive_hooks_complete(wired):
            return (
                "passive submit hooks are not fully wired to the execution "
                "service; refusing to create a reservation (no writes "
                "performed)"
            )
        gate = self._gate_issue()
        if gate is not None:
            return gate
        # Review gate: after an unproven durable write, automatic retry on
        # THIS instance is blocked until a human review clears it.
        review = getattr(self, "_review_required_table", None) or {}
        if review:
            blocked = "; ".join(
                f"mandate {mid}: {detail[:80]}" for mid, detail in review.items()
            )
            return (
                f"{PASSIVE_LANE} reservation is blocked pending review of "
                f"unproven durable writes ({blocked})"
            )
        with self._claim_gate:
            with self._session_factory() as db:
                rows = (
                    db.query(PassiveMandate)
                    .filter(PassiveMandate.lane == PASSIVE_LANE)
                    .all()
                )
                if not rows:
                    return f"no approved {PASSIVE_LANE} mandate exists"
                if len(rows) > 1:
                    return (
                        f"duplicate {PASSIVE_LANE} mandate rows exist; "
                        "refusing to choose one"
                    )
                row = rows[0]
                try:
                    facts = _facts_from_row(row)
                except ValueError as exc:
                    return str(exc)
                issue = passive_policy.validate_mandate_for_entry(facts)
                if issue is not None:
                    return issue
                # R1-7: revalidate the WHOLE unconsumed state — a v2 stamp
                # must never mask a contradictory consumed row. Rows in a
                # LIVE protocol state (SUBMIT_CLAIMED/CHECKING/SUBMITTING or
                # terminal) are simply unavailable — refused without ANY
                # write, because another execution may legitimately own
                # them (a racing reservation must never mark a winner's
                # in-flight row uncertain).
                row_is_authorized = (
                    row.submit_state
                    == passive_protocol.SUBMIT_STATE_AUTHORIZED
                )
                clear_issue = passive_protocol.legacy_row_may_remain_authorized(
                    submit_state=row.submit_state,
                    entry_authorisation_available=(
                        row.entry_authorisation_available
                    ),
                    claim_token=row.claim_token,
                    entry_authorisation_consumed_at=(
                        row.entry_authorisation_consumed_at
                    ),
                    bound_broker_order_id=row.bound_broker_order_id,
                    execution_token=row.execution_token,
                    intent_json=row.intent_json,
                    final_snapshot_json=row.final_snapshot_json,
                    uncertainty_reason=row.uncertainty_reason,
                    status=row.status,
                    policy_version=row.policy_version,
                    allotment_usd=row.allotment_usd,
                )
                if clear_issue is not None:
                    if row_is_authorized:
                        # Contradictory AUTHORIZED row (used markers on an
                        # unclaimed state): fail closed to UNCERTAIN with
                        # all facts retained; never reserve.
                        self._fail_closed_uncertain(db, row, clear_issue)
                    return (
                        f"{PASSIVE_LANE} one-time entry authorisation is "
                        f"not available ({clear_issue})"
                    )
                sizing = self.size_intent(
                    price=price, allotment_usd=facts.allotment_usd,
                )
                if not sizing.fits:
                    # R1-8: an authorized attempt that cannot form an intent
                    # atomically burns to NO_SUBMIT — the authorisation is
                    # consumed, never left reusable for a cheaper price.
                    self._burn_authorized_no_submit(
                        db, row,
                        (
                            "passive allotment cannot buy a single share at "
                            f"the requested price {price}; authorisation "
                            "consumed by the failed sizing attempt"
                        ),
                    )
                    return (
                        "passive allotment cannot buy a single share at the "
                        "current price; authorisation consumed (NO_SUBMIT)"
                    )
                claim_token = secrets.token_hex(16)
                intent = passive_protocol.ImmutablePassiveIntent(
                    symbol=PASSIVE_SYMBOL,
                    side="BUY",
                    quantity=Decimal(sizing.quantity),
                    original_price=price,
                    policy=_policy_snapshot_from_row(facts),
                )
                won = _cas_rowcount(
                    db.execute(
                        update(PassiveMandate)
                        .where(
                            PassiveMandate.id == row.id,
                            PassiveMandate.lane == PASSIVE_LANE,
                            PassiveMandate.submit_state
                            == passive_protocol.SUBMIT_STATE_AUTHORIZED,
                            PassiveMandate.entry_authorisation_available.is_(True),
                            PassiveMandate.claim_token.is_(None),
                            PassiveMandate.entry_authorisation_consumed_at.is_(None),
                            PassiveMandate.bound_broker_order_id.is_(None),
                            PassiveMandate.intent_json.is_(None),
                            PassiveMandate.execution_token.is_(None),
                            PassiveMandate.final_snapshot_json.is_(None),
                        )
                        .values(
                            submit_state=(
                                passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
                            ),
                            entry_authorisation_available=False,
                            claim_token=claim_token,
                            entry_authorisation_consumed_at=self._clock(),
                            intent_json=passive_protocol.intent_to_json(intent),
                            execution_token=None,
                            final_snapshot_json=None,
                            uncertainty_reason=None,
                            protocol_version=(
                                passive_protocol.PASSIVE_PROTOCOL_VERSION
                            ),
                        ),
                    )
                ) == 1
                if not won:
                    db.rollback()
                    return (
                        f"{PASSIVE_LANE} one-time entry authorisation could "
                        "not be claimed; it may already be in use"
                    )
                try:
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    raise _CasCommitFailed(
                        f"reservation CAS commit failed: {type(exc).__name__}",
                    ) from exc
                return passive_protocol.PassiveAttemptRef(
                    mandate_id=row.id,
                    claim_token=claim_token,
                )

    # -- execution ------------------------------------------------------------

    def execute_reservation(
        self,
        ref: passive_protocol.PassiveAttemptRef,
        *,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: NotifierInterface,
    ) -> PassiveEntryOutcome:
        """Delegate one reserved intent to the DEDICATED executor entry.

        fix-14 handshake: the execution layer owns the complete lifecycle —
        it calls ``begin_execution`` exactly once, captures strict cash
        itself before the submission/state lock, drives the single
        boundary + sole broker mutation, and finalises EVERY outcome
        (NO_SUBMIT / ORDER_KNOWN / UNCERTAIN with real risk pause and
        incident). This facade ONLY delegates ``ref`` plus the actual
        collaborators and maps the returned ``OrderStatus`` to a
        ``PassiveEntryOutcome``; it must not begin ownership, fetch cash,
        perform compensating outcome updates, or short-circuit on the lane
        gate — a reserved ref must ALWAYS reach the executor, which wins
        ownership and performs the owned NO_SUBMIT burn when the gate has
        been revoked (never a facade no-write skip that would leave the
        reservation replayable).
        """
        try:
            status = self._execution.execute_passive_entry(
                ref=ref,
                quote=quote,
                broker=broker,
                risk=risk,
                notifier=notifier,
            )
        except PassivePersistenceUncertain as exc:
            return PassiveEntryOutcome(
                submitted=False,
                reason=str(exc),
                uncertain=True,
            )
        return self._map_status(status)

    @staticmethod
    def _map_status(status: OrderStatus | None) -> PassiveEntryOutcome:
        """Map the executor's classified OrderStatus to the facade result.

        Classification/uncertainty decisions live in the executor; this is
        a pure value mapping only.
        """
        if status is None:
            return PassiveEntryOutcome(
                submitted=False,
                reason="execution returned no classifiable status",
                uncertain=True,
            )
        status_text = str(status.status or "")
        reason_text = str(status.reason or "")
        uncertain = status_text == "UNCERTAIN" or reason_text.startswith(
            _UNCERTAIN_REASON_PREFIXES,
        )
        return PassiveEntryOutcome(
            submitted=status_text in {
                "SUBMITTED", "PARTIAL_FILLED", "FILLED",
            } and not uncertain,
            reason=reason_text,
            status=status,
            uncertain=uncertain,
        )

    def _burn_no_submit(
        self,
        ref: passive_protocol.PassiveAttemptRef,
        reason: str,
    ) -> None:
        """NO_SUBMIT for a reservation whose execution could not start.

        R1-1: this is only reachable for a row THIS service still owns. The
        conditional UPDATE requires SUBMIT_CLAIMED with no execution token
        (our pre-begin state) — a CHECKING row owned by another execution
        (or any other state) is never mutated by a CAS loser.

        fix-14 finding 5: a 0-row result on a row that still reads
        SUBMIT_CLAIMED-with-our-token, or a failed commit, raises
        ``PassivePersistenceUncertain`` instead of being swallowed into a
        normal refusal.
        """
        with self._session_factory() as db:
            try:
                updated = _cas_rowcount(
                    db.execute(
                        update(PassiveMandate)
                        .where(
                            PassiveMandate.id == ref.mandate_id,
                            PassiveMandate.lane == PASSIVE_LANE,
                            PassiveMandate.claim_token == ref.claim_token,
                            PassiveMandate.submit_state == (
                                passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
                            ),
                            PassiveMandate.execution_token.is_(None),
                        )
                        .values(
                            submit_state=(
                                passive_protocol.SUBMIT_STATE_NO_SUBMIT
                            ),
                            failure_reason=reason[:500],
                        ),
                    )
                )
            except PassivePersistenceUncertain:
                raise
            except Exception as exc:
                # B4: the denial-burn UPDATE itself raised.
                try:
                    db.rollback()
                except Exception:
                    logger.exception(
                        "rollback failed after a %s denial-burn execute "
                        "failure", PASSIVE_LANE,
                    )
                self._mark_review_required(
                    ref.mandate_id,
                    f"denial burn UPDATE raised {type(exc).__name__}",
                )
                raise PassivePersistenceUncertain(
                    "denial burn",
                    f"UPDATE raised {type(exc).__name__}",
                ) from exc
            if updated != 1:
                try:
                    db.rollback()
                except Exception:
                    logger.exception(
                        "rollback failed after a 0-row %s denial burn",
                        PASSIVE_LANE,
                    )
                # Legitimate losses (row advanced by the winner / another
                # state) are silent — only a still-claimable row is a
                # durability fault. The re-read itself may raise during a
                # DB outage: that is also a durability fault (typed), not
                # a silent clean return.
                try:
                    row = db.get(PassiveMandate, ref.mandate_id)
                except Exception as exc:
                    self._mark_review_required(
                        ref.mandate_id,
                        f"denial burn post-0-row re-read raised "
                        f"{type(exc).__name__}",
                    )
                    raise PassivePersistenceUncertain(
                        "denial burn",
                        f"0-row re-check read raised {type(exc).__name__}",
                    ) from exc
                if (
                    row is not None
                    and row.claim_token == ref.claim_token
                    and row.submit_state == (
                        passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
                    )
                    and row.execution_token is None
                ):
                    self._mark_review_required(
                        ref.mandate_id,
                        "denial burn UPDATE matched 0 rows unexpectedly",
                    )
                    raise PassivePersistenceUncertain(
                        "denial burn",
                        "UPDATE matched 0 rows while the row still reads "
                        "SUBMIT_CLAIMED unowned",
                    )
                return
            try:
                db.commit()
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    logger.exception(
                        "rollback failed after a %s denial-burn commit "
                        "failure", PASSIVE_LANE,
                    )
                self._mark_review_required(
                    ref.mandate_id,
                    f"denial burn commit failed: {type(exc).__name__}",
                )
                raise PassivePersistenceUncertain(
                    "denial burn",
                    f"commit failed ({type(exc).__name__})",
                ) from exc

    def _fail_closed_uncertain(
        self,
        db: Session,
        row: PassiveMandate,
        issue: str,
    ) -> None:
        """Contradictory row: UNCERTAIN with all facts retained (R1-7).

        fix-14 finding 5: a failed quarantine write PROPAGATES as
        ``PassivePersistenceUncertain`` — the caller must not read the row
        as safely handled when it was not.
        """
        try:
            updated = _cas_rowcount(
                db.execute(
                    update(PassiveMandate)
                    .where(
                        PassiveMandate.id == row.id,
                        PassiveMandate.submit_state == row.submit_state,
                    )
                    .values(
                        submit_state=(
                            passive_protocol.SUBMIT_STATE_UNCERTAIN
                        ),
                        uncertainty_reason=(
                            f"reservation fail-closed: {issue}"
                        )[:500],
                        protocol_version=(
                            passive_protocol.PASSIVE_PROTOCOL_VERSION
                        ),
                    ),
                )
            )
        except PassivePersistenceUncertain:
            raise
        except Exception as exc:
            # B4: the quarantine UPDATE itself raised.
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a %s quarantine execute failure",
                    PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id,
                f"contradictory-row quarantine UPDATE raised "
                f"{type(exc).__name__}",
            )
            raise PassivePersistenceUncertain(
                "contradictory-row quarantine",
                f"UPDATE raised {type(exc).__name__}",
            ) from exc
        if updated != 1:
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a 0-row %s quarantine",
                    PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id, "contradictory-row quarantine UPDATE matched no row",
            )
            raise PassivePersistenceUncertain(
                "contradictory-row quarantine",
                "UPDATE matched 0 rows",
            )
        try:
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a %s quarantine commit failure",
                    PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id,
                f"contradictory-row quarantine commit failed: "
                f"{type(exc).__name__}",
            )
            raise PassivePersistenceUncertain(
                "contradictory-row quarantine",
                f"commit failed ({type(exc).__name__})",
            ) from exc

    def _burn_authorized_no_submit(
        self,
        db: Session,
        row: PassiveMandate,
        reason: str,
    ) -> None:
        """R1-8/fix-14-5: invalid sizing under a valid mandate atomically
        burns.

        Applies only to a row still provably AUTHORIZED-and-clear; the CAS
        predicate keeps it atomic against concurrent reservations. Fix-14
        finding 5: a 0-row UPDATE or a failed COMMIT is NEVER reported as a
        normal consumed refusal — the caller receives
        ``PassivePersistenceUncertain`` so the row is never claimed
        consumed while still AUTHORIZED. Automatic in-process retry is
        blocked until review; incident evidence is attempted best-effort.
        A TOTAL database outage cannot prove durability here or across a
        restart — in that case the row is honestly left as-is (uncertain),
        never faked as consumed.
        """
        try:
            updated = _cas_rowcount(
                db.execute(
                    update(PassiveMandate)
                    .where(
                        PassiveMandate.id == row.id,
                        PassiveMandate.lane == PASSIVE_LANE,
                        PassiveMandate.submit_state == (
                            passive_protocol.SUBMIT_STATE_AUTHORIZED
                        ),
                        PassiveMandate.entry_authorisation_available.is_(True),
                        PassiveMandate.claim_token.is_(None),
                        PassiveMandate.entry_authorisation_consumed_at.is_(None),
                    )
                    .values(
                        submit_state=(
                            passive_protocol.SUBMIT_STATE_NO_SUBMIT
                        ),
                        entry_authorisation_available=False,
                        entry_authorisation_consumed_at=self._clock(),
                        failure_reason=reason[:500],
                        protocol_version=(
                            passive_protocol.PASSIVE_PROTOCOL_VERSION
                        ),
                    ),
                )
            )
        except PassivePersistenceUncertain:
            raise
        except Exception as exc:
            # B4: the UPDATE itself raised (SQL/session failure) — safe
            # rollback first; secondary rollback/incident failures must
            # never mask the primary uncertainty.
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a %s burn execute failure",
                    PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id,
                f"invalid-sizing burn UPDATE raised "
                f"{type(exc).__name__}",
            )
            raise PassivePersistenceUncertain(
                "invalid-sizing burn",
                f"UPDATE raised {type(exc).__name__}",
            ) from exc
        if updated != 1:
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a 0-row %s burn", PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id,
                f"invalid-sizing burn UPDATE matched no AUTHORIZED row",
            )
            raise PassivePersistenceUncertain(
                "invalid-sizing burn",
                "UPDATE matched 0 rows while the row reads AUTHORIZED",
            )
        try:
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                logger.exception(
                    "rollback failed after a %s burn commit failure",
                    PASSIVE_LANE,
                )
            self._mark_review_required(
                row.id,
                f"invalid-sizing burn commit failed: {type(exc).__name__}",
            )
            raise PassivePersistenceUncertain(
                "invalid-sizing burn",
                f"commit failed ({type(exc).__name__})",
            ) from exc

    def _mark_review_required(
        self,
        mandate_id: int,
        detail: str,
    ) -> None:
        """Block automatic in-process retry until a human review.

        Sets a durable fault marker on the bundle instance; subsequent
        ``reserve_entry`` calls on THIS bundle refuse with an explicit
        review-required error (the row's durable state is untouched by
        this marker — it gates retries, never re-authorises). Best-effort
        incident evidence is attempted; its failure does not clear the
        gate.
        """
        with self._review_lock:
            self._review_required[mandate_id] = detail[:500]
        try:
            hooks = self.build_hook_bundle()
            hooks.record_unresolved_reference(
                f"mandate:{mandate_id}",
                f"reservation burn durability unproven: {detail}",
            )
        except Exception:
            logger.exception(
                "failed to record the %s burn-uncertainty incident",
                PASSIVE_LANE,
            )

    @property
    def _review_lock(self) -> threading.Lock:
        lock = getattr(self, "_review_lock_instance", None)
        if lock is None:
            lock = threading.Lock()
            self._review_lock_instance = lock
        return lock

    @property
    def _review_required(self) -> dict[int, str]:
        table = getattr(self, "_review_required_table", None)
        if table is None:
            table = {}
            self._review_required_table = table
        return table

