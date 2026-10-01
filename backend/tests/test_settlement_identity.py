"""Fifteen synthetic claim families, not broker authentication or integration."""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import cast

import pytest

from app.domain.settlement_identity import (
    IdentityComparison, IdentityReason as R, IdentityVerdict as V,
    SettlementIdentity, TimestampPrecision as P, TimestampProvenance as S,
    compare_settlement_identity as compare,
)


INSTANT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
BASE = SettlementIdentity("synthetic-order", "synthetic-account", "synthetic-submission", INSTANT, P.SECOND, S.BROKER_SUBMITTED_AT)


class _StringSubclass(str):
    pass


class _DatetimeSubclass(datetime):
    pass


class _IdentitySubclass(SettlementIdentity):
    pass


class _OtherEnum(str, Enum):
    SECOND = "SECOND"
    BROKER_SUBMITTED_AT = "BROKER_SUBMITTED_AT"


class _HostileObject:
    def __getattribute__(self, name: str) -> object:
        if name in {"broker_order_id", "account_scope", "submission_scope", "broker_submitted_at", "precision", "provenance"}:
            raise AssertionError("must-not-inspect-wrong-object")
        return object.__getattribute__(self, name)


class _RaisingTZ(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise RuntimeError("synthetic-private-exception")


class _NoneTZ(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


class _InvalidTZ(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return cast(timedelta, "synthetic-private-invalid-offset")


def _changed(**changes: object) -> SettlementIdentity:
    return replace(BASE, **changes)


def _assert_result(left: SettlementIdentity | None, right: SettlementIdentity | None,
                   verdict: V, reasons: tuple[R, ...]) -> None:
    # Identity comparison is symmetric, including validation reason ordering.
    result = compare(left, right)
    assert result.verdict is verdict
    assert result.reason_codes == reasons
    assert result.authorizes_actions is False
    assert compare(right, left) == result


@pytest.mark.parametrize("precision,microsecond", [(P.SECOND, 0), (P.MILLISECOND, 123000), (P.MICROSECOND, 123456)])
def test_complete_known_match_is_non_authorizing(precision: P, microsecond: int) -> None:
    item = _changed(precision=precision, broker_submitted_at=INSTANT.replace(microsecond=microsecond))
    _assert_result(item, item, V.MATCH, (R.IDENTITY_MATCH,))


@pytest.mark.parametrize("offset", [timedelta(hours=8), timedelta(hours=-5, minutes=-30), timedelta(seconds=17)])
def test_same_instant_with_offset(offset: timedelta) -> None:
    incoming = _changed(broker_submitted_at=INSTANT.astimezone(timezone(offset)))
    _assert_result(BASE, incoming, V.MATCH, (R.IDENTITY_MATCH,))


@pytest.mark.parametrize("precision,delta", [(P.SECOND, timedelta(seconds=1)), (P.MILLISECOND, timedelta(milliseconds=1)), (P.MICROSECOND, timedelta(microseconds=1))])
def test_complete_known_time_conflict(precision: P, delta: timedelta) -> None:
    _assert_result(_changed(precision=precision), _changed(precision=precision, broker_submitted_at=INSTANT+delta), V.CONFLICT, (R.BROKER_SUBMITTED_AT_CONFLICT,))


@pytest.mark.parametrize("changes,reasons", [
    ({"broker_order_id": "synthetic-ORDER"}, (R.BROKER_ORDER_ID_CONFLICT,)),
    ({"account_scope": "synthetic-ACCOUNT"}, (R.ACCOUNT_SCOPE_CONFLICT,)),
    ({"submission_scope": "synthetic-SUBMISSION"}, (R.SUBMISSION_SCOPE_CONFLICT,)),
    ({"broker_order_id": "other-order", "account_scope": "other-account", "submission_scope": "other-submission", "broker_submitted_at": INSTANT+timedelta(seconds=1)}, (R.BROKER_ORDER_ID_CONFLICT, R.ACCOUNT_SCOPE_CONFLICT, R.SUBMISSION_SCOPE_CONFLICT, R.BROKER_SUBMITTED_AT_CONFLICT)),
])
def test_complete_known_id_scope_conflicts(changes: dict[str, object], reasons: tuple[R, ...]) -> None:
    _assert_result(BASE, _changed(**changes), V.CONFLICT, reasons)


@pytest.mark.parametrize("value,reason", [
    (None, R.IDENTITY_MISSING), (True, R.IDENTITY_TYPE_INVALID), (1, R.IDENTITY_TYPE_INVALID),
    ({}, R.IDENTITY_TYPE_INVALID), ("synthetic", R.IDENTITY_TYPE_INVALID),
    (_HostileObject(), R.IDENTITY_TYPE_INVALID),
    (_IdentitySubclass(BASE.broker_order_id, BASE.account_scope, BASE.submission_scope, INSTANT, P.SECOND, S.BROKER_SUBMITTED_AT), R.IDENTITY_TYPE_INVALID),
])
def test_missing_or_wrong_identity(value: object, reason: R) -> None:
    wrong = cast(SettlementIdentity | None, value)
    _assert_result(BASE, wrong, V.UNPROVEN, (reason,))
    _assert_result(wrong, wrong, V.UNPROVEN, (reason,))


@pytest.mark.parametrize("field,reason", [("broker_order_id", R.BROKER_ORDER_ID_UNPROVEN), ("account_scope", R.ACCOUNT_SCOPE_UNPROVEN), ("submission_scope", R.SUBMISSION_SCOPE_UNPROVEN)])
@pytest.mark.parametrize("value,valid", [
    (None, False), ("", False), (" leading", False), ("trailing ", False),
    ("newline\n", False), ("a/b", False), ("é", False), ("a\x00b", False),
    ("_leading", False), ("a"*129, False), (True, False), (1, False),
    (_StringSubclass("synthetic"), False), ("a", True), ("Z"*128, True), ("a_.:-09", True),
], ids=[f"case-{i}" for i in range(16)])
def test_ids_exact_opaque_bounds(field: str, reason: R, value: object, valid: bool) -> None:
    item = _changed(**{field: value})
    _assert_result(item, item, V.MATCH if valid else V.UNPROVEN, (R.IDENTITY_MATCH,) if valid else (reason,))


@pytest.mark.parametrize("source", [None, S.UNKNOWN, S.LOCAL_TIME, S.FILL_TIME, S.OWNER_ASSERTION])
def test_unproven_provenance(source: S | None) -> None:
    _assert_result(BASE, _changed(provenance=source), V.UNPROVEN, (R.TIMESTAMP_SOURCE_UNPROVEN,))


@pytest.mark.parametrize("field,value,reason", [
    ("precision", "SECOND", R.TIME_PRECISION_UNPROVEN),
    ("precision", True, R.TIME_PRECISION_UNPROVEN),
    ("precision", 1, R.TIME_PRECISION_UNPROVEN),
    ("precision", _OtherEnum.SECOND, R.TIME_PRECISION_UNPROVEN),
    ("provenance", "BROKER_SUBMITTED_AT", R.TIMESTAMP_SOURCE_UNPROVEN),
    ("provenance", True, R.TIMESTAMP_SOURCE_UNPROVEN),
    ("provenance", 1, R.TIMESTAMP_SOURCE_UNPROVEN),
    ("provenance", _OtherEnum.BROKER_SUBMITTED_AT, R.TIMESTAMP_SOURCE_UNPROVEN),
])
def test_no_enum_or_bool_coercion(field: str, value: object, reason: R) -> None:
    _assert_result(BASE, _changed(**{field: value}), V.UNPROVEN, (reason,))


@pytest.mark.parametrize("value", [None, INSTANT.replace(tzinfo=None), "2026-01-02T03:04:05Z", 0, True, INSTANT.date(), _DatetimeSubclass(2026, 1, 2, tzinfo=timezone.utc)])
def test_missing_naive_wrong_timestamp(value: object) -> None:
    _assert_result(BASE, _changed(broker_submitted_at=value), V.UNPROVEN, (R.TIMESTAMP_UNPROVEN,))


@pytest.mark.parametrize("value", [
    INSTANT.replace(tzinfo=_RaisingTZ()), INSTANT.replace(tzinfo=_NoneTZ()), INSTANT.replace(tzinfo=_InvalidTZ()),
    datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-1))),
], ids=[f"case-{i}" for i in range(5)])
def test_adversarial_timezone_and_utc_overflow(value: datetime) -> None:
    _assert_result(BASE, _changed(broker_submitted_at=value), V.UNPROVEN, (R.TIMESTAMP_UNPROVEN,))


@pytest.mark.parametrize("precision", [None, P.UNKNOWN])
def test_unknown_precision_not_assumed(precision: P | None) -> None:
    _assert_result(BASE, _changed(precision=precision), V.UNPROVEN, (R.TIME_PRECISION_UNPROVEN,))


@pytest.mark.parametrize("precision,microsecond,offset,valid", [
    (P.SECOND, 1, timedelta(0), False),
    (P.MILLISECOND, 1, timedelta(0), False),
    (P.MILLISECOND, 999999, timedelta(0), False),
    (P.SECOND, 0, timedelta(microseconds=1), False),
    (P.MILLISECOND, 0, timedelta(microseconds=1), False),
    (P.SECOND, 1, timedelta(microseconds=1), False),
    (P.MILLISECOND, 1, timedelta(microseconds=1), False),
    (P.MILLISECOND, 123000, timedelta(milliseconds=1), True),
    (P.MICROSECOND, 123456, timedelta(microseconds=1), True),
])
def test_precision_alignment_raw_and_utc(precision: P, microsecond: int, offset: timedelta, valid: bool) -> None:
    item = _changed(precision=precision, broker_submitted_at=INSTANT.replace(microsecond=microsecond, tzinfo=timezone(offset)))
    _assert_result(item, item, V.MATCH if valid else V.UNPROVEN, (R.IDENTITY_MATCH,) if valid else (R.PRECISION_ALIGNMENT_INVALID,))


@pytest.mark.parametrize("left,right", [(P.SECOND, P.MILLISECOND), (P.SECOND, P.MICROSECOND), (P.MILLISECOND, P.MICROSECOND)])
def test_different_known_precision_even_zero(left: P, right: P) -> None:
    _assert_result(_changed(precision=left), _changed(precision=right), V.UNPROVEN, (R.PRECISION_NOT_COMPARABLE,))


def test_validation_precedence_dedup_order_and_privacy() -> None:
    incomplete = SettlementIdentity(None, None, None, None, P.UNKNOWN, S.OWNER_ASSERTION)
    expected = (R.BROKER_ORDER_ID_UNPROVEN, R.ACCOUNT_SCOPE_UNPROVEN, R.SUBMISSION_SCOPE_UNPROVEN, R.TIMESTAMP_SOURCE_UNPROVEN, R.TIME_PRECISION_UNPROVEN, R.TIMESTAMP_UNPROVEN)
    _assert_result(incomplete, incomplete, V.UNPROVEN, expected)
    _assert_result(None, incomplete, V.UNPROVEN, (R.IDENTITY_MISSING, *expected))
    _assert_result(None, cast(SettlementIdentity, _HostileObject()), V.UNPROVEN, (R.IDENTITY_MISSING, R.IDENTITY_TYPE_INVALID))
    for changes, reason in [
        ({"account_scope": None}, R.ACCOUNT_SCOPE_UNPROVEN),
        ({"precision": P.UNKNOWN}, R.TIME_PRECISION_UNPROVEN),
        ({"provenance": S.OWNER_ASSERTION}, R.TIMESTAMP_SOURCE_UNPROVEN),
        ({"precision": P.MICROSECOND}, R.PRECISION_NOT_COMPARABLE),
        ({"broker_submitted_at": INSTANT.replace(tzinfo=None)}, R.TIMESTAMP_UNPROVEN),
    ]:
        _assert_result(BASE, _changed(broker_order_id="known-different", **changes), V.UNPROVEN, (reason,))
    public = repr(asdict(compare(BASE, _changed(broker_submitted_at=INSTANT.replace(tzinfo=_RaisingTZ())))))
    assert "synthetic" not in public
    assert "private" not in public
    assert all(value.name == value.value for enum in (P, S, V, R) for value in enum)
    assert [r.value for r in R] == [
        "IDENTITY_MISSING", "IDENTITY_TYPE_INVALID", "BROKER_ORDER_ID_UNPROVEN", "ACCOUNT_SCOPE_UNPROVEN",
        "SUBMISSION_SCOPE_UNPROVEN", "TIMESTAMP_SOURCE_UNPROVEN", "TIME_PRECISION_UNPROVEN", "TIMESTAMP_UNPROVEN",
        "PRECISION_ALIGNMENT_INVALID", "PRECISION_NOT_COMPARABLE", "BROKER_ORDER_ID_CONFLICT", "ACCOUNT_SCOPE_CONFLICT",
        "SUBMISSION_SCOPE_CONFLICT", "BROKER_SUBMITTED_AT_CONFLICT", "IDENTITY_MATCH",
    ]


def test_frozen_constants_storage_only_purity_no_runtime_callers() -> None:
    # Constructors store bad values too: comparison, not construction, validates.
    bad = _changed(broker_submitted_at=INSTANT.replace(tzinfo=_RaisingTZ()), precision=cast(P, True))
    assert bad.precision is cast(P, True)
    assert bad.broker_submitted_at is not None
    assert not hasattr(BASE, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(BASE, "account_scope", "other")
    for verdict in V:
        report = IdentityComparison(verdict, ())
        assert not hasattr(report, "__dict__")
        assert report.report_scope == "PURE_IDENTITY_NON_AUTHORIZING"
        assert report.evidence_basis == "CALLER_SUPPLIED_CLASSIFICATION"
        assert report.authorizes_actions is False
        for name in ("report_scope", "evidence_basis", "authorizes_actions"):
            assert not next(f for f in fields(report) if f.name == name).init
            with pytest.raises(ValueError):
                replace(report, **{name: True})
            with pytest.raises(FrozenInstanceError):
                setattr(report, name, True)
    root = Path(__file__).resolve().parents[1]
    source = root / "app/domain/settlement_identity.py"
    tree = ast.parse(source.read_text())
    allowed = {"__future__", "dataclasses", "datetime", "enum", "typing", "re"}
    forbidden = {"open", "print", "exec", "eval", "__import__", "getattr", "now", "utcnow", "today", "timestamp", "fromtimestamp", "compare_repeat", "settlement_key"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            assert name not in forbidden
    for folder in (root / "app", root / "scripts"):
        for path in folder.rglob("*.py"):
            if path == source:
                continue
            text = path.read_text()
            assert "settlement_identity" not in text
            assert "compare_settlement_identity" not in text
