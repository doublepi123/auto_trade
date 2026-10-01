"""Strict offline codec; digests are not authentication."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints
from uuid import UUID

from .eligibility_model import AttestationClaim, EligibilityInput, FundingClaims, InputError, Reason


class _Invalid(ValueError):
    def __init__(self, code: Reason = Reason.INVALID_INPUT):
        self.code = code
        super().__init__(code.value)


def parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z", value):
        raise _Invalid()
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise _Invalid() from None


def valid_time(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0)


def valid_decimal(value: object) -> bool:
    if type(value) is not Decimal or not value.is_finite():
        return False
    parts = value.as_tuple()
    exponent = parts.exponent
    return isinstance(exponent, int) and -6 <= exponent <= 18 and len(parts.digits) <= 24 and value.adjusted() < 18


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?", value):
        raise _Invalid()
    return Decimal(value)


def _decode(expected: Any, value: Any, name: str = "") -> Any:
    if get_origin(expected) is UnionType:
        choices = get_args(expected)
        if value is None and type(None) in choices:
            return None
        return _decode(next(t for t in choices if t is not type(None)), value, name)
    if isinstance(expected, type) and is_dataclass(expected):
        if type(value) is not dict:
            raise _Invalid()
        names = {f.name for f in fields(expected)}
        if value.keys() - names:
            raise _Invalid(Reason.UNKNOWN_FIELD)
        required = {"schema_version"} if expected is EligibilityInput else names
        if required - value.keys():
            raise _Invalid(Reason.MISSING_FIELD)
        hints = get_type_hints(expected)
        decoded = {key: _decode(hints[key], value.get(key), key) for key in hints}
        version = {EligibilityInput: ("schema_version", "passive-eligibility-input-v1"), AttestationClaim: ("source_version", "offline-account-attestation-v1"), FundingClaims: ("source_version", "offline-funding-claims-v1")}.get(expected)
        if version and decoded[version[0]] != version[1]:
            raise _Invalid(Reason.INVALID_VERSION)
        return expected(**decoded)
    if expected is Decimal:
        return _decimal(value)
    if expected is datetime:
        return parse_time(value)
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is not str:
            raise _Invalid()
        try:
            return expected(value)
        except ValueError:
            raise _Invalid() from None
    if type(value) is not expected:
        raise _Invalid()
    if expected is int and (len(str(value)) > 18 or (name == "generation" and value < 0)):
        raise _Invalid()
    if expected is str:
        if len(value) > 128 or not value.isascii() or not value.isprintable() or not value:
            raise _Invalid()
        if name in {"credential_fingerprint", "context_digest", "evidence_ref", "fee_evidence_ref", "debt_evidence_ref"} and not re.fullmatch("[0-9a-f]{64}", value):
            raise _Invalid()
        if name == "account_ref" and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
            raise _Invalid()
        if name == "client_instance_id":
            try:
                if str(UUID(value)) != value:
                    raise _Invalid()
            except ValueError:
                raise _Invalid() from None
    return value


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise _Invalid(Reason.DUPLICATE_KEY)
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    raise _Invalid(Reason.INVALID_JSON)


def _depth(text: str) -> None:
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 8:
                raise _Invalid(Reason.EXCESSIVE_DEPTH)
        elif char in "]}":
            depth -= 1


def parse_input(raw: bytes) -> EligibilityInput | InputError:
    if type(raw) is not bytes:
        return InputError(Reason.INVALID_INPUT)
    if len(raw) > 65536:
        return InputError(Reason.INPUT_TOO_LARGE)
    try:
        text = raw.decode("utf-8", errors="strict")
        _depth(text)
        value = json.loads(text, object_pairs_hook=_pairs, parse_float=_reject_number, parse_constant=_reject_number)
        return _decode(EligibilityInput, value)
    except _Invalid as exc:
        return InputError(exc.code)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return InputError(Reason.INVALID_JSON)


def _normalize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        # Validate exact Python types before converting; bool/int and str/Enum
        # must not acquire validity through serialization.
        hints = get_type_hints(type(value))
        for key, expected in hints.items():
            item = getattr(value, key)
            choices = get_args(expected) if get_origin(expected) is UnionType else (expected,)
            if type(item) not in choices:
                raise _Invalid()
        return {f.name: _normalize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Decimal):
        if not valid_decimal(value):
            raise _Invalid()
        text = format(value, "f")
        return "0" if value.is_zero() else text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, datetime):
        if not valid_time(value):
            raise _Invalid()
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if value is None or type(value) in (str, int, bool):
        return value
    raise _Invalid()


def canonical_bytes(value: object) -> bytes:
    normalized = _normalize(value)
    if is_dataclass(value):
        _decode(type(value), normalized)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def claim_digest(kind: str, value: object) -> str:
    if kind not in {"attestation", "funding", "order"}:
        raise _Invalid()
    return hashlib.sha256(("passive-eligibility-" + kind + "-v1\0").encode("ascii") + canonical_bytes(value)).hexdigest()
