"""Offline diagnostics only; no order authorization."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from .eligibility_codec import canonical_bytes, claim_digest, valid_time
from .eligibility_model import DebtStatus, Environment, EligibilityInput, EligibilityReport, ReceiptTrust, ReceiptVerification, VerificationRequest, Reason, Status


def _category(reason: Reason) -> Status:
    if reason in (Reason.INVALID_INPUT, Reason.INVALID_AS_OF):
        return Status.INVALID
    if reason is Reason.MISSING_EVIDENCE:
        return Status.MISSING
    if reason is Reason.RECEIPT_REVOKED:
        return Status.REVOKED
    if reason is Reason.BINDING_MISMATCH:
        return Status.BINDING_MISMATCH
    if reason in (Reason.STALE_ATTESTATION, Reason.STALE_WINDOW, Reason.STALE_DEBT):
        return Status.STALE
    if reason in (Reason.ENVIRONMENT_INELIGIBLE, Reason.ORDER_INELIGIBLE, Reason.DEBT_PRESENT, Reason.RISK_INELIGIBLE):
        return Status.INELIGIBLE
    if reason is Reason.FUNDING_UNKNOWN:
        return Status.FUNDING_UNKNOWN
    if reason is Reason.PRICE_CHANGED:
        return Status.PRICE_CHANGED
    if reason in (Reason.INSUFFICIENT_QUANTITY, Reason.INSUFFICIENT_CASH, Reason.CAP_EXCEEDED):
        return Status.INSUFFICIENT
    return Status.UNVERIFIABLE


def evaluate(evidence: EligibilityInput, *, now: datetime,
             trusted_receipt_verifier: Callable[[VerificationRequest], ReceiptVerification] | None = None) -> EligibilityReport:
    reasons: set[Reason] = set()
    digests: list[str | None] = [None, None, None]

    def report() -> EligibilityReport:
        ordered = tuple(r for r in Reason if r in reasons)
        priority = list(Status)
        status = min((_category(r) for r in ordered), key=priority.index, default=Status.EVIDENCE_READY_OFF)
        return EligibilityReport(status, ordered, *digests)

    if trusted_receipt_verifier is None:
        reasons.add(Reason.NO_TRUSTED_VERIFIER)
    if not valid_time(now):
        reasons.add(Reason.INVALID_AS_OF)
    try:
        if type(evidence) is not EligibilityInput:
            raise ValueError()
        canonical_bytes(evidence)
    except (ValueError, TypeError, AttributeError, OverflowError):
        reasons.add(Reason.INVALID_INPUT)
        return report()
    binding, att, fund, order = evidence.expected_binding, evidence.attestation, evidence.funding, evidence.order
    for index, (kind, value) in enumerate((("attestation", att), ("funding", fund), ("order", order))):
        if value is not None:
            digests[index] = claim_digest(kind, value)
    if any(value is None for value in (binding, att, fund, order)):
        reasons.add(Reason.MISSING_EVIDENCE)
    clock_valid = valid_time(now)
    if att is not None:
        if att.revoked is True:
            reasons.add(Reason.RECEIPT_REVOKED)
        if att.revoked is None or att.evidence_ref is None or att.environment is Environment.UNKNOWN:
            reasons.add(Reason.UNVERIFIABLE)
        if att.environment is Environment.FUNDED:
            reasons.add(Reason.ENVIRONMENT_INELIGIBLE)
        if binding and any(getattr(att, key) != getattr(binding, key) for key in ("generation", "credential_fingerprint", "context_digest", "account_ref")):
            reasons.add(Reason.BINDING_MISMATCH)
        if clock_valid and not (att.observed_at <= now < att.expires_at and timedelta(0) < att.expires_at-att.observed_at <= timedelta(days=1)):
            reasons.add(Reason.STALE_ATTESTATION)
    if order is not None and ((order.symbol, order.side, order.order_type, order.currency) != ("SPY.US", "BUY", "LO", "USD") or not 1 <= order.quantity <= 100 or order.final_price <= 0):
        reasons.add(Reason.ORDER_INELIGIBLE)
    if fund is not None:
        if fund.risk_level is None or fund.debt_status is DebtStatus.UNKNOWN or fund.debt_evidence_ref is None or fund.debt_observed_at is None or fund.fee_upper is None or fund.fee_evidence_ref is None or fund.cash is None or fund.estimate is None:
            reasons.add(Reason.FUNDING_UNKNOWN)
        if fund.risk_level is not None and fund.risk_level != 0:
            reasons.add(Reason.RISK_INELIGIBLE)
        if fund.debt_status is DebtStatus.PRESENT:
            reasons.add(Reason.DEBT_PRESENT)
        if fund.fee_upper is not None and fund.fee_upper < 0:
            reasons.add(Reason.FUNDING_UNKNOWN)
        if clock_valid and fund.debt_observed_at is not None and not timedelta(0) <= now-fund.debt_observed_at <= timedelta(days=1):
            reasons.add(Reason.STALE_DEBT)
        for claim in (fund.cash, fund.estimate):
            if claim is None:
                continue
            window = claim.window
            if binding is not None and window.binding != binding:
                reasons.add(Reason.BINDING_MISMATCH)
            if clock_valid and not (window.request_started_at <= window.request_completed_at <= now and now-window.request_started_at <= timedelta(seconds=5)):
                reasons.add(Reason.STALE_WINDOW)
            if claim.currency != "USD":
                reasons.add(Reason.ORDER_INELIGIBLE)
        if fund.cash is not None and (fund.cash.available_cash is None or fund.cash.available_cash < 0):
            reasons.add(Reason.FUNDING_UNKNOWN)
        if fund.estimate is not None:
            estimate = fund.estimate
            if estimate.price is None or estimate.cash_max_qty is None or (estimate.price is not None and estimate.price <= 0) or (estimate.cash_max_qty is not None and estimate.cash_max_qty < 0):
                reasons.add(Reason.FUNDING_UNKNOWN)
            if (estimate.symbol, estimate.side, estimate.order_type, estimate.currency) != ("SPY.US", "BUY", "LO", "USD"):
                reasons.add(Reason.ORDER_INELIGIBLE)
            if order and estimate.price is not None and order.final_price != estimate.price:
                reasons.add(Reason.PRICE_CHANGED)
            if order and estimate.cash_max_qty is not None and order.quantity > estimate.cash_max_qty.to_integral_value(rounding=ROUND_FLOOR):
                reasons.add(Reason.INSUFFICIENT_QUANTITY)

    verified: ReceiptVerification | None = None
    if trusted_receipt_verifier is not None:
        if binding is None or att is None or fund is None or order is None or not clock_valid:
            reasons.add(Reason.UNVERIFIABLE)
        else:
            request = VerificationRequest(claim_digest("attestation", att), claim_digest("funding", fund), claim_digest("order", order), binding, now)
            try:
                proof = trusted_receipt_verifier(request)
                if type(proof) is not ReceiptVerification or type(proof.trust) is not ReceiptTrust:
                    raise ValueError()
                canonical_bytes(proof)
                if proof.trust is ReceiptTrust.REVOKED:
                    reasons.add(Reason.RECEIPT_REVOKED)
                elif proof.trust is ReceiptTrust.UNVERIFIABLE:
                    reasons.add(Reason.UNVERIFIABLE)
                else:
                    expected = ReceiptVerification(ReceiptTrust.VERIFIED, request.attestation_digest, request.funding_digest, request.order_digest, binding, att.environment, att.observed_at, att.expires_at, fund.fee_upper, fund.fee_evidence_ref, fund.debt_status, fund.debt_evidence_ref, fund.debt_observed_at)
                    if proof != expected or any(getattr(proof, name) is None for name in ("fee_upper", "fee_evidence_ref", "debt_status", "debt_evidence_ref", "debt_observed_at")):
                        reasons.add(Reason.PROOF_MISMATCH)
                    else:
                        verified = proof
            except Exception:
                # Trusted dependency failures are deliberately sanitized.
                reasons.add(Reason.VERIFIER_FAILED)
    if verified is not None and verified.fee_upper is not None and fund is not None and order is not None:
        # Construct a fresh context, not a copy of process precision/traps.
        with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
            total = Decimal(order.quantity) * order.final_price + verified.fee_upper
            if total > Decimal(5000):
                reasons.add(Reason.CAP_EXCEEDED)
            if fund.cash is not None and fund.cash.available_cash is not None and total > fund.cash.available_cash:
                reasons.add(Reason.INSUFFICIENT_CASH)
    return report()
