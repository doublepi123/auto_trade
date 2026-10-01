"""Pure identity comparison; caller classifications are not authenticated facts.

Account and submission scopes must already be supplied by the caller. A
submission scope denotes one submission lifecycle, not a callback retry.
No verdict authorizes an action or establishes broker provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


class TimestampPrecision(str, Enum):
    UNKNOWN = "UNKNOWN"
    SECOND = "SECOND"
    MILLISECOND = "MILLISECOND"
    MICROSECOND = "MICROSECOND"


class TimestampProvenance(str, Enum):
    UNKNOWN = "UNKNOWN"
    BROKER_SUBMITTED_AT = "BROKER_SUBMITTED_AT"
    LOCAL_TIME = "LOCAL_TIME"
    FILL_TIME = "FILL_TIME"
    OWNER_ASSERTION = "OWNER_ASSERTION"


class IdentityVerdict(str, Enum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    UNPROVEN = "UNPROVEN"


class IdentityReason(str, Enum):
    IDENTITY_MISSING = "IDENTITY_MISSING"
    IDENTITY_TYPE_INVALID = "IDENTITY_TYPE_INVALID"
    BROKER_ORDER_ID_UNPROVEN = "BROKER_ORDER_ID_UNPROVEN"
    ACCOUNT_SCOPE_UNPROVEN = "ACCOUNT_SCOPE_UNPROVEN"
    SUBMISSION_SCOPE_UNPROVEN = "SUBMISSION_SCOPE_UNPROVEN"
    TIMESTAMP_SOURCE_UNPROVEN = "TIMESTAMP_SOURCE_UNPROVEN"
    TIME_PRECISION_UNPROVEN = "TIME_PRECISION_UNPROVEN"
    TIMESTAMP_UNPROVEN = "TIMESTAMP_UNPROVEN"
    PRECISION_ALIGNMENT_INVALID = "PRECISION_ALIGNMENT_INVALID"
    PRECISION_NOT_COMPARABLE = "PRECISION_NOT_COMPARABLE"
    BROKER_ORDER_ID_CONFLICT = "BROKER_ORDER_ID_CONFLICT"
    ACCOUNT_SCOPE_CONFLICT = "ACCOUNT_SCOPE_CONFLICT"
    SUBMISSION_SCOPE_CONFLICT = "SUBMISSION_SCOPE_CONFLICT"
    BROKER_SUBMITTED_AT_CONFLICT = "BROKER_SUBMITTED_AT_CONFLICT"
    IDENTITY_MATCH = "IDENTITY_MATCH"


@dataclass(frozen=True, slots=True)
class SettlementIdentity:
    broker_order_id: str | None
    account_scope: str | None
    submission_scope: str | None
    broker_submitted_at: datetime | None
    precision: TimestampPrecision | None
    provenance: TimestampProvenance | None


@dataclass(frozen=True, slots=True)
class IdentityComparison:
    verdict: IdentityVerdict
    reason_codes: tuple[IdentityReason, ...]
    report_scope: Literal["PURE_IDENTITY_NON_AUTHORIZING"] = field(
        default="PURE_IDENTITY_NON_AUTHORIZING", init=False,
    )
    evidence_basis: Literal["CALLER_SUPPLIED_CLASSIFICATION"] = field(
        default="CALLER_SUPPLIED_CLASSIFICATION", init=False,
    )
    authorizes_actions: Literal[False] = field(default=False, init=False)


def _validate_identity(
    identity: SettlementIdentity | None,
    reasons: set[IdentityReason],
) -> tuple[datetime | None, TimestampPrecision | None]:
    if identity is None:
        reasons.add(IdentityReason.IDENTITY_MISSING)
        return None, None
    if type(identity) is not SettlementIdentity:
        reasons.add(IdentityReason.IDENTITY_TYPE_INVALID)
        return None, None

    for value, reason in (
        (identity.broker_order_id, IdentityReason.BROKER_ORDER_ID_UNPROVEN),
        (identity.account_scope, IdentityReason.ACCOUNT_SCOPE_UNPROVEN),
        (identity.submission_scope, IdentityReason.SUBMISSION_SCOPE_UNPROVEN),
    ):
        if type(value) is not str or not 1 <= len(value) <= 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
            reasons.add(reason)

    if type(identity.provenance) is not TimestampProvenance or identity.provenance is not TimestampProvenance.BROKER_SUBMITTED_AT:
        reasons.add(IdentityReason.TIMESTAMP_SOURCE_UNPROVEN)
    precision = identity.precision
    if type(precision) is not TimestampPrecision or precision is TimestampPrecision.UNKNOWN:
        reasons.add(IdentityReason.TIME_PRECISION_UNPROVEN)
        precision = None

    raw = identity.broker_submitted_at
    if type(raw) is not datetime or raw.tzinfo is None:
        reasons.add(IdentityReason.TIMESTAMP_UNPROVEN)
        return None, precision
    try:
        # Read the caller's tzinfo exactly once. Explicit offset subtraction
        # avoids local-zone inference and a second, potentially stateful hook.
        offset = raw.utcoffset()
        if offset is None:
            reasons.add(IdentityReason.TIMESTAMP_UNPROVEN)
            return None, precision
        utc = (raw.replace(tzinfo=None) - offset).replace(tzinfo=timezone.utc)
    except Exception:
        # Arbitrary tzinfo implementations can fail; never disclose their text.
        reasons.add(IdentityReason.TIMESTAMP_UNPROVEN)
        return None, precision

    if precision is TimestampPrecision.SECOND:
        if raw.microsecond != 0 or utc.microsecond != 0:
            reasons.add(IdentityReason.PRECISION_ALIGNMENT_INVALID)
    elif precision is TimestampPrecision.MILLISECOND:
        if raw.microsecond % 1000 != 0 or utc.microsecond % 1000 != 0:
            reasons.add(IdentityReason.PRECISION_ALIGNMENT_INVALID)
    return utc, precision


def compare_settlement_identity(
    stored: SettlementIdentity | None,
    incoming: SettlementIdentity | None,
) -> IdentityComparison:
    """Compare complete caller-classified identities, never authorize actions."""
    reasons: set[IdentityReason] = set()
    stored_utc, stored_precision = _validate_identity(stored, reasons)
    incoming_utc, incoming_precision = _validate_identity(incoming, reasons)
    if stored_precision is not None and incoming_precision is not None and stored_precision is not incoming_precision:
        reasons.add(IdentityReason.PRECISION_NOT_COMPARABLE)
    if reasons:
        return IdentityComparison(
            IdentityVerdict.UNPROVEN,
            tuple(reason for reason in IdentityReason if reason in reasons),
        )

    # Reached only after both exact-type identities and their times validated.
    assert stored is not None and incoming is not None
    for left, right, reason in (
        (stored.broker_order_id, incoming.broker_order_id, IdentityReason.BROKER_ORDER_ID_CONFLICT),
        (stored.account_scope, incoming.account_scope, IdentityReason.ACCOUNT_SCOPE_CONFLICT),
        (stored.submission_scope, incoming.submission_scope, IdentityReason.SUBMISSION_SCOPE_CONFLICT),
        (stored_utc, incoming_utc, IdentityReason.BROKER_SUBMITTED_AT_CONFLICT),
    ):
        if left != right:
            reasons.add(reason)
    if reasons:
        return IdentityComparison(
            IdentityVerdict.CONFLICT,
            tuple(reason for reason in IdentityReason if reason in reasons),
        )
    return IdentityComparison(IdentityVerdict.MATCH, (IdentityReason.IDENTITY_MATCH,))
