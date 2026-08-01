from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION = 1
FORWARD_REPLAY_ARTIFACT_KIND = "STRATEGY_V2_FORWARD_REPLAY"
FORWARD_REPLAY_ARTIFACT_CODEC = "zlib"
FORWARD_REPLAY_ARTIFACT_ROLE = "REPLAY_BUNDLE"
MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES = 8 * 1024 * 1024
MAX_FORWARD_REPLAY_ARTIFACT_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_FORWARD_REPLAY_ARTIFACT_JSON_DEPTH = 64

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ForwardReplayArtifactError(ValueError):
    """Raised when replay artifact bytes violate the frozen storage contract."""


@dataclass(frozen=True)
class EncodedForwardReplayArtifact:
    digest_sha256: str
    schema_version: int
    kind: str
    codec: str
    raw_size: int
    compressed_size: int
    payload: bytes


def canonical_forward_replay_json(value: Mapping[str, object]) -> bytes:
    """Encode one strict JSON object using the v1 canonical representation."""
    normalized = _validated_json_value(dict(value), path="$", require_object=True)
    try:
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ForwardReplayArtifactError(
            "forward replay artifact is not canonical JSON"
        ) from exc
    if not encoded:
        raise ForwardReplayArtifactError("forward replay artifact must not be empty")
    if len(encoded) > MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES:
        raise ForwardReplayArtifactError(
            "forward replay artifact exceeds the raw size limit"
        )
    return encoded


def encode_forward_replay_artifact(
    value: Mapping[str, object],
) -> EncodedForwardReplayArtifact:
    raw = canonical_forward_replay_json(value)
    compressed = zlib.compress(raw, level=9)
    if not compressed:
        raise ForwardReplayArtifactError(
            "forward replay artifact compression produced no bytes"
        )
    if len(compressed) > MAX_FORWARD_REPLAY_ARTIFACT_COMPRESSED_BYTES:
        raise ForwardReplayArtifactError(
            "forward replay artifact exceeds the compressed size limit"
        )
    return EncodedForwardReplayArtifact(
        digest_sha256=hashlib.sha256(raw).hexdigest(),
        schema_version=FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION,
        kind=FORWARD_REPLAY_ARTIFACT_KIND,
        codec=FORWARD_REPLAY_ARTIFACT_CODEC,
        raw_size=len(raw),
        compressed_size=len(compressed),
        payload=compressed,
    )


def decode_forward_replay_artifact(
    *,
    digest_sha256: str,
    schema_version: int,
    kind: str,
    codec: str,
    raw_size: int,
    compressed_size: int,
    payload: bytes,
) -> dict[str, Any]:
    """Validate, bounded-decompress, and decode one canonical v1 artifact."""
    _validate_sha256(digest_sha256, label="artifact digest")
    if schema_version != FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION:
        raise ForwardReplayArtifactError("unsupported forward replay artifact schema")
    if kind != FORWARD_REPLAY_ARTIFACT_KIND:
        raise ForwardReplayArtifactError("unsupported forward replay artifact kind")
    if codec != FORWARD_REPLAY_ARTIFACT_CODEC:
        raise ForwardReplayArtifactError("unsupported forward replay artifact codec")
    _validate_size(
        raw_size,
        maximum=MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES,
        label="raw size",
    )
    _validate_size(
        compressed_size,
        maximum=MAX_FORWARD_REPLAY_ARTIFACT_COMPRESSED_BYTES,
        label="compressed size",
    )
    if not isinstance(payload, bytes):
        raise ForwardReplayArtifactError("compressed payload must be bytes")
    if len(payload) != compressed_size:
        raise ForwardReplayArtifactError("compressed payload size does not match metadata")

    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(payload, raw_size + 1)
        if len(raw) > raw_size or decompressor.unconsumed_tail:
            raise ForwardReplayArtifactError(
                "decompressed payload exceeds the declared raw size"
            )
        raw += decompressor.flush()
    except zlib.error as exc:
        raise ForwardReplayArtifactError(
            "forward replay artifact decompression failed"
        ) from exc
    if not decompressor.eof:
        raise ForwardReplayArtifactError("compressed payload is incomplete")
    if decompressor.unused_data:
        raise ForwardReplayArtifactError("compressed payload has trailing bytes")
    if len(raw) != raw_size:
        raise ForwardReplayArtifactError(
            "decompressed payload size does not match metadata"
        )
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_digest, digest_sha256):
        raise ForwardReplayArtifactError("forward replay artifact digest mismatch")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except ForwardReplayArtifactError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ForwardReplayArtifactError(
            "forward replay artifact payload is not valid JSON"
        ) from exc
    normalized = _validated_json_value(decoded, path="$", require_object=True)
    if not isinstance(normalized, dict):
        raise ForwardReplayArtifactError("forward replay artifact must be a JSON object")
    if canonical_forward_replay_json(normalized) != raw:
        raise ForwardReplayArtifactError(
            "forward replay artifact payload is not canonical JSON"
        )
    return normalized


def forward_replay_artifact_binding_sha256(
    *,
    evidence_id: int,
    evidence_digest_sha256: str,
    artifact_digest_sha256: str,
    role: str = FORWARD_REPLAY_ARTIFACT_ROLE,
) -> str:
    """Bind one immutable evidence identity to one content-addressed artifact."""
    if isinstance(evidence_id, bool) or not isinstance(evidence_id, int) or evidence_id <= 0:
        raise ForwardReplayArtifactError("evidence_id must be a positive integer")
    _validate_sha256(evidence_digest_sha256, label="evidence digest")
    _validate_sha256(artifact_digest_sha256, label="artifact digest")
    if role != FORWARD_REPLAY_ARTIFACT_ROLE:
        raise ForwardReplayArtifactError("unsupported forward replay artifact role")
    encoded = canonical_forward_replay_json({
        "artifact_digest_sha256": artifact_digest_sha256,
        "evidence_digest_sha256": evidence_digest_sha256,
        "evidence_id": evidence_id,
        "role": role,
    })
    return hashlib.sha256(encoded).hexdigest()


def _validate_sha256(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ForwardReplayArtifactError(f"{label} must be lowercase SHA-256 hex")


def _validate_size(value: int, *, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ForwardReplayArtifactError(f"{label} is outside the allowed range")


def _validated_json_value(
    value: object,
    *,
    path: str,
    require_object: bool = False,
    depth: int = 0,
) -> Any:
    if depth > MAX_FORWARD_REPLAY_ARTIFACT_JSON_DEPTH:
        raise ForwardReplayArtifactError(
            "forward replay artifact exceeds the JSON nesting limit"
        )
    if require_object and not isinstance(value, dict):
        raise ForwardReplayArtifactError(f"{path} must be a JSON object")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ForwardReplayArtifactError(f"{path} must contain only finite numbers")
        return value
    if isinstance(value, list):
        return [
            _validated_json_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ForwardReplayArtifactError(f"{path} contains a non-string key")
            normalized[key] = _validated_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return normalized
    raise ForwardReplayArtifactError(
        f"{path} contains unsupported JSON value type {type(value).__name__}"
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ForwardReplayArtifactError(
                f"forward replay artifact contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ForwardReplayArtifactError(
        f"forward replay artifact contains non-finite number {value}"
    )
