"""Pure Phase2a OFF-recovery classification values (writer W1).

Source of truth: /tmp/opencode/spy-passive-phase2a-contract.md. Everything
here is frozen values and pure classification — no DB, no services, no
settings, no wall clock, no network. ``PassiveRecoveryService`` (services/
passive_recovery_service.py) is the only IO consumer of these types.

Phase2a scope guard: this module reasons about ALREADY-PERSISTED passive
mandate rows and observed broker/local/holding facts. It never creates an
authorization, never submits/cancels/modifies a broker order, and never
adopts positions heuristically — a missing provenance or an unprovable
fact is always HARD, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from app.domain.passive_allocation.model import PASSIVE_SYMBOL
from app.domain.passive_allocation.protocol import (
    ImmutablePassiveIntent,
    PassiveOutcomeFact,
    SUBMIT_STATE_AUTHORIZED,
    SUBMIT_STATE_CHECKING,
    SUBMIT_STATE_NO_SUBMIT,
    SUBMIT_STATE_ORDER_KNOWN,
    SUBMIT_STATE_SUBMIT_CLAIMED,
    SUBMIT_STATE_SUBMITTING,
    SUBMIT_STATE_UNCERTAIN,
    intent_from_json,
)

# --- classification ---------------------------------------------------------

_RECOVERY_CLASS_DOC = (
    "CLEAR (verified safe), BURN_NO_SUBMIT (provably never submitted),"
    " ORDER_LIVE, TERMINAL_NO_FILL, HOLDING_CONFIRMED, HARD_UNCERTAIN."
)


class RecoveryClass(str, Enum):
    """Outcome of classifying one passive mandate row at recovery time."""

    CLEAR = "CLEAR"
    BURN_NO_SUBMIT = "BURN_NO_SUBMIT"
    ORDER_LIVE = "ORDER_LIVE"
    TERMINAL_NO_FILL = "TERMINAL_NO_FILL"
    HOLDING_CONFIRMED = "HOLDING_CONFIRMED"
    HARD_UNCERTAIN = "HARD_UNCERTAIN"


# --- row / observation facts ------------------------------------------------


@dataclass(frozen=True, slots=True)
class MandateRowFacts:
    """Explicit typed facts of one persisted passive_mandates row.

    Parent reconciliation #2: the authorization availability flag and
    consumed timestamp are FIRST-CLASS fields (never re-derived from the
    submit state), and raw evidence is preserved verbatim for the reason
    strings. ``intent``/``intent_issue`` carry the parsed immutable intent
    or its parse failure — a normal never-used AUTHORIZED row may
    legitimately have no intent by design.
    """

    mandate_id: int
    submit_state: str | None
    claim_token: str | None
    execution_token: str | None
    intent: ImmutablePassiveIntent | None
    intent_issue: str | None
    bound_broker_order_id: str | None
    bound_status: str | None
    bound_qty: Decimal | None
    bound_price: Decimal | None
    authorization_available: bool | None
    authorization_consumed_at: datetime | None

    def owner_ref(self) -> str | None:
        """``mandate:claim:exec`` ONLY when all three parts are complete.

        No guessing: a missing or empty token yields None (the caller must
        treat that as unproven identity, never fabricate a reference).
        """
        if (
            isinstance(self.mandate_id, int)
            and self.mandate_id > 0
            and self.claim_token
            and self.execution_token
        ):
            return f"{self.mandate_id}:{self.claim_token}:{self.execution_token}"
        return None


def row_facts_from_columns(
    *,
    mandate_id: int,
    submit_state: str | None,
    claim_token: str | None,
    execution_token: str | None,
    intent_json: str | None,
    bound_broker_order_id: str | None,
    bound_status: str | None,
    bound_qty: Decimal | None,
    bound_price: Decimal | None,
    authorization_available: bool | None,
    authorization_consumed_at: datetime | None,
) -> MandateRowFacts:
    """Build facts from raw columns, isolating intent parse failures.

    An unparseable intent becomes ``intent=None`` + ``intent_issue=...``
    (fail-closed evidence), never an exception that would abort loading
    the whole inventory.
    """
    intent: ImmutablePassiveIntent | None = None
    intent_issue: str | None = None
    if intent_json:
        try:
            intent = intent_from_json(intent_json)
        except ValueError as exc:
            intent_issue = str(exc)
    elif submit_state != SUBMIT_STATE_AUTHORIZED:
        # A used state without an intent snapshot is itself an anomaly the
        # classifier must see; a never-used AUTHORIZED row may lack it.
        intent_issue = "intent snapshot missing on a used submit state"
    return MandateRowFacts(
        mandate_id=mandate_id,
        submit_state=submit_state,
        claim_token=claim_token,
        execution_token=execution_token,
        intent=intent,
        intent_issue=intent_issue,
        bound_broker_order_id=bound_broker_order_id,
        bound_status=bound_status,
        bound_qty=bound_qty,
        bound_price=bound_price,
        authorization_available=authorization_available,
        authorization_consumed_at=authorization_consumed_at,
    )


@dataclass(frozen=True, slots=True)
class PassiveInventory:
    """DB-only inventory of persisted passive mandate rows.

    Parent reconciliation #1: ``read_error`` is non-None when the table is
    missing or the database is unreadable under the installed schema —
    that is HARD, never "no rows". Only a successful query returning zero
    rows is a CLEAR empty inventory (which must add NO broker reads).
    """

    rows: tuple[MandateRowFacts, ...]
    read_error: str | None

    @property
    def needs_recovery(self) -> bool:
        """True when any row or the read itself requires recovery work.

        Includes: read errors, contradictory/unknown facts, and any USED
        submit state. A merely non-AUTHORIZED-but-unused state still counts
        (it is in-flight or terminal protocol state we must resolve);
        ``state != AUTHORIZED`` alone is NOT the criterion — the facts are.
        """
        if self.read_error is not None:
            return True
        for row in self.rows:
            issue = _contradiction_issue(row)
            if issue is not None:
                return True
            if row.submit_state != SUBMIT_STATE_AUTHORIZED:
                return True
        return False


@dataclass(frozen=True, slots=True)
class BrokerOrderFact:
    """One read-only broker order observation (never a mutation)."""

    broker_order_id: str
    status: str | None
    executed_quantity: Decimal | None
    executed_price: Decimal | None
    error: str | None


@dataclass(frozen=True, slots=True)
class LocalOrderFact:
    """The local ledger's view of one broker order id."""

    broker_order_id: str
    exists: bool
    symbol: str
    side: str
    quantity: Decimal | None
    lane_marker_ok: bool
    provenance_ref: str | None


@dataclass(frozen=True, slots=True)
class HoldingFacts:
    """Account holdings relevant to SPY ownership proof.

    ``tracked_spy_cost`` is the TOTAL confirmed tracked cost (adapter must
    use actual tracked-entry semantics — never mix a unit average with a
    total). A missing/unproven cost is HARD, never zero.
    """

    broker_spy_qty: Decimal | None
    other_nonzero_symbols: tuple[str, ...]
    tracked_spy_qty: Decimal | None
    tracked_spy_cost: Decimal | None


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One row's recovery verdict plus its concrete durable action."""

    mandate_id: int
    cls: RecoveryClass
    reason: str
    cas_from_state: str | None = None
    cas_to_state: str | None = None
    progress: PassiveOutcomeFact | None = None
    quarantine_symbol: str | None = None
    restore_ref: tuple[str, str] | None = None


# --- helpers ----------------------------------------------------------------

_KNOWN_LIVE_STATUSES: Final[frozenset[str]] = frozenset(
    {"SUBMITTED", "PARTIAL_FILLED"},
)
_KNOWN_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"FILLED", "REJECTED", "CANCELLED"},
)


def _contradiction_issue(row: MandateRowFacts) -> str | None:
    """Contradictory AUTHORIZED/NO_SUBMIT facts (parent reconciliation #2).

    - An AUTHORIZED row with any claim/exec/consumed/bound/snapshot marker
      is NOT clear (it claims to be unused while carrying used evidence).
    - A NO_SUBMIT row with a possible-submission or broker binding is NOT
      clear.
    - Unknown submit states and intent parse failures on used rows are
      fail-closed anomalies.
    """
    state = row.submit_state
    if state is None or state not in (
        SUBMIT_STATE_AUTHORIZED,
        SUBMIT_STATE_NO_SUBMIT,
        SUBMIT_STATE_CHECKING,
        SUBMIT_STATE_SUBMIT_CLAIMED,
        SUBMIT_STATE_SUBMITTING,
        SUBMIT_STATE_ORDER_KNOWN,
        SUBMIT_STATE_UNCERTAIN,
    ):
        return f"unknown submit_state {state!r}"
    if state == SUBMIT_STATE_AUTHORIZED:
        problems: list[str] = []
        if row.claim_token:
            problems.append("claim_token present")
        if row.execution_token:
            problems.append("execution_token present")
        if row.authorization_consumed_at is not None:
            problems.append("consumed_at present")
        if row.bound_broker_order_id:
            problems.append("bound_broker_order_id present")
        if row.intent is not None:
            problems.append("intent snapshot present")
        if row.authorization_available is False:
            problems.append("authorization_available=False")
        if problems:
            return "AUTHORIZED row carries used markers: " + ", ".join(
                problems,
            )
        return None
    if state == SUBMIT_STATE_NO_SUBMIT:
        problems = []
        if row.bound_broker_order_id:
            problems.append("broker binding present")
        if row.submit_state == SUBMIT_STATE_NO_SUBMIT and (
            row.execution_token
        ):
            problems.append("execution_token present")
        if problems:
            return "NO_SUBMIT row carries possible-submission markers: " + (
                ", ".join(problems)
            )
        return None
    if row.intent_issue is not None and row.submit_state not in (
        SUBMIT_STATE_AUTHORIZED,
    ):
        return row.intent_issue
    return None


# --- classification (pure) ---------------------------------------------------


def classify_preliminary(row: MandateRowFacts) -> RecoveryDecision:
    """DB-only classification: safe rows are CLEAR/BURN_NO_SUBMIT; every
    used/unknown/contradictory row is hard-pending-verification with the
    SPY quarantine, and bound rows may carry a restore_ref the RUNNER must
    authenticate (via the local order) before installing."""
    issue = _contradiction_issue(row)
    if issue is not None:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=f"contradictory row facts: {issue}",
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    state = row.submit_state
    if state == SUBMIT_STATE_AUTHORIZED:
        # Valid never-used authorization (no intent by design).
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.CLEAR,
            reason="clearly never-used AUTHORIZED row",
        )
    if state == SUBMIT_STATE_NO_SUBMIT:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.CLEAR,
            reason="valid NO_SUBMIT (no possible submission)",
        )
    if state == SUBMIT_STATE_SUBMIT_CLAIMED:
        if row.execution_token is None and row.intent is not None:
            return RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=RecoveryClass.BURN_NO_SUBMIT,
                reason=(
                    "SUBMIT_CLAIMED without execution ownership: only a"
                    " durable SUBMITTING state ever allows a broker call,"
                    " so a provable no-submit burn is safe"
                ),
                cas_from_state=SUBMIT_STATE_SUBMIT_CLAIMED,
                cas_to_state=SUBMIT_STATE_NO_SUBMIT,
            )
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=(
                "SUBMIT_CLAIMED with unexpected ownership markers"
                " (execution_token present or intent unusable)"
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    if state == SUBMIT_STATE_CHECKING:
        if (
            row.claim_token
            and row.execution_token
            and row.intent is not None
        ):
            return RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=RecoveryClass.BURN_NO_SUBMIT,
                reason=(
                    "CHECKING with complete exact tokens: execution"
                    " ownership was won but the submit right was never"
                    " taken, so a provable no-submit burn is safe"
                ),
                cas_from_state=SUBMIT_STATE_CHECKING,
                cas_to_state=SUBMIT_STATE_NO_SUBMIT,
            )
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason="CHECKING row with incomplete tokens",
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    # SUBMITTING / ORDER_KNOWN / UNCERTAIN need broker/local/holding facts
    # (preliminary = hard pending verification; bound rows may offer a
    # restore_ref for the RUNNER to authenticate later).
    restore_ref: tuple[str, str] | None = None
    if (
        state == SUBMIT_STATE_ORDER_KNOWN
        and row.bound_broker_order_id
        and row.owner_ref() is not None
    ):
        restore_ref = (row.bound_broker_order_id, row.owner_ref() or "")
    return RecoveryDecision(
        mandate_id=row.mandate_id,
        cls=RecoveryClass.HARD_UNCERTAIN,
        reason=f"used submit state {state!r} requires verified facts",
        quarantine_symbol=PASSIVE_SYMBOL,
        restore_ref=restore_ref,
    )


def classify_final(
    row: MandateRowFacts,
    order: BrokerOrderFact | None,
    local: LocalOrderFact | None,
    holding: HoldingFacts | None,
) -> RecoveryDecision:
    """Full classification with observed broker/local/holding facts.

    All identity/value validation happens FIRST (finite non-negative
    quantities, positive prices where needed, exact ids); anything
    unprovable is HARD — never a heuristic adoption, never a guess.
    """
    issue = _contradiction_issue(row)
    if issue is not None:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=f"contradictory row facts: {issue}",
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    state = row.submit_state
    prelim = classify_preliminary(row)
    if prelim.cls in (RecoveryClass.CLEAR, RecoveryClass.BURN_NO_SUBMIT):
        # Preliminary-safe rows stay safe; BURN CAS is executed by the
        # service, the classifier only describes it.
        return prelim

    # -- SUBMITTING: a durable submit right was taken. ----------------------
    if state == SUBMIT_STATE_SUBMITTING:
        if not row.bound_broker_order_id:
            return RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=RecoveryClass.HARD_UNCERTAIN,
                reason=(
                    "SUBMITTING without a bound broker id: possibly"
                    " submitted, no heuristic symbol/time/qty adoption"
                ),
                progress=PassiveOutcomeFact(
                    outcome=SUBMIT_STATE_UNCERTAIN,
                    reason=(
                        "recovery: SUBMITTING with no bound id"
                    ),
                ),
                quarantine_symbol=PASSIVE_SYMBOL,
            )
        # With an id but SUBMITTING still recorded, the bind never
        # completed; durable facts are inconsistent — conservative HARD.
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=(
                "SUBMITTING with a bound id but no completed bind:"
                " inconsistent durable facts"
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )

    # -- UNCERTAIN (sticky): --------------------------------------------------
    if state == SUBMIT_STATE_UNCERTAIN:
        # Ordinary observations never auto-clear UNCERTAIN; an observed
        # overfill/unknown status is recorded as a guarded outcome that
        # PRESERVES the observation and any known id.
        observation = _observation_fact(row, order, local)
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason="UNCERTAIN is sticky pending explicit reconciliation",
            progress=observation,
            quarantine_symbol=PASSIVE_SYMBOL,
        )

    if state != SUBMIT_STATE_ORDER_KNOWN:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=f"unclassifiable submit state {state!r}",
            quarantine_symbol=PASSIVE_SYMBOL,
        )

    # -- ORDER_KNOWN: full identity validation -------------------------------
    identity_issue = _order_known_identity_issue(row, order, local)
    if identity_issue is not None:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=identity_issue,
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    assert order is not None and row.intent is not None
    observed_qty = order.executed_quantity
    observed_price = order.executed_price
    status = str(order.status or "")
    if observed_qty is not None and observed_qty > row.intent.quantity:
        # Real broker overfill: guarded P3 outcome — observation preserved,
        # known id retained, canonical bound qty stays unset for the excess.
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=(
                f"broker executed quantity {observed_qty} exceeds the"
                f" immutable intent quantity {row.intent.quantity}"
            ),
            progress=PassiveOutcomeFact(
                outcome=SUBMIT_STATE_UNCERTAIN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status=status,
                executed_quantity=observed_qty,
                executed_price=observed_price,
                reason=(
                    f"overfill observed: {observed_qty} > intent"
                    f" {row.intent.quantity}"
                ),
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    if status not in _KNOWN_LIVE_STATUSES | _KNOWN_TERMINAL_STATUSES:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=f"unknown broker status {status!r}",
            progress=PassiveOutcomeFact(
                outcome=SUBMIT_STATE_UNCERTAIN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status=status,
                executed_quantity=observed_qty,
                executed_price=observed_price,
                reason=f"unknown broker status {status!r}",
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    if observed_qty is not None and observed_qty < 0:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=f"negative executed quantity {observed_qty}",
            quarantine_symbol=PASSIVE_SYMBOL,
        )

    if status in _KNOWN_LIVE_STATUSES:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.ORDER_LIVE,
            reason=f"broker order live with status {status!r}",
            progress=PassiveOutcomeFact(
                outcome=SUBMIT_STATE_ORDER_KNOWN,
                broker_order_id=row.bound_broker_order_id or "",
                broker_status=status,
                executed_quantity=observed_qty,
                executed_price=observed_price,
                reason=f"recovery observation: {status}",
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )

    # -- terminal -------------------------------------------------------------
    qty = observed_qty
    if qty is None:
        # A MISSING quantity is NOT zero: the terminal receipt gives no
        # fill proof, so ownership cannot be decided — HARD.
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=(
                "terminal status without an explicit executed quantity"
                " (missing is not zero)"
            ),
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    if qty == 0:
        # Explicit zero requires the broker holding to be EXACTLY zero too.
        holding_issue = _require_holding(holding)
        if holding_issue is not None:
            return RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=RecoveryClass.HARD_UNCERTAIN,
                reason=holding_issue,
                quarantine_symbol=PASSIVE_SYMBOL,
            )
        assert holding is not None
        if (holding.broker_spy_qty or Decimal("0")) != 0:
            return RecoveryDecision(
                mandate_id=row.mandate_id,
                cls=RecoveryClass.HARD_UNCERTAIN,
                reason=(
                    "terminal no-fill claimed but broker holds"
                    f" {holding.broker_spy_qty} SPY"
                ),
                quarantine_symbol=PASSIVE_SYMBOL,
            )
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.TERMINAL_NO_FILL,
            reason="terminal with explicit zero fill and zero SPY holding",
        )

    # terminal positive q <= intent: full ownership proof required.
    holding_issue = _require_holding(holding)
    if holding_issue is not None:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=holding_issue,
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    assert holding is not None and row.intent is not None
    price = observed_price
    if price is None or price <= 0:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason="terminal positive fill without a positive price",
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    proof_issue = _holding_proof_issue(row, holding, qty, price)
    if proof_issue is not None:
        return RecoveryDecision(
            mandate_id=row.mandate_id,
            cls=RecoveryClass.HARD_UNCERTAIN,
            reason=proof_issue,
            quarantine_symbol=PASSIVE_SYMBOL,
        )
    return RecoveryDecision(
        mandate_id=row.mandate_id,
        cls=RecoveryClass.HOLDING_CONFIRMED,
        reason=(
            f"terminal positive fill {qty} proven against broker and"
            " tracked holdings"
        ),
        progress=PassiveOutcomeFact(
            outcome=SUBMIT_STATE_ORDER_KNOWN,
            broker_order_id=row.bound_broker_order_id or "",
            broker_status=status,
            executed_quantity=qty,
            executed_price=price,
            reason="recovery: holding-confirmed terminal fill",
        ),
        quarantine_symbol=PASSIVE_SYMBOL,
    )


def _require_holding(holding: HoldingFacts | None) -> str | None:
    """Holdings are REQUIRED wherever ownership is decided; None is HARD."""
    if holding is None:
        return (
            "holding snapshot missing: ownership cannot be proven"
            " (None is not empty)"
        )
    return None


def _holding_proof_issue(
    row: MandateRowFacts,
    holding: HoldingFacts,
    qty: Decimal,
    price: Decimal,
) -> str | None:
    """The full HOLDING_CONFIRMED proof chain (contract §64)."""
    assert row.intent is not None
    if holding.broker_spy_qty is None:
        return "broker SPY quantity missing"
    if holding.broker_spy_qty != qty:
        return (
            f"broker SPY {holding.broker_spy_qty} != observed fill {qty}"
        )
    if holding.other_nonzero_symbols:
        return (
            "other nonzero positions: "
            + ", ".join(sorted(holding.other_nonzero_symbols))
        )
    if holding.tracked_spy_qty is None:
        return "tracked SPY quantity missing"
    if holding.tracked_spy_qty != qty:
        return (
            f"tracked SPY {holding.tracked_spy_qty} != observed fill {qty}"
        )
    if holding.tracked_spy_cost is None:
        return "tracked TOTAL SPY cost missing (unit averages are not total)"
    tolerance = max(Decimal("0.01"), Decimal("0.0001") * qty * price)
    expected = qty * price
    if abs(holding.tracked_spy_cost - expected) > tolerance:
        return (
            f"tracked TOTAL cost {holding.tracked_spy_cost} does not match"
            f" q*price {expected} within {tolerance}"
        )
    return None


def _order_known_identity_issue(
    row: MandateRowFacts,
    order: BrokerOrderFact | None,
    local: LocalOrderFact | None,
) -> str | None:
    """The exact ORDER_KNOWN identity chain (contract §60)."""
    if not row.bound_broker_order_id:
        return "ORDER_KNOWN without a bound broker id"
    if row.owner_ref() is None:
        return "ORDER_KNOWN without a complete owner reference"
    if order is None:
        return "broker order fact unavailable"
    if order.error is not None:
        return f"broker order read error: {order.error}"
    if order.broker_order_id != row.bound_broker_order_id:
        return (
            f"queried id {order.broker_order_id!r} != bound id"
            f" {row.bound_broker_order_id!r}"
        )
    if local is None:
        return "local order fact unavailable"
    if not local.exists:
        return f"local order {row.bound_broker_order_id!r} not found"
    if local.broker_order_id != row.bound_broker_order_id:
        return "local order id mismatch"
    if row.intent is None:
        return "intent unusable for identity validation"
    if local.symbol != row.intent.symbol:
        return f"local order symbol {local.symbol!r} != intent symbol"
    if local.side != "BUY":
        return f"local order side {local.side!r} is not BUY"
    if local.quantity != row.intent.quantity:
        return (
            f"local order quantity {local.quantity} != intent quantity"
            f" {row.intent.quantity}"
        )
    if not local.lane_marker_ok:
        return "local order lacks the passive lane marker"
    if not local.provenance_ref or local.provenance_ref != row.owner_ref():
        return (
            "local order provenance reference missing or does not match"
            " the row's complete owner reference"
        )
    return None


def _observation_fact(
    row: MandateRowFacts,
    order: BrokerOrderFact | None,
    local: LocalOrderFact | None,
) -> PassiveOutcomeFact | None:
    """Best-effort guarded observation for sticky-UNCERTAIN rows.

    Uses only authenticated existing owner/state facts; never invents an
    id (a missing/unreadable broker fact yields no progress fact at all,
    so nothing can be smuggled into the mandate).
    """
    if order is None or order.error is not None:
        return None
    if not order.broker_order_id:
        return None
    bound_id = row.bound_broker_order_id
    if bound_id and order.broker_order_id != bound_id:
        # Never record an id that is not the bound one.
        return None
    return PassiveOutcomeFact(
        outcome=SUBMIT_STATE_UNCERTAIN,
        broker_order_id=bound_id or order.broker_order_id or "",
        broker_status=order.status or "",
        executed_quantity=order.executed_quantity,
        executed_price=order.executed_price,
        reason="recovery observation on sticky-uncertain row",
    )
