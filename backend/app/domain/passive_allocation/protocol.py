"""Pure submit-protocol values for the SPY passive lane (phase 1, OFF).

Source of truth: architect contract ``spy-passive-submit-contract.md``
(ora-21). Everything in this module is frozen values and pure validation:
no DB, no services, no settings, no wall clock, no network.

State machine (strictly forward; no transition restores authorisation)::

    AUTHORIZED -> SUBMIT_CLAIMED   service reservation (claim token, immutable intent)
              -> CHECKING          execution ownership (fresh execution token)
              -> SUBMITTING        submit right (final order/cash/fee snapshot)
              -> ORDER_KNOWN       broker receipt bound (fills preserved)
               | NO_SUBMIT         certain refusal BEFORE any broker mutation
               | UNCERTAIN         possibly-submitted / unknown outcome

``NO_SUBMIT`` and ``UNCERTAIN`` are terminal exactly like ``ORDER_KNOWN``;
a burned-but-unused authorisation is always preferred over a replay.

The four database hooks keep their frozen signatures (see
``PassiveSubmitHooks``); the bundle additionally carries two fresh readers
(``current_gate_issue`` and ``now``) so no layer caches a stale lane flag,
PAPER attestation, or clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol, runtime_checkable

from app.domain.passive_allocation.model import (
    PASSIVE_ALLOTMENT_USD,
    PASSIVE_LANE,
    POLICY_VERSION,
    PASSIVE_ORDER_BINDING,
    PASSIVE_REVIEW_INTERVAL_MONTHS,
    PASSIVE_SYMBOL,
    RiskModel,
)

# --- protocol identity -----------------------------------------------------

PASSIVE_PROTOCOL_VERSION: Final[str] = "passive-submit-v2"

#: Identity markers for uncertainty-path owner objects ONLY (never used to
#: authorise anything — the row lookup re-checks every token).
POLICY_VERSION_FALLBACK: Final[str] = "passive-allocation-uncertain-fallback"
FALLBACK_ALLOTMENT_USD: Final[Decimal] = Decimal("1")

# --- submit states ---------------------------------------------------------

SUBMIT_STATE_AUTHORIZED: Final[str] = "AUTHORIZED"
SUBMIT_STATE_SUBMIT_CLAIMED: Final[str] = "SUBMIT_CLAIMED"
SUBMIT_STATE_CHECKING: Final[str] = "CHECKING"
SUBMIT_STATE_SUBMITTING: Final[str] = "SUBMITTING"
SUBMIT_STATE_ORDER_KNOWN: Final[str] = "ORDER_KNOWN"
SUBMIT_STATE_NO_SUBMIT: Final[str] = "NO_SUBMIT"
SUBMIT_STATE_UNCERTAIN: Final[str] = "UNCERTAIN"

#: Legacy phase-1 states that may exist in rows written before this protocol.
#: They are never re-authorised; the migration maps them to UNCERTAIN.
LEGACY_SUBMIT_STATES: Final[frozenset[str]] = frozenset(
    {"SUBMITTING", "SUBMITTED", "FAILED"},
)

SUBMIT_STATES: Final[tuple[str, ...]] = (
    SUBMIT_STATE_AUTHORIZED,
    SUBMIT_STATE_SUBMIT_CLAIMED,
    SUBMIT_STATE_CHECKING,
    SUBMIT_STATE_SUBMITTING,
    SUBMIT_STATE_ORDER_KNOWN,
    SUBMIT_STATE_NO_SUBMIT,
    SUBMIT_STATE_UNCERTAIN,
)

#: Outcome facts may only land from these source states. ``ORDER_KNOWN`` ->
#: ``UNCERTAIN`` exists solely for escalation (persistence/settlement failure
#: or a conflicting broker id after a known submission); it never erases the
#: bound broker id.
_ALLOWED_OUTCOME_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    SUBMIT_STATE_NO_SUBMIT: frozenset(
        {SUBMIT_STATE_CHECKING, SUBMIT_STATE_SUBMITTING, SUBMIT_STATE_NO_SUBMIT},
    ),
    SUBMIT_STATE_ORDER_KNOWN: frozenset(
        {SUBMIT_STATE_SUBMITTING, SUBMIT_STATE_ORDER_KNOWN},
    ),
    SUBMIT_STATE_UNCERTAIN: frozenset(
        {
            SUBMIT_STATE_CHECKING,
            SUBMIT_STATE_SUBMITTING,
            SUBMIT_STATE_ORDER_KNOWN,
            SUBMIT_STATE_UNCERTAIN,
        },
    ),
}

# --- strict cash evidence --------------------------------------------------

#: Maximum age of a locally observed cash fact, measured start -> now so the
#: window includes network, queueing and lock latency on our side.
PASSIVE_CASH_MAX_AGE: Final[timedelta] = timedelta(seconds=5)

PASSIVE_CASH_CURRENCY: Final[str] = "USD"
PASSIVE_CASH_PROVENANCE: Final[str] = (
    "account_balance.cash_infos.available_cash"
)


@runtime_checkable
class UsdCashEvidence(Protocol):
    """Structural type of writer A's frozen ``UsdCashSnapshot``.

    The concrete dataclass lives in ``app.core.cash_evidence`` (writer A).
    This protocol pins only the fields this lane validates, so A's frozen
    type satisfies it structurally without either package importing the
    other's internals.
    """

    @property
    def amount(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...

    @property
    def request_started_at(self) -> datetime: ...

    @property
    def request_completed_at(self) -> datetime: ...

    @property
    def provenance(self) -> str: ...


def validate_cash_evidence(
    *,
    cash: UsdCashEvidence | None,
    quantity: Decimal,
    approved_price: Decimal,
    fee: Decimal,
    now: datetime,
    max_age: timedelta = PASSIVE_CASH_MAX_AGE,
) -> str | None:
    """Validate a locally observed strict-USD cash fact; None when valid.

    Fail-closed on every dimension the contract pins: exact currency and
    provenance, finite non-negative amount, aware UTC-ish timestamps with
    ``start <= completed <= now``, and ``now - start <= max_age`` so queue
    and lock latency count against freshness. This validates the fact we
    observed; it is NOT evidence about financing or account identity.
    """
    if cash is None:
        return "strict USD cash evidence is missing; passive entry denied"
    currency = getattr(cash, "currency", None)
    if currency != PASSIVE_CASH_CURRENCY:
        return (
            f"cash evidence currency {currency!r} is not exactly "
            f"{PASSIVE_CASH_CURRENCY!r}"
        )
    provenance = getattr(cash, "provenance", None)
    if provenance != PASSIVE_CASH_PROVENANCE:
        return (
            f"cash evidence provenance {provenance!r} is not exactly "
            f"{PASSIVE_CASH_PROVENANCE!r}"
        )
    raw_amount = getattr(cash, "amount", None)
    if not isinstance(raw_amount, Decimal):
        return "cash evidence amount must be a Decimal"
    if not raw_amount.is_finite() or raw_amount < 0:
        return "cash evidence amount must be finite and non-negative"
    started = getattr(cash, "request_started_at", None)
    completed = getattr(cash, "request_completed_at", None)
    if not isinstance(started, datetime) or not isinstance(completed, datetime):
        return "cash evidence timestamps are missing or malformed"
    if started.tzinfo is None or completed.tzinfo is None:
        return "cash evidence timestamps must be timezone-aware"
    if now.tzinfo is None:
        return "validation clock must be timezone-aware"
    if completed < started:
        return "cash evidence completed before it started"
    if started > now or completed > now:
        return "cash evidence timestamps are in the future"
    if now - started > max_age:
        return (
            f"cash evidence is stale: {max((now - started).total_seconds(), 0.0):.3f}s "
            f"old exceeds the {max_age.total_seconds():.1f}s window"
        )
    if not quantity.is_finite() or quantity <= 0:
        return "cash affordability requires a positive finite quantity"
    if not approved_price.is_finite() or approved_price <= 0:
        return "cash affordability requires a positive finite price"
    if not fee.is_finite() or fee < 0:
        return "cash affordability requires a finite non-negative fee"
    required = quantity * approved_price + fee
    if raw_amount < required:
        return (
            f"cash evidence {raw_amount.normalize()} does not cover "
            f"{quantity} x {approved_price} plus fee {fee.normalize()} "
            f"(required {required.normalize()})"
        )
    return None


# --- typed protocol values --------------------------------------------------


def _positive_int_quantity(quantity: Decimal, field: str) -> int:
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError(f"{field} must be a positive integer share count")
    return int(quantity)


def _finite_price(price: Decimal, field: str) -> Decimal:
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ValueError(f"{field} must be a finite Decimal greater than zero")
    return price


@dataclass(frozen=True, slots=True)
class PassiveAttemptRef:
    """Immutable, unforgeable-by-accident reference to a reservation.

    Carries the mandate row id plus the random claim token written by the
    reservation CAS. Every later hook re-checks the token, so a ref with a
    wrong token can never mutate another owner's row.
    """

    mandate_id: int
    claim_token: str
    lane: str = PASSIVE_LANE

    def validate(self) -> str | None:
        if self.lane != PASSIVE_LANE:
            return f"attempt ref lane {self.lane!r} is not {PASSIVE_LANE!r}"
        if not self.claim_token:
            return "attempt ref carries no claim token"
        if not isinstance(self.mandate_id, int) or self.mandate_id <= 0:
            return "attempt ref carries an invalid mandate id"
        return None


@dataclass(frozen=True, slots=True)
class PassivePolicySnapshot:
    """Immutable owner-approved policy pinned at reservation time."""

    policy_version: str
    allotment_usd: Decimal
    risk_model: str
    exemptions: tuple[str, ...]
    order_binding: str
    review_interval_months: int

    def validate(self) -> str | None:
        if self.policy_version != POLICY_VERSION:
            return (
                f"policy snapshot version {self.policy_version!r} does not "
                f"match the code policy version {POLICY_VERSION!r}"
            )
        if (
            not self.allotment_usd.is_finite()
            or self.allotment_usd <= 0
            or self.allotment_usd > PASSIVE_ALLOTMENT_USD
        ):
            return (
                f"policy snapshot allotment {self.allotment_usd} must be "
                f"positive and no greater than {PASSIVE_ALLOTMENT_USD}"
            )
        if self.risk_model != RiskModel.FULL_PRINCIPAL.value:
            return f"policy snapshot risk model {self.risk_model!r} is not FULL_PRINCIPAL"
        if self.order_binding != PASSIVE_ORDER_BINDING:
            return (
                f"policy snapshot order binding {self.order_binding!r} is not "
                f"{PASSIVE_ORDER_BINDING!r}"
            )
        if self.review_interval_months != PASSIVE_REVIEW_INTERVAL_MONTHS:
            return (
                f"policy snapshot review interval {self.review_interval_months} "
                f"does not match {PASSIVE_REVIEW_INTERVAL_MONTHS}"
            )
        return None


@dataclass(frozen=True, slots=True)
class ImmutablePassiveIntent:
    """The order the reservation authorised; nothing may drift from it."""

    symbol: str
    side: str
    quantity: Decimal
    original_price: Decimal
    policy: PassivePolicySnapshot
    _qty_int: int = -1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_qty_int",
            _positive_int_quantity(self.quantity, "intent quantity"),
        )
        _finite_price(self.original_price, "intent original price")

    @property
    def quantity_int(self) -> int:
        return self.__dict__["_qty_int"]

    def validate(self) -> str | None:
        if self.symbol != PASSIVE_SYMBOL:
            return f"intent symbol {self.symbol!r} is not {PASSIVE_SYMBOL!r}"
        if self.side != "BUY":
            return f"intent side {self.side!r} is not 'BUY'"
        return self.policy.validate()

    def matches_request(self, request: PassiveOrderSpec) -> str | None:
        """None when the request is exactly the ORIGINAL intent; else drift.

        Strict on every field INCLUDING the original request price — this is
        the immutable-intent check used at policy resolution (R1-6).
        """
        if request.symbol != self.symbol:
            return (
                f"request symbol {request.symbol!r} does not match the "
                f"immutable intent symbol {self.symbol!r}"
            )
        if request.side != self.side:
            return (
                f"request side {request.side!r} does not match the "
                f"immutable intent side {self.side!r}"
            )
        if request.quantity != self.quantity:
            return (
                f"request quantity {request.quantity} does not match the "
                f"immutable intent quantity {self.quantity}"
            )
        if request.price != self.original_price:
            return (
                f"request price {request.price} does not match the immutable "
                f"intent original price {self.original_price}"
            )
        return None

    def matches_final_order(self, order: PassiveOrderSpec) -> str | None:
        """None when the FINAL approved order still executes this intent.

        The final approved price legitimately differs from the original
        request price (boundary reprices to the fresh executable price), so
        only symbol/side/quantity are immutable here; the final price is
        validated separately against cash/allotment/caps at the boundary
        and after the submit CAS (contract §6/§8, R1-6).
        """
        if order.symbol != self.symbol:
            return (
                f"final order symbol {order.symbol!r} does not match the "
                f"immutable intent symbol {self.symbol!r}"
            )
        if order.side != self.side:
            return (
                f"final order side {order.side!r} does not match the "
                f"immutable intent side {self.side!r}"
            )
        if order.quantity != self.quantity:
            return (
                f"final order quantity {order.quantity} does not match the "
                "immutable intent quantity "
                f"{self.quantity}"
            )
        return None


@dataclass(frozen=True, slots=True)
class PassiveOrderSpec:
    """A typed order description flowing through the execution layer."""

    symbol: str
    side: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class PassiveOwner:
    """Execution-ownership proof: reservation token + fresh execution token."""

    ref: PassiveAttemptRef
    execution_token: str
    intent: ImmutablePassiveIntent

    def validate(self) -> str | None:
        ref_issue = self.ref.validate()
        if ref_issue is not None:
            return ref_issue
        if not self.execution_token:
            return "owner carries no execution token"
        return self.intent.validate()


@dataclass(frozen=True, slots=True)
class ValidatedPassiveIntent:
    """Read-only ``resolve_policy`` result used by the risk boundary."""

    owner: PassiveOwner
    order: PassiveOrderSpec
    allotment_usd: Decimal

    @property
    def quantity_int(self) -> int:
        return self.owner.intent.quantity_int


@dataclass(frozen=True, slots=True)
class PassiveRejection:
    """A refusal carrying the durable reason (never an exception)."""

    reason: str


@dataclass(frozen=True, slots=True)
class PassiveOutcomeFact:
    """One observed terminal (or escalated) outcome for a submission.

    ``broker_order_id``/``broker_status``/fill observations are preserved
    best-effort; the bound broker id can never be erased or replaced.
    """

    outcome: str
    broker_order_id: str = ""
    broker_status: str = ""
    executed_quantity: Decimal | None = None
    executed_price: Decimal | None = None
    reason: str = ""

    def validate(self) -> str | None:
        if self.outcome not in _ALLOWED_OUTCOME_TRANSITIONS:
            return f"outcome {self.outcome!r} is not recognised"
        if self.outcome == SUBMIT_STATE_ORDER_KNOWN and not self.broker_order_id:
            return "ORDER_KNOWN requires a broker order id"
        return None

    def fact_signature(self) -> tuple[object, ...]:
        """The identity of the observed receipt facts (progress-aware).

        Same signature = the same fact (idempotent). A different signature
        on the same broker id is PROGRESS (new status/fill observations)
        when it does not contradict ownership, never a silent no-op — the
        row keeps the most advanced facts (R1-5c).
        """
        return (
            self.outcome,
            self.broker_order_id,
            self.broker_status,
            self.executed_quantity,
            self.executed_price,
        )


# --- outcome write results (Phase2a P3, frozen) ----------------------------


class OutcomeWriteResult(str, Enum):
    """Typed result of one ``record_outcome`` write (Phase2a P3).

    - APPLIED — the durable write landed (first bind, forward progress, or
      a burn).
    - IDEMPOTENT — the row already carried exactly this fact; no write.
    - ESCALATED_UNCERTAIN — the fact conflicted (or the row is sticky
      uncertain) and the row was escalated/preserved as UNCERTAIN; NEVER
      masquerades as success.

    IO/CAS failures remain EXCEPTIONS (``PassivePersistenceUncertain``,
    ``ValueError`` on lost races) — they are never typed results.
    """

    APPLIED = "APPLIED"
    IDEMPOTENT = "IDEMPOTENT"
    ESCALATED_UNCERTAIN = "ESCALATED_UNCERTAIN"


@runtime_checkable
class PassiveSubmitHooks(Protocol):
    """The frozen hook bundle the service injects into TradeExecutionService.

    All hooks are provided or all are absent; a partially wired bundle is
    treated as absent and every passive marker is refused. The first four
    signatures are frozen by the architect contract; ``current_gate_issue``
    and ``now`` are the fresh flag/PAPER/clock readers the same bundle must
    supply (no layer may cache a stale constructor boolean).
    """

    def begin_execution(
        self, ref: PassiveAttemptRef, execution_token: str,
    ) -> PassiveOwner | PassiveRejection: ...

    def resolve_policy(  # READ ONLY: never mutates mandate state
        self, owner: PassiveOwner, request: PassiveOrderSpec,
    ) -> ValidatedPassiveIntent | PassiveRejection: ...

    def claim_submission(
        self,
        owner: PassiveOwner,
        final_order: PassiveOrderSpec,
        cash: UsdCashEvidence,
    ) -> bool: ...

    def record_outcome(
        self, owner: PassiveOwner, fact: PassiveOutcomeFact,
    ) -> OutcomeWriteResult: ...

    def record_unresolved_reference(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None: ...

    def current_gate_issue(self) -> str | None: ...

    def now(self) -> datetime: ...

    def owner_intent_for(
        self, mandate_id: int, claim_token: str,
    ) -> ImmutablePassiveIntent | None: ...


_PASSIVE_HOOK_METHODS: Final[tuple[str, ...]] = (
    "begin_execution",
    "resolve_policy",
    "claim_submission",
    "record_outcome",
    "record_unresolved_reference",
    "current_gate_issue",
    "now",
    "owner_intent_for",
)


def passive_hooks_complete(hooks: object) -> bool:
    """True only when the hook bundle is fully wired (all-or-absent rule)."""
    return all(
        callable(getattr(hooks, name, None))
        for name in _PASSIVE_HOOK_METHODS
    )


# --- outcome classification -------------------------------------------------

_KNOWN_RECEIPT_STATUSES: Final[frozenset[str]] = frozenset(
    {"SUBMITTED", "PARTIAL_FILLED", "FILLED", "REJECTED", "CANCELLED"},
)


def classify_submit_receipt(
    *,
    broker_order_id: str,
    status: str,
) -> str:
    """Classify a broker submit receipt: ORDER_KNOWN or UNCERTAIN.

    A missing id, or any status outside the recognised set, is UNCERTAIN —
    never silently FAILED and never "no submission". REJECTED/CANCELLED are
    KNOWN terminal receipts (still consumed authorisation; a terminal
    partial fill is not "no position").
    """
    if not broker_order_id:
        return SUBMIT_STATE_UNCERTAIN
    if str(status).upper() in _KNOWN_RECEIPT_STATUSES:
        return SUBMIT_STATE_ORDER_KNOWN
    return SUBMIT_STATE_UNCERTAIN


def validate_outcome_write(
    *,
    current_state: str | None,
    bound_broker_order_id: str | None,
    fact: PassiveOutcomeFact,
    intent_quantity: Decimal,
    bound_broker_status: str | None = None,
    bound_executed_quantity: Decimal | None = None,
    bound_executed_price: Decimal | None = None,
) -> str | None:
    """Pure decision for ``record_outcome``.

    ``intent_quantity`` (the immutable owner intent bound) is REQUIRED and
    is validated on EVERY branch — including the initial SUBMITTING bind
    and UNCERTAIN/unknown observations. A fill beyond the intent is a
    conflict that escalates to UNCERTAIN with the observation preserved.

    Returns one of:

    - ``None`` — the write may proceed (first binding or a valid forward
      transition).
    - ``"IDEMPOTENT"`` — the row already carries exactly this fact; no
      write needed.
    - ``"PROGRESS"`` — the same broker id carries genuine FORWARD facts
      (strictly higher status rank or strictly greater cumulative fill);
      the caller must CAS the write against the prior facts it read.
    - ``"CONFLICT"`` — the fact contradicts the row (different id, stale/
      backwards/regressive facts, an overfill, or late doubt on a known
      row). The caller escalates to UNCERTAIN preserving every recorded
      fact — the bound id is never erased or replaced.
    - an error string — the write must be refused outright.

    Monotonicity guarantees: missing observed fields never erase recorded
    facts; cumulative fill quantity never decreases and never exceeds the
    intent bound; a terminal status never reverts to a live one; UNCERTAIN
    is sticky under all ordinary receipts (no hidden
    auto-reconciliation — an explicit reconciliation operation is out of
    Phase-1 scope).
    """
    fact_issue = fact.validate()
    if fact_issue is not None:
        return fact_issue
    if not isinstance(intent_quantity, Decimal) or (
        not intent_quantity.is_finite()
    ):
        return "intent_quantity must be a finite Decimal bound"
    if intent_quantity <= 0:
        return "intent_quantity must be a positive share count"
    observed = fact.executed_quantity
    if observed is not None and (
        not observed.is_finite() or observed < 0
    ):
        return "observed executed_quantity must be finite and non-negative"
    # Phase2a P3: the overfill bound applies on EVERY branch, including
    # the FIRST bind (SUBMITTING -> ORDER_KNOWN) and UNCERTAIN/unknown
    # observations — an overfilling first bind escalates rather than
    # becoming canonical progress.
    if observed is not None and observed > intent_quantity:
        return "CONFLICT"
    if current_state is None or current_state not in SUBMIT_STATES:
        return (
            f"mandate submit state {current_state!r} is not recognised; "
            "refusing to record an outcome"
        )
    if fact.outcome == SUBMIT_STATE_ORDER_KNOWN:
        bound = bound_broker_order_id or ""
        if bound and bound != fact.broker_order_id:
            # A different broker id for an already-bound row is a hard
            # conflict: the bound id is immutable (R1-1).
            return "CONFLICT"
        if current_state == SUBMIT_STATE_UNCERTAIN:
            # STICKY: an ordinary receipt never clears UNCERTAIN. The id
            # is preserved; resolution is an explicit reconciliation
            # operation (Phase 2+), never an automatic transition.
            if bound and bound == fact.broker_order_id:
                return "IDEMPOTENT"
            return "CONFLICT"
        if current_state == SUBMIT_STATE_ORDER_KNOWN and bound:
            comparison = compare_receipt_facts(
                fact=fact,
                bound_broker_status=bound_broker_status,
                bound_executed_quantity=bound_executed_quantity,
                bound_executed_price=bound_executed_price,
                intent_quantity=intent_quantity,
            )
            if comparison == "EXACT":
                return "IDEMPOTENT"
            if comparison == "FORWARD":
                return "PROGRESS"
            # BACKWARD: stale/regressive facts — preserve the recorded
            # proof and escalate, never a silent overwrite.
            return "CONFLICT"
    if fact.outcome == SUBMIT_STATE_UNCERTAIN:
        bound = bound_broker_order_id or ""
        if current_state == SUBMIT_STATE_UNCERTAIN:
            same_id = bound == (fact.broker_order_id or bound)
            if same_id:
                return "IDEMPOTENT"
        if current_state == SUBMIT_STATE_ORDER_KNOWN and bound:
            # Late doubt on a known row: escalate but preserve the bound id
            # and record the contradictory observation separately.
            return "CONFLICT"
    if current_state == fact.outcome:
        if fact.outcome == SUBMIT_STATE_NO_SUBMIT:
            return "IDEMPOTENT"
        if fact.outcome == SUBMIT_STATE_UNCERTAIN:
            return "IDEMPOTENT"
        return "CONFLICT"
    if (
        fact.outcome == SUBMIT_STATE_NO_SUBMIT
        and current_state == SUBMIT_STATE_UNCERTAIN
    ):
        # A denial observed after the row already became UNCERTAIN needs no
        # write — the row is already terminal-and-conservative.
        return "IDEMPOTENT"
    if current_state not in _ALLOWED_OUTCOME_TRANSITIONS[fact.outcome]:
        return (
            f"outcome {fact.outcome} is not allowed from state {current_state}"
        )
    return None


def _fill_eq(
    stored: Decimal | None,
    observed: Decimal | None,
) -> bool:
    """Fill-fact equality where missing/None matches only missing/None."""
    if stored is None or observed is None:
        return stored is None and observed is None
    return stored == observed


def _status_rank(status: str) -> int:
    """Monotonic forward rank of ordinary broker receipt statuses.

    Higher is further along the fill lifecycle; a lower-ranked receipt
    arriving after a higher-ranked one is stale/backwards and must never
    overwrite the recorded facts — INDEPENDENTLY of any quantity increase
    (a later SUBMITTED with a bigger number is still backwards).
    """
    order = {
        "": 0,
        "SUBMITTED": 1,
        "PARTIAL_FILLED": 2,
        "FILLED": 3,
        "REJECTED": 3,
        "CANCELLED": 3,
    }
    return order.get(str(status or "").upper(), -1)


def _is_terminal_status(status: str) -> bool:
    return str(status or "").upper() in {"FILLED", "REJECTED", "CANCELLED"}


def compare_receipt_facts(
    *,
    fact: PassiveOutcomeFact,
    bound_broker_status: str | None,
    bound_executed_quantity: Decimal | None,
    bound_executed_price: Decimal | None,
    intent_quantity: Decimal | None = None,
) -> str:
    """Classify an ORDER_KNOWN receipt against already-recorded facts.

    Returns one of:

    - ``"EXACT"`` — identical facts, idempotent no-op.
    - ``"FORWARD"`` — genuine positive progress: status rank strictly
      higher with no backwards fill, strictly greater cumulative fill
      quantity at a non-decreasing status, OR the FIRST positive fill
      observed on an id whose recorded facts are still missing (a coherent
      first observation is forward information, not a regression). The
      row may advance. A first EXPLICIT zero is also forward information
      at an unchanged SUBMITTED, REJECTED or CANCELLED status, provided
      any previously known price is unchanged. Missing is never zero.
    - ``"BACKWARD"`` — stale/regressive or contradictory. The existing
      proof is preserved and the row escalates to UNCERTAIN — differences
      are NOT blanket "progress".

    Independent constraints (B5):

    - Status rank never decreases, independently of any quantity rise
      (``PARTIAL_FILLED 3 -> SUBMITTED 4`` is BACKWARD).
    - A recorded TERMINAL status never changes to another status at all —
      terminal→terminal swaps and terminal→live are both BACKWARD. The
      only legal same-terminal changes are a coherent incremental fill
      or the first explicit zero at REJECTED/CANCELLED.
    - When ``intent_quantity`` (the immutable owner intent bound) is
      supplied, the cumulative fill quantity may never exceed it: a fill
      beyond the intent is BACKWARD (kept as conflicting evidence by the
      caller, never canonical progress).
    - Missing observed (NEW) fields never erase recorded (OLD) facts.
    """
    observed_status = str(fact.broker_status or "")
    stored_status = str(bound_broker_status or "")
    observed_qty = fact.executed_quantity
    stored_qty = bound_executed_quantity
    observed_price = fact.executed_price
    stored_price = bound_executed_price

    same_status = observed_status == stored_status
    same_qty = _fill_eq(stored_qty, observed_qty)
    same_price = _fill_eq(stored_price, observed_price)
    if same_status and same_qty and same_price:
        return "EXACT"

    # A recorded TERMINAL status never changes to any other status.
    if _is_terminal_status(stored_status) and not same_status:
        return "BACKWARD"

    # A recorded terminal status is never reverted to a live status.
    if _is_terminal_status(stored_status) and not _is_terminal_status(
        observed_status,
    ):
        return "BACKWARD"

    # Status rank never decreases — independently of quantity movement.
    if (
        stored_status
        and observed_status
        and _status_rank(observed_status) < _status_rank(stored_status)
    ):
        return "BACKWARD"
    # An unrecognised observed status cannot be progress.
    if observed_status and _status_rank(observed_status) < 0:
        return "BACKWARD"

    # Missing NEW facts never erase recorded OLD facts.
    if stored_qty is not None and observed_qty is None:
        return "BACKWARD"
    if stored_price is not None and observed_price is None:
        return "BACKWARD"

    # Cumulative fill quantity never decreases.
    if (
        stored_qty is not None
        and observed_qty is not None
        and observed_qty < stored_qty
    ):
        return "BACKWARD"

    # Cumulative fill may never exceed the immutable intent quantity.
    if (
        intent_quantity is not None
        and observed_qty is not None
        and observed_qty > intent_quantity
    ):
        return "BACKWARD"

    # Learning an explicit no-fill quantity is progress even when the
    # status has not changed. Keep this separate from positive first fills:
    # zero cannot prove FILLED/PARTIAL_FILLED or an unknown status.
    if (
        same_status
        and observed_status in {"SUBMITTED", "REJECTED", "CANCELLED"}
        and stored_qty is None
        and observed_qty is not None
        and observed_qty == Decimal("0")
    ):
        if stored_price is not None and observed_price != stored_price:
            return "BACKWARD"
        return "FORWARD"

    status_forward = _status_rank(observed_status) > _status_rank(
        stored_status,
    )
    fill_forward = (
        stored_qty is not None
        and observed_qty is not None
        and observed_qty > stored_qty
    )
    first_fill = (
        stored_qty is None
        and observed_qty is not None
        and observed_qty > 0
    )
    if status_forward or fill_forward or first_fill:
        # Same-status same-fill with a different price is contradictory,
        # not progress. (Applies only where BOTH prices are present; a
        # first-observed price against a missing stored price is forward.)
        if (
            same_status
            and not fill_forward
            and not first_fill
            and stored_price is not None
            and observed_price is not None
            and observed_price != stored_price
        ):
            return "BACKWARD"
        # A same-status first fill carrying a price that contradicts an
        # already-recorded price (fill fields missing together is fine,
        # price present while fill missing is not a coherent first fill)
        # is contradictory.
        if (
            same_status
            and first_fill
            and stored_price is not None
            and observed_price is not None
            and observed_price != stored_price
        ):
            return "BACKWARD"
        return "FORWARD"
    return "BACKWARD"


# --- strict JSON codecs (Decimal strings, no NaN) ---------------------------


def _decimal_to_str(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("refusing to serialise a non-finite Decimal")
    return format(value.normalize(), "f")


def _decimal_from_str(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} is not finite (NaN/Infinity forbidden)")
    return parsed


def intent_to_json(intent: ImmutablePassiveIntent) -> str:
    """Serialise the immutable intent; raises on any non-finite value."""
    import json

    payload = {
        "protocol_version": PASSIVE_PROTOCOL_VERSION,
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": _decimal_to_str(intent.quantity),
        "original_price": _decimal_to_str(intent.original_price),
        "policy": {
            "policy_version": intent.policy.policy_version,
            "allotment_usd": _decimal_to_str(intent.policy.allotment_usd),
            "risk_model": intent.policy.risk_model,
            "exemptions": list(intent.policy.exemptions),
            "order_binding": intent.policy.order_binding,
            "review_interval_months": intent.policy.review_interval_months,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def intent_from_json(raw: str | None) -> ImmutablePassiveIntent:
    """Strictly parse an intent snapshot; any defect raises ValueError."""
    import json

    if not raw:
        raise ValueError("intent snapshot is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("intent snapshot is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("intent snapshot is not an object")
    if payload.get("protocol_version") != PASSIVE_PROTOCOL_VERSION:
        raise ValueError("intent snapshot protocol version is not recognised")
    policy_payload = payload.get("policy")
    if not isinstance(policy_payload, dict):
        raise ValueError("intent snapshot policy is missing")
    exemptions = policy_payload.get("exemptions")
    if not isinstance(exemptions, list) or not all(
        isinstance(item, str) for item in exemptions
    ):
        raise ValueError("intent snapshot exemptions are malformed")
    review_interval = policy_payload.get("review_interval_months")
    if not isinstance(review_interval, int) or isinstance(review_interval, bool):
        raise ValueError("intent snapshot review interval is malformed")
    intent = ImmutablePassiveIntent(
        symbol=str(payload.get("symbol", "")),
        side=str(payload.get("side", "")),
        quantity=_decimal_from_str(payload.get("quantity"), "intent quantity"),
        original_price=_decimal_from_str(
            payload.get("original_price"), "intent original price",
        ),
        policy=PassivePolicySnapshot(
            policy_version=str(policy_payload.get("policy_version", "")),
            allotment_usd=_decimal_from_str(
                policy_payload.get("allotment_usd"), "policy allotment",
            ),
            risk_model=str(policy_payload.get("risk_model", "")),
            exemptions=tuple(exemptions),
            order_binding=str(policy_payload.get("order_binding", "")),
            review_interval_months=review_interval,
        ),
    )
    issue = intent.validate()
    if issue is not None:
        raise ValueError(f"intent snapshot is invalid: {issue}")
    return intent


def final_snapshot_to_json(
    *,
    quantity: Decimal,
    approved_price: Decimal,
    fee: Decimal,
    cash: UsdCashEvidence,
) -> str:
    """Serialise the final order/cash/fee snapshot persisted at the CAS."""
    import json

    payload = {
        "protocol_version": PASSIVE_PROTOCOL_VERSION,
        "quantity": _decimal_to_str(quantity),
        "approved_price": _decimal_to_str(approved_price),
        "sec98_fee": _decimal_to_str(fee),
        "cash_evidence": {
            "amount": _decimal_to_str(cash.amount),
            "currency": str(cash.currency),
            "request_started_at": cash.request_started_at.isoformat(),
            "request_completed_at": cash.request_completed_at.isoformat(),
            "provenance": str(cash.provenance),
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# --- legacy migration classification (pure) ---------------------------------


def legacy_row_may_remain_authorized(
    *,
    submit_state: str | None,
    entry_authorisation_available: bool | None,
    claim_token: str | None,
    entry_authorisation_consumed_at: object | None,
    bound_broker_order_id: str | None,
    execution_token: str | None,
    intent_json: str | None,
    final_snapshot_json: str | None,
    uncertainty_reason: str | None,
    status: str | None,
    policy_version: str | None,
    allotment_usd: object | None,
) -> str | None:
    """None when a legacy row is CLEARLY unspent and may stay AUTHORIZED.

    Otherwise returns the disqualifying facts (joined) — the migration then
    marks the row UNCERTAIN and retains every existing value. Never infer a
    safe lifecycle solely from a broker id; never reauthorise consumed rows.
    Duplicate-lane/schema ambiguity must fail closed (UNCERTAIN).
    """
    issues: list[str] = []
    if entry_authorisation_available is not True:
        issues.append(f"available={entry_authorisation_available!r}")
    if claim_token:
        issues.append("claim_token present")
    if entry_authorisation_consumed_at is not None:
        issues.append("consumed_at present")
    if bound_broker_order_id:
        issues.append("bound_broker_order_id present")
    if execution_token:
        issues.append("execution_token present")
    if intent_json or final_snapshot_json or uncertainty_reason:
        issues.append("protocol-v2 snapshot columns present")
    if submit_state != SUBMIT_STATE_AUTHORIZED:
        # None / legacy used states / unknown values are all uncertain.
        issues.append(f"submit_state={submit_state!r}")
    if status != "ACTIVE":
        issues.append(f"status={status!r}")
    if policy_version != POLICY_VERSION:
        issues.append(f"policy_version={policy_version!r}")
    try:
        allotment = Decimal(str(allotment_usd))
    except (InvalidOperation, TypeError, ValueError):
        allotment = Decimal("NaN")
    if not allotment.is_finite() or allotment <= 0 or allotment > PASSIVE_ALLOTMENT_USD:
        issues.append(f"allotment_usd={allotment_usd!r}")
    if issues:
        return "; ".join(issues)
    return None
