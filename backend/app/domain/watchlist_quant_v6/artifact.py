from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any


QUANT_V6_ARTIFACT_SCHEMA_VERSION = 1
QUANT_V6_PAYLOAD_SCHEMA_VERSION = 1
QUANT_V6_ARTIFACT_CODEC = "zlib"
QUANT_V6_ARTIFACT_COMPRESSION_LEVEL = 9
QUANT_V6_EVENT_ARTIFACT_KIND = "WATCHLIST_QUANT_V6_EVENT"
QUANT_V6_ASSESSMENT_ARTIFACT_KIND = "WATCHLIST_QUANT_V6_ASSESSMENT"
QUANT_V6_SESSION_INPUT_ARTIFACT_KIND = "WATCHLIST_QUANT_V6_SESSION_INPUT"
QUANT_V6_EVENT_CONTRACT = "watchlist-quant-v6-event-v1"
QUANT_V6_ASSESSMENT_CONTRACT = "watchlist-quant-v6-assessment-v1"
QUANT_V6_SESSION_INPUT_CONTRACT = "watchlist-quant-v6-session-input-v1"
QUANT_V6_ARTIFACT_KINDS = frozenset({
    QUANT_V6_EVENT_ARTIFACT_KIND,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
})
QUANT_V6_CONTRACT_BY_ARTIFACT_KIND: Mapping[str, str] = MappingProxyType({
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND: QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_EVENT_ARTIFACT_KIND: QUANT_V6_EVENT_CONTRACT,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND: QUANT_V6_SESSION_INPUT_CONTRACT,
})
MAX_QUANT_V6_ARTIFACT_RAW_BYTES = 2 * 1024 * 1024
MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES = 512 * 1024
MAX_QUANT_V6_ARTIFACT_JSON_DEPTH = 64
MAX_QUANT_V6_ARTIFACT_JSON_NODES = 20_000
MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS = 2_000
MAX_QUANT_V6_ARTIFACT_STRING_BYTES = 512 * 1024
MAX_QUANT_V6_ARTIFACT_KEY_BYTES = 256
MAX_QUANT_V6_ARTIFACT_INTEGER_ABS = 10**36
MAX_QUANT_V6_DECIMAL_DIGITS = 256
MAX_QUANT_V6_DECIMAL_ADJUSTED_EXPONENT = 1024

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class QuantV6ArtifactError(ValueError):
    """Raised when bytes violate the independent quant-v6 artifact contract."""


@dataclass(frozen=True)
class EncodedQuantV6Artifact:
    digest_sha256: str
    schema_version: int
    kind: str
    codec: str
    raw_size: int
    compressed_size: int
    payload: bytes


@dataclass(frozen=True)
class _VerifiedDecodedQuantV6Artifact:
    decoded: dict[str, Any]
    canonical_raw: bytes


@dataclass
class _CanonicalJsonBudget:
    nodes: int = 0
    utf8_bytes: int = 0

    def consume_node(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_QUANT_V6_ARTIFACT_JSON_NODES:
            raise QuantV6ArtifactError(
                "quant-v6 payload exceeds the JSON node limit"
            )
        # Account for at least one byte of JSON syntax per value so a graph of
        # tiny repeated values cannot evade the early byte budget entirely.
        self.consume_utf8(1)

    def consume_utf8(self, size: int) -> None:
        self.utf8_bytes += size
        if self.utf8_bytes > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
            raise QuantV6ArtifactError(
                "quant-v6 payload exceeds the raw size limit"
            )


def canonical_decimal(value: Decimal | int | str) -> str:
    """Return a finite, exponent-free decimal string with one zero spelling."""
    if isinstance(value, (bool, float)):
        raise QuantV6ArtifactError("boolean or native float is not a decimal value")
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise QuantV6ArtifactError("invalid decimal value") from exc
    if not candidate.is_finite():
        raise QuantV6ArtifactError("decimal value must be finite")
    decimal_tuple = candidate.as_tuple()
    if len(decimal_tuple.digits) > MAX_QUANT_V6_DECIMAL_DIGITS:
        raise QuantV6ArtifactError("decimal value exceeds the digit limit")
    if candidate and abs(candidate.adjusted()) > MAX_QUANT_V6_DECIMAL_ADJUSTED_EXPONENT:
        raise QuantV6ArtifactError("decimal value exceeds the exponent limit")
    if not candidate:
        return "0"
    rendered = format(candidate, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered == "-0":
        return "0"
    return rendered


def canonical_utc_timestamp(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise QuantV6ArtifactError("timestamp must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_quant_v6_json(value: Mapping[str, object]) -> bytes:
    """Encode one strict object using the quant-v6 canonical JSON contract.

    Decimal instances are normalized into JSON strings. Native JSON floats are
    deliberately rejected so platform formatting cannot change an evidence
    digest.
    """
    try:
        normalized = _validated_json_value(
            value,
            path="$",
            require_object=True,
            budget=_CanonicalJsonBudget(),
        )
        encoder = json.JSONEncoder(
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        output = bytearray()
        for chunk in encoder.iterencode(normalized):
            encoded = chunk.encode("utf-8")
            if len(output) + len(encoded) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
                raise QuantV6ArtifactError(
                    "quant-v6 payload exceeds the raw size limit"
                )
            output.extend(encoded)
        raw = bytes(output)
    except QuantV6ArtifactError:
        raise
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeEncodeError,
    ) as exc:
        raise QuantV6ArtifactError("quant-v6 payload is not canonical JSON") from exc
    if not raw:
        raise QuantV6ArtifactError("quant-v6 payload must not be empty")
    return raw


def quant_v6_payload_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_quant_v6_json(value)).hexdigest()


def encode_quant_v6_artifact(
    value: Mapping[str, object],
    *,
    kind: str,
) -> EncodedQuantV6Artifact:
    _validate_kind(kind)
    _validate_payload_contract(value, kind=kind)
    raw = canonical_quant_v6_json(value)
    return _encode_quant_v6_canonical_bytes(
        value=value,
        raw=raw,
        kind=kind,
    )


def _encode_quant_v6_canonical_bytes(
    *,
    value: Mapping[str, object],
    raw: bytes,
    kind: str,
) -> EncodedQuantV6Artifact:
    """Encode bytes already proven canonical for ``value`` and ``kind``."""
    _validate_kind(kind)
    _validate_payload_contract(value, kind=kind)
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES
    ):
        raise QuantV6ArtifactError(
            "verified quant-v6 canonical bytes are outside the raw size limit"
        )
    compressed = zlib.compress(
        raw,
        level=QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    )
    if not compressed:
        raise QuantV6ArtifactError("quant-v6 compression produced no bytes")
    if len(compressed) > MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES:
        raise QuantV6ArtifactError(
            "quant-v6 payload exceeds the compressed size limit"
        )
    return EncodedQuantV6Artifact(
        digest_sha256=hashlib.sha256(raw).hexdigest(),
        schema_version=QUANT_V6_ARTIFACT_SCHEMA_VERSION,
        kind=kind,
        codec=QUANT_V6_ARTIFACT_CODEC,
        raw_size=len(raw),
        compressed_size=len(compressed),
        payload=compressed,
    )


def _decode_quant_v6_artifact_verified(
    *,
    digest_sha256: str,
    schema_version: int,
    kind: str,
    codec: str,
    raw_size: int,
    compressed_size: int,
    payload: bytes,
) -> _VerifiedDecodedQuantV6Artifact:
    """Bounded-decompress, digest-check, and canonical-check one artifact."""
    _validate_sha256(digest_sha256)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != QUANT_V6_ARTIFACT_SCHEMA_VERSION
    ):
        raise QuantV6ArtifactError("unsupported quant-v6 artifact schema")
    _validate_kind(kind)
    if codec != QUANT_V6_ARTIFACT_CODEC:
        raise QuantV6ArtifactError("unsupported quant-v6 artifact codec")
    _validate_size(
        raw_size,
        maximum=MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
        label="raw size",
    )
    _validate_size(
        compressed_size,
        maximum=MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES,
        label="compressed size",
    )
    if not isinstance(payload, bytes):
        raise QuantV6ArtifactError("compressed payload must be bytes")
    if len(payload) != compressed_size:
        raise QuantV6ArtifactError("compressed payload size does not match metadata")

    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(payload, raw_size + 1)
        if len(raw) > raw_size or decompressor.unconsumed_tail:
            raise QuantV6ArtifactError(
                "decompressed payload exceeds the declared raw size"
            )
        remaining = raw_size + 1 - len(raw)
        if remaining > 0:
            raw += decompressor.flush(remaining)
    except zlib.error as exc:
        raise QuantV6ArtifactError("quant-v6 artifact decompression failed") from exc
    if len(raw) > raw_size:
        raise QuantV6ArtifactError(
            "decompressed payload exceeds the declared raw size"
        )
    if not decompressor.eof:
        raise QuantV6ArtifactError("compressed payload is incomplete")
    if decompressor.unused_data:
        raise QuantV6ArtifactError("compressed payload has trailing bytes")
    if len(raw) != raw_size:
        raise QuantV6ArtifactError(
            "decompressed payload size does not match metadata"
        )
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_digest, digest_sha256):
        raise QuantV6ArtifactError("quant-v6 artifact digest mismatch")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except QuantV6ArtifactError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise QuantV6ArtifactError(
            "quant-v6 artifact payload is not valid JSON"
        ) from exc
    normalized = _validated_json_value(
        decoded,
        path="$",
        require_object=True,
        budget=_CanonicalJsonBudget(),
    )
    if not isinstance(normalized, dict):
        raise QuantV6ArtifactError("quant-v6 artifact root must be an object")
    if canonical_quant_v6_json(normalized) != raw:
        raise QuantV6ArtifactError("quant-v6 artifact payload is not canonical JSON")
    _validate_payload_contract(normalized, kind=kind)
    return _VerifiedDecodedQuantV6Artifact(
        decoded=normalized,
        canonical_raw=raw,
    )


def _reencode_verified_quant_v6_artifact(
    verified: _VerifiedDecodedQuantV6Artifact,
    *,
    kind: str,
) -> EncodedQuantV6Artifact:
    """Reproduce the deterministic envelope without re-canonicalizing JSON."""
    if type(verified) is not _VerifiedDecodedQuantV6Artifact:
        raise QuantV6ArtifactError(
            "quant-v6 verified decode result has an unsupported type"
        )
    return _encode_quant_v6_canonical_bytes(
        value=verified.decoded,
        raw=verified.canonical_raw,
        kind=kind,
    )


def decode_quant_v6_artifact(
    *,
    digest_sha256: str,
    schema_version: int,
    kind: str,
    codec: str,
    raw_size: int,
    compressed_size: int,
    payload: bytes,
) -> dict[str, Any]:
    """Bounded-decompress, digest-check, and canonical-check one artifact."""
    return _decode_quant_v6_artifact_verified(
        digest_sha256=digest_sha256,
        schema_version=schema_version,
        kind=kind,
        codec=codec,
        raw_size=raw_size,
        compressed_size=compressed_size,
        payload=payload,
    ).decoded


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise QuantV6ArtifactError("artifact digest must be lowercase SHA-256 hex")


def _validate_kind(value: str) -> None:
    if value not in QUANT_V6_ARTIFACT_KINDS:
        raise QuantV6ArtifactError("unsupported quant-v6 artifact kind")


def _validate_payload_contract(
    value: Mapping[str, object],
    *,
    kind: str,
) -> None:
    expected_contract = QUANT_V6_CONTRACT_BY_ARTIFACT_KIND[kind]
    if value.get("contract") != expected_contract:
        raise QuantV6ArtifactError(
            "quant-v6 payload contract does not match artifact kind"
        )
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != QUANT_V6_PAYLOAD_SCHEMA_VERSION
    ):
        raise QuantV6ArtifactError("unsupported quant-v6 payload schema")


def _validate_size(value: int, *, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise QuantV6ArtifactError(f"{label} is outside the allowed range")


def _validated_json_value(
    value: object,
    *,
    path: str,
    budget: _CanonicalJsonBudget,
    require_object: bool = False,
    depth: int = 0,
) -> Any:
    if depth > MAX_QUANT_V6_ARTIFACT_JSON_DEPTH:
        raise QuantV6ArtifactError("quant-v6 payload exceeds the JSON nesting limit")
    if require_object and not isinstance(value, Mapping):
        raise QuantV6ArtifactError(f"{path} must be a JSON object")
    budget.consume_node()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        encoded_size = len(value.encode("utf-8"))
        if encoded_size > MAX_QUANT_V6_ARTIFACT_STRING_BYTES:
            raise QuantV6ArtifactError(f"{path} exceeds the JSON string size limit")
        budget.consume_utf8(encoded_size)
        return value
    if isinstance(value, int):
        if abs(value) > MAX_QUANT_V6_ARTIFACT_INTEGER_ABS:
            raise QuantV6ArtifactError(f"{path} exceeds the JSON integer limit")
        return value
    if isinstance(value, Decimal):
        rendered = canonical_decimal(value)
        budget.consume_utf8(len(rendered.encode("utf-8")))
        return rendered
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuantV6ArtifactError(f"{path} contains a non-finite float")
        raise QuantV6ArtifactError(f"{path} contains a native JSON float")
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS:
            raise QuantV6ArtifactError(
                f"{path} exceeds the JSON container item limit"
            )
        normalized_items: list[Any] = []
        for index, item in enumerate(value):
            if len(normalized_items) >= MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS:
                raise QuantV6ArtifactError(
                    f"{path} exceeds the JSON container item limit"
                )
            normalized_items.append(
                _validated_json_value(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    budget=budget,
                )
            )
        return normalized_items
    if isinstance(value, Mapping):
        if len(value) > MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS:
            raise QuantV6ArtifactError(
                f"{path} exceeds the JSON container item limit"
            )
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if len(normalized) >= MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS:
                raise QuantV6ArtifactError(
                    f"{path} exceeds the JSON container item limit"
                )
            if not isinstance(key, str):
                raise QuantV6ArtifactError(f"{path} contains a non-string key")
            if key in normalized:
                raise QuantV6ArtifactError(f"{path} contains a duplicate key")
            encoded_key_size = len(key.encode("utf-8"))
            if encoded_key_size > MAX_QUANT_V6_ARTIFACT_KEY_BYTES:
                raise QuantV6ArtifactError(f"{path} contains an oversized key")
            budget.consume_utf8(encoded_key_size)
            normalized[key] = _validated_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                budget=budget,
            )
        return normalized
    raise QuantV6ArtifactError(
        f"{path} contains unsupported JSON value type {type(value).__name__}"
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QuantV6ArtifactError(
                f"quant-v6 artifact contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_float(value: str) -> Any:
    raise QuantV6ArtifactError(
        f"quant-v6 artifact contains native JSON float {value}"
    )


def _reject_json_constant(value: str) -> Any:
    raise QuantV6ArtifactError(
        f"quant-v6 artifact contains non-finite number {value}"
    )
