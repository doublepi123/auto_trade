"""Synthetic offline cases; no live identity or receipt source is implemented."""
from __future__ import annotations

import ast
import json
import hashlib
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from typing import cast

import pytest

from app.domain.passive_allocation.eligibility import evaluate
from app.domain.passive_allocation.eligibility_codec import canonical_bytes, claim_digest, parse_input
from app.domain.passive_allocation.eligibility_model import (
    AttestationClaim, CashClaim, DebtStatus, EligibilityInput, Environment,
    ExpectedBinding, FundingClaims, InputError, ObservationWindow, OrderCandidate,
    PurchaseEstimateClaim, Reason, ReceiptTrust, ReceiptVerification, Status, VerificationRequest,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BINDING = ExpectedBinding(3, "a" * 64, "b" * 64, "00000000-0000-4000-8000-000000000001", "synthetic-only")
WINDOW = ObservationWindow(BINDING, NOW - timedelta(seconds=5), NOW)
ATT = AttestationClaim("offline-account-attestation-v1", 3, "a" * 64, "b" * 64, "synthetic-only", Environment.PAPER, NOW - timedelta(hours=1), NOW + timedelta(hours=1), False, "c" * 64)
FUND = FundingClaims("offline-funding-claims-v1", CashClaim(WINDOW, "USD", Decimal("5000")), PurchaseEstimateClaim(WINDOW, "SPY.US", "BUY", "LO", "USD", Decimal("499.9"), Decimal("10.9")), Decimal("1"), "d" * 64, DebtStatus.NONE_REPORTED, "e" * 64, NOW, 0)
ORDER = OrderCandidate("SPY.US", "BUY", "LO", "USD", 10, Decimal("499.9"))
INPUT = EligibilityInput("passive-eligibility-input-v1", BINDING, ATT, FUND, ORDER)


def _raw_input() -> bytes:
    def encode(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError("unsupported synthetic fixture")
    return json.dumps(asdict(INPUT), default=encode).encode()


class _FakeRegistry:
    """Prebuilt synthetic receipt: requests never manufacture trusted facts."""
    def __init__(self, evidence: EligibilityInput = INPUT):
        assert evidence.attestation and evidence.funding and evidence.order and evidence.expected_binding
        a, f = evidence.attestation, evidence.funding
        self.receipt = ReceiptVerification(ReceiptTrust.VERIFIED,
            claim_digest("attestation", a), claim_digest("funding", f), claim_digest("order", evidence.order),
            evidence.expected_binding, a.environment, a.observed_at, a.expires_at,
            f.fee_upper, f.fee_evidence_ref, f.debt_status, f.debt_evidence_ref, f.debt_observed_at)

    def __call__(self, request: VerificationRequest) -> ReceiptVerification:
        if self.receipt.trust is not ReceiptTrust.VERIFIED or request.attestation_digest == self.receipt.attestation_digest:
            return self.receipt
        return ReceiptVerification(ReceiptTrust.UNVERIFIABLE)


def test_positive_and_default():
    result = evaluate(INPUT, now=NOW, trusted_receipt_verifier=_FakeRegistry())
    assert result.status is Status.EVIDENCE_READY_OFF
    assert result.reasons == ()
    assert result.orders_authorized is False
    assert evaluate(INPUT, now=NOW).reasons == (Reason.NO_TRUSTED_VERIFIER,)
    assert evaluate(INPUT, now=NOW).status is Status.UNVERIFIABLE
    assert parse_input(canonical_bytes(INPUT)) == INPUT
    public = json.dumps(asdict(result))
    for secret in (BINDING.account_ref, BINDING.credential_fingerprint, ATT.evidence_ref):
        assert secret and secret not in public


@pytest.mark.parametrize("field,value,reason", [
    ("quantity", 0, Reason.ORDER_INELIGIBLE), ("quantity", 101, Reason.ORDER_INELIGIBLE),
    ("quantity", True, Reason.INVALID_INPUT), ("quantity", Decimal("1.5"), Reason.INVALID_INPUT),
    ("symbol", "QQQ.US", Reason.ORDER_INELIGIBLE), ("side", "SELL", Reason.ORDER_INELIGIBLE),
    ("order_type", "MO", Reason.ORDER_INELIGIBLE), ("currency", "HKD", Reason.ORDER_INELIGIBLE),
    ("final_price", Decimal("499"), Reason.PRICE_CHANGED), ("final_price", Decimal("0"), Reason.ORDER_INELIGIBLE),
])
def test_order_denials(field, value, reason):
    result = evaluate(replace(INPUT, order=replace(ORDER, **{field: value})), now=NOW)
    assert reason in result.reasons
    assert Reason.NO_TRUSTED_VERIFIER in result.reasons
    assert not result.orders_authorized


@pytest.mark.parametrize("field,value", [("generation", 1), ("credential_fingerprint", "f" * 64), ("context_digest", "f" * 64), ("account_ref", "other"), ("client_instance_id", "00000000-0000-4000-8000-000000000002")])
def test_binding(field, value):
    e = replace(INPUT, expected_binding=replace(BINDING, **{field: value}))
    assert Reason.BINDING_MISMATCH in evaluate(e, now=NOW).reasons


@pytest.mark.parametrize("att,reason", [
    (replace(ATT, revoked=True), Reason.RECEIPT_REVOKED),
    (replace(ATT, environment=Environment.FUNDED), Reason.ENVIRONMENT_INELIGIBLE),
    (replace(ATT, environment=Environment.UNKNOWN), Reason.UNVERIFIABLE),
    (replace(ATT, observed_at=NOW + timedelta(seconds=1)), Reason.STALE_ATTESTATION),
    (replace(ATT, expires_at=NOW), Reason.STALE_ATTESTATION),
    (replace(ATT, expires_at=NOW + timedelta(days=1)), Reason.STALE_ATTESTATION),
    (replace(ATT, revoked=None), Reason.UNVERIFIABLE),
])
def test_attestation(att, reason):
    assert reason in evaluate(replace(INPUT, attestation=att), now=NOW).reasons


@pytest.mark.parametrize("fund,reason", [
    (replace(FUND, cash=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, estimate=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, fee_upper=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, fee_evidence_ref=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, debt_evidence_ref=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, debt_status=DebtStatus.UNKNOWN), Reason.FUNDING_UNKNOWN),
    (replace(FUND, debt_status=DebtStatus.PRESENT), Reason.DEBT_PRESENT),
    (replace(FUND, risk_level=None), Reason.FUNDING_UNKNOWN),
    (replace(FUND, risk_level=1), Reason.RISK_INELIGIBLE),
    (replace(FUND, debt_observed_at=NOW + timedelta(microseconds=1)), Reason.STALE_DEBT),
    (replace(FUND, debt_observed_at=NOW - timedelta(days=1, microseconds=1)), Reason.STALE_DEBT),
    (replace(FUND, cash=CashClaim(replace(WINDOW, request_started_at=NOW-timedelta(seconds=5, microseconds=1)), "USD", Decimal(5000))), Reason.STALE_WINDOW),
    (replace(FUND, cash=CashClaim(replace(WINDOW, request_completed_at=NOW+timedelta(seconds=1)), "USD", Decimal(5000))), Reason.STALE_WINDOW),
    (replace(FUND, cash=CashClaim(WINDOW, "USD", Decimal("4999.999999"))), Reason.INSUFFICIENT_CASH),
    (replace(FUND, fee_upper=Decimal("1.000001")), Reason.CAP_EXCEEDED),
    (replace(FUND, estimate=PurchaseEstimateClaim(WINDOW, "SPY.US", "BUY", "LO", "USD", Decimal("499.9"), Decimal("9.999999"))), Reason.INSUFFICIENT_QUANTITY),
])
def test_funding(fund, reason):
    evidence = replace(INPUT, funding=fund)
    result = evaluate(evidence, now=NOW, trusted_receipt_verifier=_FakeRegistry(evidence))
    assert reason in result.reasons
    assert not result.orders_authorized


@pytest.mark.parametrize("result", [True, {}, "VERIFIED", None, ReceiptVerification(cast(ReceiptTrust, "VERIFIED")), ReceiptVerification(ReceiptTrust.VERIFIED)])
def test_bad_verifier(result):
    def verifier(request: VerificationRequest) -> ReceiptVerification:
        return cast(ReceiptVerification, result)
    report = evaluate(INPUT, now=NOW, trusted_receipt_verifier=verifier)
    assert report.status is Status.UNVERIFIABLE
    assert set(report.reasons) & {Reason.VERIFIER_FAILED, Reason.PROOF_MISMATCH}


@pytest.mark.parametrize("field,value", [("funding_digest", "0"*64), ("order_digest", "0"*64), ("binding", replace(BINDING, generation=1)), ("environment", Environment.FUNDED), ("observed_at", NOW), ("expires_at", NOW), ("fee_upper", Decimal("0")), ("fee_evidence_ref", "f"*64), ("debt_status", DebtStatus.PRESENT), ("debt_evidence_ref", "f"*64), ("debt_observed_at", NOW-timedelta(seconds=1)), ("fee_upper", Decimal("1E+999999"))])
def test_proof_mismatch(field, value):
    registry = _FakeRegistry()
    registry.receipt = replace(registry.receipt, **{field: value})
    report = evaluate(INPUT, now=NOW, trusted_receipt_verifier=registry)
    assert report.status is Status.UNVERIFIABLE
    assert set(report.reasons) & {Reason.PROOF_MISMATCH, Reason.VERIFIER_FAILED}


def test_exception_revocation_priority_context():
    def broken(request: VerificationRequest) -> ReceiptVerification:
        raise ValueError("secret-fixture-must-not-escape")
    assert evaluate(INPUT, now=NOW, trusted_receipt_verifier=broken).reasons == (Reason.VERIFIER_FAILED,)
    registry = _FakeRegistry()
    registry.receipt = ReceiptVerification(ReceiptTrust.REVOKED)
    assert evaluate(INPUT, now=NOW, trusted_receipt_verifier=registry).status is Status.REVOKED
    report = evaluate(replace(INPUT, attestation=replace(ATT, revoked=True), order=None), now=NOW)
    assert report.status is Status.MISSING
    assert report.reasons == (Reason.MISSING_EVIDENCE, Reason.RECEIPT_REVOKED, Reason.NO_TRUSTED_VERIFIER)
    assert Reason.INVALID_AS_OF in evaluate(INPUT, now=NOW.replace(tzinfo=None)).reasons
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert evaluate(INPUT, now=NOW, trusted_receipt_verifier=_FakeRegistry()).status is Status.EVIDENCE_READY_OFF


@pytest.mark.parametrize("raw,code", [
    (b"x"*65537, Reason.INPUT_TOO_LARGE), (b"\xff", Reason.INVALID_JSON),
    (b'{"x":1,"x":2}', Reason.DUPLICATE_KEY),
    (b'[[[[[[[[[0]]]]]]]]]', Reason.EXCESSIVE_DEPTH),
    (b'{"schema_version":"bad"}', Reason.INVALID_VERSION),
    (b'{"schema_version":1.0}', Reason.INVALID_JSON),
    (b'{"schema_version":NaN}', Reason.INVALID_JSON),
    (b'{"schema_version":Infinity}', Reason.INVALID_JSON),
    (b'{"secret":"never-print"}', Reason.UNKNOWN_FIELD),
    (b'{}', Reason.MISSING_FIELD),
])
def test_codec_errors(raw, code):
    assert parse_input(raw) == InputError(code)


@pytest.mark.parametrize("value", ["1e1", " 1", "+1", "01", "1.0000001", "1000000000000000000", "NaN", 1, True, 1.5])
def test_decimal_codec(value):
    data = json.loads(_raw_input())
    data["order"]["final_price"] = value
    assert isinstance(parse_input(json.dumps(data).encode()), InputError)


def test_canonical_and_missing():
    assert parse_input(b'{"schema_version":"passive-eligibility-input-v1"}') == EligibilityInput(INPUT.schema_version, None, None, None, None)
    data = json.loads(_raw_input())
    data["order"]["final_price"] = "499.900000"
    data["attestation"]["observed_at"] = "2025-12-31T23:00:00Z"
    decoded = parse_input(json.dumps(data, sort_keys=True).encode())
    assert decoded == INPUT
    assert canonical_bytes(decoded) == canonical_bytes(INPUT)
    assert claim_digest("order", ORDER) != claim_digest("order", replace(ORDER, quantity=9))
    del data["attestation"]["revoked"]
    assert parse_input(json.dumps(data).encode()) == InputError(Reason.MISSING_FIELD)
    data["verified"] = True
    assert parse_input(json.dumps(data).encode()) == InputError(Reason.UNKNOWN_FIELD)


@pytest.mark.parametrize("segment,key,value", [
    ("expected_binding", "generation", True), ("expected_binding", "generation", -1),
    ("expected_binding", "client_instance_id", "not-uuid"),
    ("expected_binding", "credential_fingerprint", "A" * 64),
    ("expected_binding", "account_ref", "a" * 65),
    ("expected_binding", "account_ref", "private/path"),
    ("attestation", "observed_at", "2026-01-01T00:00:00"),
    ("attestation", "observed_at", "2026-01-01T00:00:00+00:00"),
    ("attestation", "observed_at", "2026-01-01T00:00:00.0000001Z"),
    ("attestation", "expires_at", "2026-02-30T00:00:00Z"),
    ("attestation", "revoked", 0), ("funding", "risk_level", False),
    ("funding", "source_version", "untrusted-v2"),
    ("attestation", "source_version", "untrusted-v2"),
    ("attestation", "verified", True), ("attestation", "signature", "synthetic"),
    ("attestation", "private_key", "synthetic"), ("order", "quantity", 1.5),
])
def test_strict_fields(segment, key, value):
    data = json.loads(_raw_input())
    data[segment][key] = value
    assert isinstance(parse_input(json.dumps(data).encode()), InputError)


@pytest.mark.parametrize("quantity,price", [(1, "4999"), (100, "49.99"), (10, "499.9")])
def test_exact_boundary_and_clock_windows(quantity, price):
    assert FUND.estimate
    order = replace(ORDER, quantity=quantity, final_price=Decimal(price))
    fund = replace(FUND, estimate=replace(FUND.estimate, price=Decimal(price), cash_max_qty=Decimal(quantity) + Decimal("0.9")), debt_observed_at=NOW-timedelta(days=1))
    evidence = replace(INPUT, order=order, funding=fund, attestation=replace(ATT, observed_at=NOW-timedelta(hours=12), expires_at=NOW+timedelta(hours=12)))
    report = evaluate(evidence, now=NOW, trusted_receipt_verifier=_FakeRegistry(evidence))
    assert report.status is Status.EVIDENCE_READY_OFF
    assert not report.orders_authorized


def test_canonical_prefix_and_nested_duplicates():
    canonical = b'{"currency":"USD","final_price":"499.9","order_type":"LO","quantity":10,"side":"BUY","symbol":"SPY.US"}'
    assert canonical_bytes(ORDER) == canonical
    assert claim_digest("order", ORDER) == hashlib.sha256(b"passive-eligibility-order-v1\0" + canonical).hexdigest()
    raw = canonical_bytes(INPUT).replace(b'"quantity":10', b'"quantity":10,"quantity":10')
    assert parse_input(raw) == InputError(Reason.DUPLICATE_KEY)
    assert isinstance(parse_input(b" " * (65536-len(canonical_bytes(INPUT))) + canonical_bytes(INPUT)), EligibilityInput)
    assert canonical_bytes(replace(FUND, fee_upper=Decimal("-0.000000"))) == canonical_bytes(replace(FUND, fee_upper=Decimal("0")))


@pytest.mark.parametrize("field,value", [("price", None), ("cash_max_qty", None), ("cash_max_qty", Decimal(-1))])
def test_missing_estimate_values(field, value):
    assert FUND.estimate
    evidence = replace(INPUT, funding=replace(FUND, estimate=replace(FUND.estimate, **{field: value})))
    assert Reason.FUNDING_UNKNOWN in evaluate(evidence, now=NOW).reasons


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("1E-999999"), Decimal("1E+999999")])
def test_direct_invalid_decimals(value):
    report = evaluate(replace(INPUT, order=replace(ORDER, final_price=value)), now=NOW)
    assert report.status is Status.INVALID
    assert Reason.INVALID_INPUT in report.reasons
    assert report.order_digest is None


@pytest.mark.parametrize("args,raw,reason", [
    (["--as-of", "2026-01-01T00:00:00Z"], None, Reason.NO_TRUSTED_VERIFIER),
    (["--as-of", "not-a-time"], b"{}", Reason.INVALID_AS_OF),
    (["--key", "secret-fixture"], b"{}", Reason.INVALID_ARGUMENTS),
    (["--allow-unverified"], b"{}", Reason.INVALID_ARGUMENTS),
    (["--input", "/private/secret-fixture"], b"{}", Reason.INVALID_ARGUMENTS),
    (["--as-of", "2026-01-01T00:00:00Z"], b'{"private_key":"secret-fixture"}', Reason.UNKNOWN_FIELD),
])
def test_cli_isolation(tmp_path, args, raw, reason):
    # Import source/stdlib reads are allowed. All business file opens are blocked.
    launcher = '''import sys, runpy, builtins, importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith(('app.config','app.services','app.api','app.database','app.main','app.core','dotenv','longport','longbridge')):
   raise RuntimeError('blocked-runtime-import')
sys.meta_path.insert(0, Block())
def deny(*args, **kwargs):
 raise RuntimeError('blocked-business-io')
builtins.open = deny
runpy.run_module('app.cli.passive_eligibility', run_name='__main__')
'''
    backend = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-B", "-c", launcher, *args], input=_raw_input() if raw is None else raw, capture_output=True, cwd=tmp_path, env={"PYTHONPATH": backend, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10)
    assert result.returncode == 2
    assert result.stderr == b""
    assert len(result.stdout.splitlines()) == 1
    report = json.loads(result.stdout)
    assert reason in report["reasons"]
    assert report["orders_authorized"] is False
    assert report["status"] != "EVIDENCE_READY_OFF"
    assert b"secret-fixture" not in result.stdout


def test_purity_and_report_constants():
    root = Path(__file__).resolve().parents[1] / "app/domain/passive_allocation"
    for filename in ("eligibility_model.py", "eligibility_codec.py", "eligibility.py"):
        tree = ast.parse((root / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(x.name.split(".")[0] in sys.stdlib_module_names for x in node.names)
            if isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module and node.module.split(".")[0] in sys.stdlib_module_names
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id in {"open", "eval", "exec", "__import__"})
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr in {"now", "utcnow", "read_text", "write_text", "connect"})
    result = evaluate(INPUT, now=NOW)
    with pytest.raises(ValueError):
        replace(result, orders_authorized=True)
    assert result.report_scope == "OFFLINE_NON_AUTHORIZING"
    assert result.binding_basis == "CALLER_SUPPLIED_ASSUMPTION"
    assert result.clock_basis == "CALLER_SUPPLIED_AS_OF"
