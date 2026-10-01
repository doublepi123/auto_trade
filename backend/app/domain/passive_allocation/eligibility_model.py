"""Offline caller assumptions and non-authorizing diagnostics, never broker facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


class Environment(str, Enum):
    PAPER = "PAPER"
    FUNDED = "FUNDED"
    UNKNOWN = "UNKNOWN"


class DebtStatus(str, Enum):
    NONE_REPORTED = "NONE_REPORTED"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class ReceiptTrust(str, Enum):
    VERIFIED = "VERIFIED"
    REVOKED = "REVOKED"
    UNVERIFIABLE = "UNVERIFIABLE"


class Status(str, Enum):
    INVALID = "INVALID"
    MISSING = "MISSING"
    REVOKED = "REVOKED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    STALE = "STALE"
    INELIGIBLE = "INELIGIBLE"
    FUNDING_UNKNOWN = "FUNDING_UNKNOWN"
    PRICE_CHANGED = "PRICE_CHANGED"
    INSUFFICIENT = "INSUFFICIENT"
    UNVERIFIABLE = "UNVERIFIABLE"
    EVIDENCE_READY_OFF = "EVIDENCE_READY_OFF"


class Reason(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_AS_OF = "INVALID_AS_OF"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    INVALID_JSON = "INVALID_JSON"
    EXCESSIVE_DEPTH = "EXCESSIVE_DEPTH"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_VERSION = "INVALID_VERSION"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    RECEIPT_REVOKED = "RECEIPT_REVOKED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    STALE_ATTESTATION = "STALE_ATTESTATION"
    STALE_WINDOW = "STALE_WINDOW"
    STALE_DEBT = "STALE_DEBT"
    ENVIRONMENT_INELIGIBLE = "ENVIRONMENT_INELIGIBLE"
    ORDER_INELIGIBLE = "ORDER_INELIGIBLE"
    DEBT_PRESENT = "DEBT_PRESENT"
    RISK_INELIGIBLE = "RISK_INELIGIBLE"
    FUNDING_UNKNOWN = "FUNDING_UNKNOWN"
    PRICE_CHANGED = "PRICE_CHANGED"
    INSUFFICIENT_QUANTITY = "INSUFFICIENT_QUANTITY"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    CAP_EXCEEDED = "CAP_EXCEEDED"
    NO_TRUSTED_VERIFIER = "NO_TRUSTED_VERIFIER"
    VERIFIER_FAILED = "VERIFIER_FAILED"
    PROOF_MISMATCH = "PROOF_MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True, slots=True)
class ExpectedBinding:
    generation: int
    credential_fingerprint: str
    context_digest: str
    client_instance_id: str
    account_ref: str


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    binding: ExpectedBinding
    request_started_at: datetime
    request_completed_at: datetime


@dataclass(frozen=True, slots=True)
class AttestationClaim:
    source_version: str
    generation: int
    credential_fingerprint: str
    context_digest: str
    account_ref: str
    environment: Environment
    observed_at: datetime
    expires_at: datetime
    revoked: bool | None
    evidence_ref: str | None


@dataclass(frozen=True, slots=True)
class CashClaim:
    window: ObservationWindow
    currency: str
    available_cash: Decimal | None


@dataclass(frozen=True, slots=True)
class PurchaseEstimateClaim:
    window: ObservationWindow
    symbol: str
    side: str
    order_type: str
    currency: str
    price: Decimal | None
    cash_max_qty: Decimal | None


@dataclass(frozen=True, slots=True)
class FundingClaims:
    source_version: str
    cash: CashClaim | None
    estimate: PurchaseEstimateClaim | None
    fee_upper: Decimal | None
    fee_evidence_ref: str | None
    debt_status: DebtStatus
    debt_evidence_ref: str | None
    debt_observed_at: datetime | None
    risk_level: int | None


@dataclass(frozen=True, slots=True)
class OrderCandidate:
    symbol: str
    side: str
    order_type: str
    currency: str
    quantity: int
    final_price: Decimal


@dataclass(frozen=True, slots=True)
class EligibilityInput:
    schema_version: str
    expected_binding: ExpectedBinding | None
    attestation: AttestationClaim | None
    funding: FundingClaims | None
    order: OrderCandidate | None


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    attestation_digest: str
    funding_digest: str
    order_digest: str
    expected_binding: ExpectedBinding
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    trust: ReceiptTrust
    attestation_digest: str | None = None
    funding_digest: str | None = None
    order_digest: str | None = None
    binding: ExpectedBinding | None = None
    environment: Environment | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    fee_upper: Decimal | None = None
    fee_evidence_ref: str | None = None
    debt_status: DebtStatus | None = None
    debt_evidence_ref: str | None = None
    debt_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InputError:
    code: Reason


@dataclass(frozen=True, slots=True)
class EligibilityReport:
    status: Status
    reasons: tuple[Reason, ...]
    attestation_digest: str | None = None
    funding_digest: str | None = None
    order_digest: str | None = None
    orders_authorized: Literal[False] = field(default=False, init=False)
    report_scope: Literal["OFFLINE_NON_AUTHORIZING"] = field(default="OFFLINE_NON_AUTHORIZING", init=False)
    binding_basis: Literal["CALLER_SUPPLIED_ASSUMPTION"] = field(default="CALLER_SUPPLIED_ASSUMPTION", init=False)
    clock_basis: Literal["CALLER_SUPPLIED_AS_OF"] = field(default="CALLER_SUPPLIED_AS_OF", init=False)
