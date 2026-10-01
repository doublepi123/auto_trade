"""Passive mandate OFF-recovery service (Phase2a, writer W1).

Read-only inventory + conservative classification + the two provable
CAS burns. NEVER creates an authorization, NEVER submits/cancels/modifies
a broker order, NEVER adopts positions heuristically: an unprovable fact
is always HARD (quarantine + incident), never a guess.

Parent reconciliations implemented here (binding over the architect
draft):

1. A MISSING ``passive_mandates`` table or an unreadable DB under the
   installed schema is ``read_error``/HARD — never "no rows". Only a
   successful query returning zero rows is a CLEAR empty inventory, and
   it performs NO external callbacks and NO broker reads.
2. Inventory contradictions are validated BEFORE treating
   AUTHORIZED/NO_SUBMIT as clear, using the explicit typed
   available/consumed facts, with raw evidence preserved in reasons.
3. CAS burns (SUBMIT_CLAIMED-unowned / CHECKING) are executed against the
   EXACT id/claim/state/token predicates; a zero-row result re-reads and
   reclassifies exactly ONCE and never clobbers a concurrent winner.
4. Write failures become HARD + an incident attempt; the service can
   never return "cleared" for a row whose durable state is unproven.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.passive_allocation import recovery as recovery_types
from app.domain.passive_allocation import protocol as passive_protocol
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
)
from app.models import PassiveMandate

if TYPE_CHECKING:
    pass

logger = logging.getLogger("auto_trade.services.passive_recovery_service")

RECOVERY_INCIDENT_SOURCE = "passive_recovery"
RECOVERY_INCIDENT_CATEGORY = "PASSIVE_RECOVERY"


class PassiveRecoveryError(RuntimeError):
    """A recovery write or read failed; the outcome is HARD, never clear."""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class PassiveRecoverySnapshot:
    """Immutable-ish result of one recovery pass (values only).

    ``pending_refs`` is a MappingProxyType keyed by broker order id with
    the complete owner reference value — installed only by the RUNNER
    after authenticating the local order (W3), never by this service.
    """

    __slots__ = (
        "hard_reasons", "order_live", "quarantined_symbols",
        "pending_refs", "decisions", "complete",
    )

    def __init__(
        self,
        *,
        hard_reasons: tuple[str, ...],
        order_live: bool,
        quarantined_symbols: frozenset[str],
        pending_refs: dict[str, str] | MappingProxyType[str, str],
        decisions: tuple[recovery_types.RecoveryDecision, ...],
        complete: bool,
    ) -> None:
        self.hard_reasons = hard_reasons
        self.order_live = order_live
        self.quarantined_symbols = quarantined_symbols
        self.pending_refs = MappingProxyType(dict(pending_refs))
        self.decisions = decisions
        self.complete = complete


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PassiveRecoveryService:
    """DB inventory + classification + the two provable CAS burns.

    ``session_factory`` is the runner's session factory; ``clock`` is an
    injected monotonic-ish callable (never a wall-clock read here).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- observation hook bundle (review1 M1, narrow permission) ---------------

    def observation_hooks(self) -> "_RecoveryObservationHooks":
        """The existing DENY-entry observation bundle, exposed for W3 wiring.

        Every entry-granting method (begin_execution / resolve_policy /
        claim_submission) rejects, the fresh-entry gate always reports
        disabled, and ``current_gate_issue`` can never return None — the
        bundle can ONLY observe/escalate historical outcomes
        (``record_outcome`` / ``record_unresolved_reference`` /
        ``owner_intent_for``) for a runner-lifetime executor's reloaded
        pending orders. It grants no new entry authority.
        """
        return _RecoveryObservationHooks(
            self._session_factory, self._clock,
        )

    # -- inventory ------------------------------------------------------------

    def load_inventory(self) -> recovery_types.PassiveInventory:
        """DB-only inventory read; missing table/unreadable DB is HARD."""
        try:
            with self._session_factory() as db:
                rows = (
                    db.query(PassiveMandate)
                    .filter(PassiveMandate.lane == PASSIVE_LANE)
                    .all()
                )
                facts = tuple(
                    recovery_types.row_facts_from_columns(
                        mandate_id=row.id,
                        submit_state=row.submit_state,
                        claim_token=row.claim_token,
                        execution_token=row.execution_token,
                        intent_json=row.intent_json,
                        bound_broker_order_id=row.bound_broker_order_id,
                        bound_status=row.bound_broker_status,
                        bound_qty=(
                            row.bound_executed_quantity
                            if row.bound_executed_quantity is not None
                            else None
                        ),
                        bound_price=(
                            row.bound_executed_price
                            if row.bound_executed_price is not None
                            else None
                        ),
                        authorization_available=(
                            row.entry_authorisation_available
                        ),
                        authorization_consumed_at=(
                            row.entry_authorisation_consumed_at
                        ),
                    )
                    for row in rows
                )
                return recovery_types.PassiveInventory(
                    rows=facts, read_error=None,
                )
        except SQLAlchemyError as exc:
            return recovery_types.PassiveInventory(
                rows=(),
                read_error=(
                    f"passive mandate inventory unreadable: "
                    f"{type(exc).__name__}"
                ),
            )
        except Exception as exc:  # unreadable DB under installed schema
            return recovery_types.PassiveInventory(
                rows=(),
                read_error=(
                    f"passive mandate inventory read failed: "
                    f"{type(exc).__name__}"
                ),
            )

    # -- preliminary ------------------------------------------------------------

    def preliminary(
        self,
        inv: recovery_types.PassiveInventory,
    ) -> PassiveRecoverySnapshot:
        """DB-only classification; prefers zero DB writes.

        Used/unknown rows become hard-pending-verification with the SPY
        quarantine; the only durable work optionally performed is the two
        PROVABLE no-submit burns (SUBMIT_CLAIMED-unowned, CHECKING with
        complete tokens) — each an exact-predicate CAS with a single
        bounded re-read on zero rows.
        """
        if inv.read_error is not None:
            return self._hard_snapshot(
                (f"inventory read error: {inv.read_error}",),
                quarantine=PASSIVE_SYMBOL,
            )
        hard: list[str] = []
        quarantine: set[str] = set()
        decisions: list[recovery_types.RecoveryDecision] = []
        pending_refs: dict[str, str] = {}
        order_live = False
        for row in inv.rows:
            decision = recovery_types.classify_preliminary(row)
            if decision.cls == recovery_types.RecoveryClass.BURN_NO_SUBMIT:
                executed = self._execute_burn(row, decision)
                decisions.append(executed)
                if executed.cls == recovery_types.RecoveryClass.HARD_UNCERTAIN:
                    hard.append(executed.reason)
                    quarantine.add(PASSIVE_SYMBOL)
                continue
            decisions.append(decision)
            if decision.cls == recovery_types.RecoveryClass.HARD_UNCERTAIN:
                hard.append(
                    f"mandate {row.mandate_id}: {decision.reason}",
                )
                if decision.quarantine_symbol:
                    quarantine.add(decision.quarantine_symbol)
                if decision.restore_ref is not None:
                    pending_refs[decision.restore_ref[0]] = (
                        decision.restore_ref[1]
                    )
        return PassiveRecoverySnapshot(
            hard_reasons=tuple(hard),
            order_live=order_live,
            quarantined_symbols=frozenset(quarantine),
            pending_refs=pending_refs,
            decisions=tuple(decisions),
            complete=True,
        )

    # -- reconcile ----------------------------------------------------------------

    def reconcile(
        self,
        inv: recovery_types.PassiveInventory,
        *,
        order_status: Callable[[str], recovery_types.BrokerOrderFact],
        local_order: Callable[[str], recovery_types.LocalOrderFact],
        holding: recovery_types.HoldingFacts | None,
    ) -> PassiveRecoverySnapshot:
        """Full classification with observed facts + guarded outcomes.

        Read adapters are supplied by the caller (the runner): they are
        read-only broker/local lookups, never mutations. An EMPTY verified
        inventory performs no adapter calls at all. Broker read ERRORS
        never mutate the mandate. Guarded observations use only the
        authenticated existing owner/state facts.
        """
        if inv.read_error is not None:
            return self._hard_snapshot(
                (f"inventory read error: {inv.read_error}",),
                quarantine=PASSIVE_SYMBOL,
            )
        if not inv.rows:
            # Successful zero-row query: CLEAR, zero external reads.
            return PassiveRecoverySnapshot(
                hard_reasons=(),
                order_live=False,
                quarantined_symbols=frozenset(),
                pending_refs={},
                decisions=(),
                complete=True,
            )
        hard: list[str] = []
        quarantine: set[str] = set()
        decisions: list[recovery_types.RecoveryDecision] = []
        pending_refs: dict[str, str] = {}
        order_live = False
        for row in inv.rows:
            bound_id = row.bound_broker_order_id
            order_fact = (
                self._safe_order_fact(order_status, bound_id)
                if bound_id
                else None
            )
            local_fact = (
                self._safe_local_fact(local_order, bound_id)
                if bound_id
                else None
            )
            decision = recovery_types.classify_final(
                row, order_fact, local_fact, holding,
            )
            if decision.cls == recovery_types.RecoveryClass.TERMINAL_NO_FILL:
                assert order_fact is not None
                decision = replace(decision, progress=passive_protocol.PassiveOutcomeFact(
                    outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id=order_fact.broker_order_id,
                    broker_status=order_fact.status or "",
                    executed_quantity=order_fact.executed_quantity,
                    executed_price=order_fact.executed_price,
                    reason="recovery: terminal explicit no fill",
                ))
            if decision.cls == recovery_types.RecoveryClass.BURN_NO_SUBMIT:
                executed = self._execute_burn(row, decision)
                decisions.append(executed)
                if (
                    executed.cls
                    == recovery_types.RecoveryClass.HARD_UNCERTAIN
                ):
                    hard.append(executed.reason)
                    quarantine.add(PASSIVE_SYMBOL)
                continue
            # Classification is provisional until every durable observation
            # succeeds. Never publish a proof that its own write invalidated.
            if decision.progress is not None:
                try:
                    result = self._record_guarded_observation(row, decision.progress)
                    if not (
                        result is passive_protocol.OutcomeWriteResult.APPLIED
                        or result is passive_protocol.OutcomeWriteResult.IDEMPOTENT
                    ):
                        raise PassiveRecoveryError(
                            f"guarded observation did not succeed: {result!r}",
                        )
                    if decision.cls in (
                        recovery_types.RecoveryClass.HOLDING_CONFIRMED,
                        recovery_types.RecoveryClass.TERMINAL_NO_FILL,
                        recovery_types.RecoveryClass.ORDER_LIVE,
                    ):
                        self._verify_safe_order_observation(row, decision.progress)
                except Exception as exc:
                    reason = f"guarded observation/postcondition failed: {type(exc).__name__}: {exc}"
                    self._attempt_incident(f"mandate {row.mandate_id}: {reason}")
                    decision = recovery_types.RecoveryDecision(
                        mandate_id=row.mandate_id,
                        cls=recovery_types.RecoveryClass.HARD_UNCERTAIN,
                        reason=reason,
                        quarantine_symbol=PASSIVE_SYMBOL,
                    )
            decisions.append(decision)
            if decision.cls == recovery_types.RecoveryClass.HARD_UNCERTAIN:
                hard.append(f"mandate {row.mandate_id}: {decision.reason}")
                if decision.quarantine_symbol:
                    quarantine.add(decision.quarantine_symbol)
                if decision.restore_ref is not None:
                    pending_refs[decision.restore_ref[0]] = (
                        decision.restore_ref[1]
                    )
            elif decision.cls == recovery_types.RecoveryClass.ORDER_LIVE:
                order_live = True
                reference = row.owner_ref()
                if bound_id and reference:
                    pending_refs[bound_id] = reference
                if decision.quarantine_symbol:
                    quarantine.add(decision.quarantine_symbol)
            elif decision.cls in (
                recovery_types.RecoveryClass.HOLDING_CONFIRMED,
            ):
                if decision.quarantine_symbol:
                    quarantine.add(decision.quarantine_symbol)
        return PassiveRecoverySnapshot(
            hard_reasons=tuple(hard),
            order_live=order_live,
            quarantined_symbols=frozenset(quarantine),
            pending_refs=pending_refs,
            decisions=tuple(decisions),
            complete=True,
        )

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _hard_snapshot(
        reasons: tuple[str, ...], *, quarantine: str,
    ) -> PassiveRecoverySnapshot:
        return PassiveRecoverySnapshot(
            hard_reasons=reasons,
            order_live=False,
            quarantined_symbols=frozenset({quarantine}),
            pending_refs={},
            decisions=(),
            complete=False,
        )

    @staticmethod
    def _safe_order_fact(
        order_status: Callable[[str], recovery_types.BrokerOrderFact],
        bound_id: str,
    ) -> recovery_types.BrokerOrderFact:
        try:
            fact = order_status(bound_id)
        except Exception as exc:
            return recovery_types.BrokerOrderFact(
                broker_order_id=bound_id,
                status=None,
                executed_quantity=None,
                executed_price=None,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        return fact

    @staticmethod
    def _safe_local_fact(
        local_order: Callable[[str], recovery_types.LocalOrderFact],
        bound_id: str,
    ) -> recovery_types.LocalOrderFact:
        try:
            fact = local_order(bound_id)
        except Exception as exc:
            return recovery_types.LocalOrderFact(
                broker_order_id=bound_id,
                exists=False,
                symbol="",
                side="",
                quantity=None,
                lane_marker_ok=False,
                provenance_ref=None,
            )
        return fact

    def _execute_burn(
        self,
        row: recovery_types.MandateRowFacts,
        decision: recovery_types.RecoveryDecision,
    ) -> recovery_types.RecoveryDecision:
        """Execute a provable NO_SUBMIT burn with a bounded re-read.

        The CAS predicates on the EXACT id/claim/state/(exec-token) facts
        the decision was made on, so a concurrent winner is never
        clobbered. A zero-row result re-reads and reclassifies ONCE; an
        unresolvable row becomes HARD (never a silent clear or a guess).
        Write/commit failures raise into HARD handling by the caller.
        """
        try:
            with self._session_factory() as db:
                predicates = [
                    PassiveMandate.id == row.mandate_id,
                    PassiveMandate.lane == PASSIVE_LANE,
                    PassiveMandate.claim_token == (row.claim_token or ""),
                    PassiveMandate.submit_state == (decision.cas_from_state),
                ]
                if decision.cas_from_state == (
                    passive_protocol.SUBMIT_STATE_SUBMIT_CLAIMED
                ):
                    predicates.append(
                        PassiveMandate.execution_token.is_(None),
                    )
                else:
                    predicates.append(
                        PassiveMandate.execution_token
                        == (row.execution_token or ""),
                    )
                updated = _rowcount(
                    db.execute(
                        update(PassiveMandate)
                        .where(*predicates)
                        .values(
                            submit_state=(
                                passive_protocol.SUBMIT_STATE_NO_SUBMIT
                            ),
                            failure_reason=decision.reason[:500],
                        ),
                    )
                )
                if updated != 1:
                    db.rollback()
                    # Bounded re-read + reclassify ONCE.
                    fresh = db.get(PassiveMandate, row.mandate_id)
                    if fresh is None or fresh.lane != PASSIVE_LANE:
                        return recovery_types.RecoveryDecision(
                            mandate_id=row.mandate_id,
                            cls=recovery_types.RecoveryClass.HARD_UNCERTAIN,
                            reason=(
                                "burn CAS lost the row; mandate missing"
                            ),
                            quarantine_symbol=PASSIVE_SYMBOL,
                        )
                    if fresh.submit_state != decision.cas_from_state:
                        # A concurrent winner advanced the row: NEVER
                        # clobber; reclassify the observed state.
                        refreshed = recovery_types.row_facts_from_columns(
                            mandate_id=fresh.id,
                            submit_state=fresh.submit_state,
                            claim_token=fresh.claim_token,
                            execution_token=fresh.execution_token,
                            intent_json=fresh.intent_json,
                            bound_broker_order_id=(
                                fresh.bound_broker_order_id
                            ),
                            bound_status=fresh.bound_broker_status,
                            bound_qty=fresh.bound_executed_quantity,
                            bound_price=fresh.bound_executed_price,
                            authorization_available=(
                                fresh.entry_authorisation_available
                            ),
                            authorization_consumed_at=(
                                fresh.entry_authorisation_consumed_at
                            ),
                        )
                        reclassified = recovery_types.classify_preliminary(
                            refreshed,
                        )
                        if (
                            reclassified.cls
                            == recovery_types.RecoveryClass.BURN_NO_SUBMIT
                        ):
                            # Another worker legitimately burned it first.
                            return recovery_types.RecoveryDecision(
                                mandate_id=row.mandate_id,
                                cls=recovery_types.RecoveryClass.CLEAR,
                                reason=(
                                    "concurrent worker already burned the"
                                    " authorization to NO_SUBMIT"
                                ),
                            )
                        return reclassified
                    return recovery_types.RecoveryDecision(
                        mandate_id=row.mandate_id,
                        cls=recovery_types.RecoveryClass.HARD_UNCERTAIN,
                        reason=(
                            "burn CAS matched no row and the state did not"
                            " advance; unresolved"
                        ),
                        quarantine_symbol=PASSIVE_SYMBOL,
                    )
                db.commit()
                return recovery_types.RecoveryDecision(
                    mandate_id=row.mandate_id,
                    cls=recovery_types.RecoveryClass.CLEAR,
                    reason=(
                        "burned provable no-submit authorization"
                        f" ({decision.cas_from_state} -> NO_SUBMIT)"
                    ),
                )
        except Exception as exc:
            self._attempt_incident(
                f"mandate {row.mandate_id}: burn CAS failed: "
                f"{type(exc).__name__}",
            )
            return recovery_types.RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=recovery_types.RecoveryClass.HARD_UNCERTAIN,
                reason=(
                    f"burn CAS raised {type(exc).__name__}; durable state"
                    " unproven"
                ),
                quarantine_symbol=PASSIVE_SYMBOL,
            )

    def _record_guarded_observation(
        self,
        row: recovery_types.MandateRowFacts,
        fact: passive_protocol.PassiveOutcomeFact,
    ) -> passive_protocol.OutcomeWriteResult:
        """Persist a guarded observation via the authenticated owner.

        Builds the PassiveOwner ONLY from the row's complete validated
        tokens+intent (never invented), and uses the existing
        ``record_outcome`` CAS path. The caller consumes the typed result
        and converts exceptions to HARD before aggregating its snapshot.
        """
        owner_ref = row.owner_ref()
        if not owner_ref or row.intent is None:
            raise PassiveRecoveryError("observation has no authenticated owner")
        mandate_id_s, claim, exec_token = owner_ref.split(":", 2)
        owner = passive_protocol.PassiveOwner(
            ref=passive_protocol.PassiveAttemptRef(
                mandate_id=int(mandate_id_s), claim_token=claim,
            ),
            execution_token=exec_token, intent=row.intent,
        )
        bundle = _RecoveryObservationHooks(self._session_factory, self._clock)
        return bundle.record_outcome(owner, fact)

    def _verify_safe_order_observation(
        self,
        row: recovery_types.MandateRowFacts,
        fact: passive_protocol.PassiveOutcomeFact,
    ) -> None:
        """Prove every safe known-order class against committed facts.

        A same-ID receipt on UNCERTAIN deliberately returns IDEMPOTENT.
        That acknowledgement is not authority to clear a terminal-no-fill
        guard or advertise a normal live order. Require the exact observed
        status/quantity/price as well as the unchanged authenticated owner.
        For terminal no-fill the classified quantity is explicitly zero;
        missing durable quantity cannot satisfy this comparison.
        """
        inventory = self.load_inventory()
        if inventory.read_error is not None:
            raise PassiveRecoveryError(inventory.read_error)
        fresh = next((item for item in inventory.rows if item.mandate_id == row.mandate_id), None)
        if (
            fresh is None
            or fresh.submit_state != passive_protocol.SUBMIT_STATE_ORDER_KNOWN
            or fresh.bound_qty != fact.executed_quantity
            or fresh.bound_status != fact.broker_status
            or fresh.bound_price != fact.executed_price
            or fresh.bound_broker_order_id != row.bound_broker_order_id
            or fresh.owner_ref() != row.owner_ref()
            or fresh.intent != row.intent
            or fresh.intent_issue is not None
            or fresh.authorization_available != row.authorization_available
            or fresh.authorization_consumed_at != row.authorization_consumed_at
        ):
            raise PassiveRecoveryError("safe order durable state/fill/identity changed")

    def _attempt_incident(self, message: str) -> None:
        """Best-effort durable incident (existing dedup category)."""
        try:
            from app.services.reconciliation_incident_service import (
                ReconciliationFailure,
                ReconciliationIncidentService,
            )

            with self._session_factory() as db:
                service = ReconciliationIncidentService(
                    first_reminder_seconds=0.0,
                )
                service.record_failure(
                    db,
                    ReconciliationFailure(
                        source=RECOVERY_INCIDENT_SOURCE,
                        category=RECOVERY_INCIDENT_CATEGORY,
                        symbols=(PASSIVE_SYMBOL,),
                        message=message[:1000],
                        error_type="PassiveRecovery",
                    ),
                )
                db.commit()
        except Exception:
            logger.exception(
                "failed to record the %s incident", RECOVERY_INCIDENT_CATEGORY,
            )


def _rowcount(execution_result: Any) -> int:
    rowcount = getattr(execution_result, "rowcount", None)
    try:
        return int(rowcount) if rowcount is not None else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Recovery-only observation hook bundle
# ---------------------------------------------------------------------------


class _RecoveryObservationHooks:
    """Observation-only facade over ``PassiveSubmitHookBundle`` mechanics.

    Contract: a recovery bundle must DENY every entry-granting method
    (begin/resolve/claim and the fresh-entry gate always reports disabled)
    while permitting ``record_outcome``/``record_unresolved_reference`` —
    so recovery can observe/escalate even with the feature OFF, and can
    never be used to grant a NEW entry.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- denied entry surface --------------------------------------------------

    def begin_execution(
        self,
        ref: passive_protocol.PassiveAttemptRef,
        execution_token: str,
    ) -> passive_protocol.PassiveRejection:
        return passive_protocol.PassiveRejection(
            reason=(
                "recovery observation bundle cannot begin execution; new"
                " entries are not authorized through recovery"
            ),
        )

    def resolve_policy(
        self,
        owner: passive_protocol.PassiveOwner,
        request: passive_protocol.PassiveOrderSpec,
    ) -> passive_protocol.PassiveRejection:
        return passive_protocol.PassiveRejection(
            reason=(
                "recovery observation bundle cannot resolve entry policy"
            ),
        )

    def claim_submission(
        self,
        owner: passive_protocol.PassiveOwner,
        final_order: passive_protocol.PassiveOrderSpec,
        cash: passive_protocol.UsdCashEvidence,
    ) -> bool:
        return False

    def current_gate_issue(self) -> str:
        # Always-ALLOW would be an entry grant; always-DENY is required.
        return (
            "SPY passive lane is disabled for recovery observation"
            " (AUTO_TRADE_SPY_PASSIVE_ENABLED=false)"
        )

    def now(self) -> datetime:
        return self._clock()

    def owner_intent_for(
        self, mandate_id: int, claim_token: str,
    ) -> passive_protocol.ImmutablePassiveIntent | None:
        try:
            with self._session_factory() as db:
                row = db.get(PassiveMandate, mandate_id)
                if row is None or row.lane != PASSIVE_LANE:
                    return None
                if row.claim_token != claim_token:
                    return None
                if not row.intent_json:
                    return None
                # DB errors propagate (never catch -> None).
                return passive_protocol.intent_from_json(row.intent_json)
        except Exception:
            raise

    # -- permitted observation surface ------------------------------------------

    def record_outcome(
        self,
        owner: passive_protocol.PassiveOwner,
        fact: passive_protocol.PassiveOutcomeFact,
    ) -> passive_protocol.OutcomeWriteResult:
        from app.services.passive_allocation_service import (
            PassiveSubmitHookBundle,
        )

        bundle = PassiveSubmitHookBundle(
            self._session_factory,
            current_gate_issue=self.current_gate_issue,
            clock=self._clock,
        )
        return bundle.record_outcome(owner, fact)

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        from app.services.passive_allocation_service import (
            PassiveSubmitHookBundle,
        )

        bundle = PassiveSubmitHookBundle(
            self._session_factory,
            current_gate_issue=self.current_gate_issue,
            clock=self._clock,
        )
        bundle.record_unresolved_reference(
            reference, reason, broker_order_id=broker_order_id,
        )
