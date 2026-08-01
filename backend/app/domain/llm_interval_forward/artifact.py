from __future__ import annotations

import hashlib
import hmac
import json
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contract import (
    MAX_CANONICAL_INTEGER_ABS,
    MAX_CANONICAL_JSON_BYTES,
    IntervalForwardContractError,
    canonical_json_bytes,
)


INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION = 1
INTERVAL_FORWARD_ARTIFACT_KIND = "LLM_INTERVAL_PAIRED_FORWARD_REPLAY"
INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION = (
    "llm-interval-paired-replay-artifact-v1"
)
INTERVAL_FORWARD_ARTIFACT_CODEC = "zlib"
INTERVAL_FORWARD_ARTIFACT_COMPRESSION_LEVEL = 9

MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES = 2 * 1024 * 1024
MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES = 512 * 1024

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class IntervalForwardArtifactError(ValueError):
    """Raised when an interval-forward artifact fails its frozen codec contract."""


if MAX_CANONICAL_JSON_BYTES != MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES:
    raise IntervalForwardArtifactError(
        "artifact and canonical JSON raw-size limits have drifted"
    )


@dataclass(frozen=True)
class EncodedIntervalForwardArtifact:
    digest_sha256: str
    schema_version: int
    kind: str
    codec: str
    raw_size: int
    compressed_size: int
    payload: bytes


def canonical_interval_forward_artifact_json(
    value: Mapping[str, object],
) -> bytes:
    """Return the contract's canonical JSON after payload identity checks."""
    if not isinstance(value, Mapping):
        raise IntervalForwardArtifactError("artifact payload must be a JSON object")
    _validate_payload_identity(value)
    try:
        raw = canonical_json_bytes(value)
    except (
        IntervalForwardContractError,
        RecursionError,
        UnicodeEncodeError,
    ) as exc:
        raise IntervalForwardArtifactError(
            "interval-forward artifact payload is not canonical JSON"
        ) from exc
    if not raw:
        raise IntervalForwardArtifactError("artifact payload must not be empty")
    if len(raw) > MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES:
        raise IntervalForwardArtifactError(
            "interval-forward artifact exceeds the raw size limit"
        )
    return raw


def encode_interval_forward_artifact(
    value: Mapping[str, object],
) -> EncodedIntervalForwardArtifact:
    """Canonicalize and compress one immutable replay source artifact."""
    raw = canonical_interval_forward_artifact_json(value)
    compressed = zlib.compress(
        raw,
        level=INTERVAL_FORWARD_ARTIFACT_COMPRESSION_LEVEL,
    )
    if not compressed:
        raise IntervalForwardArtifactError("artifact compression produced no bytes")
    if len(compressed) > MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES:
        raise IntervalForwardArtifactError(
            "interval-forward artifact exceeds the compressed size limit"
        )
    return EncodedIntervalForwardArtifact(
        digest_sha256=hashlib.sha256(raw).hexdigest(),
        schema_version=INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION,
        kind=INTERVAL_FORWARD_ARTIFACT_KIND,
        codec=INTERVAL_FORWARD_ARTIFACT_CODEC,
        raw_size=len(raw),
        compressed_size=len(compressed),
        payload=compressed,
    )


def decode_interval_forward_artifact(
    *,
    digest_sha256: str,
    schema_version: int,
    kind: str,
    codec: str,
    raw_size: int,
    compressed_size: int,
    payload: bytes,
) -> dict[str, Any]:
    """Bounded-decompress and content-integrity-check one canonical artifact."""
    _validate_sha256(digest_sha256)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION
    ):
        raise IntervalForwardArtifactError("unsupported artifact schema")
    if kind != INTERVAL_FORWARD_ARTIFACT_KIND:
        raise IntervalForwardArtifactError("unsupported artifact kind")
    if codec != INTERVAL_FORWARD_ARTIFACT_CODEC:
        raise IntervalForwardArtifactError("unsupported artifact codec")
    _validate_size(
        raw_size,
        maximum=MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES,
        label="raw size",
    )
    _validate_size(
        compressed_size,
        maximum=MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES,
        label="compressed size",
    )
    if not isinstance(payload, bytes):
        raise IntervalForwardArtifactError("compressed payload must be bytes")
    if len(payload) != compressed_size:
        raise IntervalForwardArtifactError(
            "compressed payload size does not match metadata"
        )

    raw = _bounded_decompress(payload, declared_raw_size=raw_size)
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_digest, digest_sha256):
        raise IntervalForwardArtifactError("artifact digest mismatch")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_json_float,
            parse_int=_parse_json_int,
            parse_constant=_reject_json_constant,
        )
    except IntervalForwardArtifactError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise IntervalForwardArtifactError(
            "artifact payload is not valid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise IntervalForwardArtifactError("artifact payload must be a JSON object")

    canonical = canonical_interval_forward_artifact_json(decoded)
    if canonical != raw:
        raise IntervalForwardArtifactError(
            "artifact payload is not canonical JSON"
        )
    return decoded


def _bounded_decompress(payload: bytes, *, declared_raw_size: int) -> bytes:
    try:
        decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS)
        raw = decompressor.decompress(payload, declared_raw_size + 1)
        if len(raw) > declared_raw_size or decompressor.unconsumed_tail:
            raise IntervalForwardArtifactError(
                "decompressed payload exceeds the declared raw size"
            )
    except zlib.error as exc:
        raise IntervalForwardArtifactError("artifact decompression failed") from exc
    if len(raw) > declared_raw_size:
        raise IntervalForwardArtifactError(
            "decompressed payload exceeds the declared raw size"
        )
    if not decompressor.eof:
        raise IntervalForwardArtifactError("compressed payload is incomplete")
    if decompressor.unused_data:
        raise IntervalForwardArtifactError("compressed payload has trailing bytes")
    if len(raw) != declared_raw_size:
        raise IntervalForwardArtifactError(
            "decompressed payload size does not match metadata"
        )
    return raw


def _validate_payload_identity(value: Mapping[str, object]) -> None:
    if value.get("kind") != INTERVAL_FORWARD_ARTIFACT_KIND:
        raise IntervalForwardArtifactError(
            "payload kind does not match the artifact kind"
        )
    if value.get("schema_version") != INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION:
        raise IntervalForwardArtifactError("unsupported payload schema")


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise IntervalForwardArtifactError(
            "artifact digest must be lowercase SHA-256 hex"
        )


def _validate_size(value: int, *, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise IntervalForwardArtifactError(f"{label} is outside the allowed range")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntervalForwardArtifactError(
                f"artifact payload contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_float(value: str) -> Any:
    raise IntervalForwardArtifactError(
        f"artifact payload contains native JSON float {value}"
    )


def _parse_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > len(str(MAX_CANONICAL_INTEGER_ABS)):
        raise IntervalForwardArtifactError(
            "artifact payload contains an oversized JSON integer"
        )
    parsed = int(value)
    if abs(parsed) > MAX_CANONICAL_INTEGER_ABS:
        raise IntervalForwardArtifactError(
            "artifact payload contains an oversized JSON integer"
        )
    return parsed


def _reject_json_constant(value: str) -> Any:
    raise IntervalForwardArtifactError(
        f"artifact payload contains non-finite number {value}"
    )


__all__ = [
    "INTERVAL_FORWARD_ARTIFACT_CODEC",
    "INTERVAL_FORWARD_ARTIFACT_COMPRESSION_LEVEL",
    "INTERVAL_FORWARD_ARTIFACT_KIND",
    "INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION",
    "INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION",
    "MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES",
    "MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES",
    "EncodedIntervalForwardArtifact",
    "IntervalForwardArtifactError",
    "canonical_interval_forward_artifact_json",
    "decode_interval_forward_artifact",
    "encode_interval_forward_artifact",
]
