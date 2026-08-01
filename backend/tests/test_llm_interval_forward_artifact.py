from __future__ import annotations

import hashlib
import inspect
import json
import zlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date
from typing import Mapping, cast

import pytest

import app.domain.llm_interval_forward.artifact as artifact_module
import app.domain.llm_interval_forward.replay as replay_module
from app.domain.llm_interval_forward.artifact import (
    INTERVAL_FORWARD_ARTIFACT_CODEC,
    INTERVAL_FORWARD_ARTIFACT_COMPRESSION_LEVEL,
    INTERVAL_FORWARD_ARTIFACT_KIND,
    INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION,
    INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION,
    MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES,
    MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES,
    EncodedIntervalForwardArtifact,
    IntervalForwardArtifactError,
    canonical_interval_forward_artifact_json,
    decode_interval_forward_artifact,
    encode_interval_forward_artifact,
)
from app.domain.llm_interval_forward.contract import (
    MAX_CANONICAL_CONTAINER_ITEMS,
    MAX_CANONICAL_INTEGER_ABS,
    MAX_CANONICAL_JSON_DEPTH,
    MAX_CANONICAL_JSON_BYTES,
    MAX_CANONICAL_JSON_NODES,
    MAX_CANONICAL_KEY_BYTES,
    MAX_CANONICAL_STRING_BYTES,
    ProposalObservation,
    canonical_sha256,
)
from app.domain.llm_interval_forward.replay import ForwardBar


@dataclass(frozen=True)
class _PolicyShape:
    symbol: str


@dataclass(frozen=True)
class _ProposalShape:
    execution_policy: _PolicyShape
    target_session_date: date


@dataclass(frozen=True)
class _BarShape:
    def to_payload(self) -> dict[str, object]:
        return {"close": "100", "timestamp": "2026-08-03T13:31:00Z"}


def _payload(**extra: object) -> dict[str, object]:
    return {
        "kind": INTERVAL_FORWARD_ARTIFACT_KIND,
        "schema_version": INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION,
        "symbol": "NVDA.US",
        "target_session_date": "2026-08-03",
        **extra,
    }


def _encoded_raw(raw: bytes) -> EncodedIntervalForwardArtifact:
    compressed = zlib.compress(
        raw,
        level=INTERVAL_FORWARD_ARTIFACT_COMPRESSION_LEVEL,
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


def _decode(encoded: EncodedIntervalForwardArtifact) -> dict[str, object]:
    return decode_interval_forward_artifact(
        digest_sha256=encoded.digest_sha256,
        schema_version=encoded.schema_version,
        kind=encoded.kind,
        codec=encoded.codec,
        raw_size=encoded.raw_size,
        compressed_size=encoded.compressed_size,
        payload=encoded.payload,
    )


def test_codec_is_canonical_deterministic_and_round_trips() -> None:
    left = _payload(
        bars=[{"timestamp": "2026-08-03T13:31:00Z", "close": "100.20"}],
        unicode="证据",
        integer=2,
    )
    right = {
        "unicode": "证据",
        "integer": 2,
        "target_session_date": "2026-08-03",
        "symbol": "NVDA.US",
        "schema_version": INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION,
        "kind": INTERVAL_FORWARD_ARTIFACT_KIND,
        "bars": [{"close": "100.20", "timestamp": "2026-08-03T13:31:00Z"}],
    }

    raw = canonical_interval_forward_artifact_json(left)
    encoded_left = encode_interval_forward_artifact(left)
    encoded_right = encode_interval_forward_artifact(right)

    assert encoded_left == encoded_right
    assert encoded_left.schema_version == INTERVAL_FORWARD_ARTIFACT_SCHEMA_VERSION
    assert encoded_left.kind == INTERVAL_FORWARD_ARTIFACT_KIND
    assert encoded_left.codec == INTERVAL_FORWARD_ARTIFACT_CODEC
    assert encoded_left.raw_size == len(raw)
    assert encoded_left.compressed_size == len(encoded_left.payload)
    assert encoded_left.digest_sha256 == hashlib.sha256(raw).hexdigest()
    assert encoded_left.digest_sha256 == canonical_sha256(left)
    assert encoded_left.digest_sha256 != hashlib.sha256(
        encoded_left.payload
    ).hexdigest()
    assert zlib.decompress(encoded_left.payload) == raw
    assert _decode(encoded_left) == left


def test_codec_identity_matches_real_source_artifact_payload() -> None:
    proposal = cast(
        ProposalObservation,
        cast(
            object,
            _ProposalShape(
                execution_policy=_PolicyShape(symbol="NVDA.US"),
                target_session_date=date(2026, 8, 3),
            ),
        ),
    )
    bars = cast(
        tuple[ForwardBar, ...],
        cast(object, (_BarShape(),)),
    )
    payload = replay_module.source_artifact_payload(proposal, bars)

    assert payload["kind"] == replay_module.REPLAY_ARTIFACT_KIND
    assert payload["kind"] == INTERVAL_FORWARD_ARTIFACT_KIND
    assert payload["schema_version"] == INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION
    assert _decode(encode_interval_forward_artifact(payload)) == payload


def test_encoded_envelope_is_immutable() -> None:
    encoded = encode_interval_forward_artifact(_payload(value="frozen"))
    with pytest.raises(FrozenInstanceError):
        setattr(encoded, "raw_size", 1)


def test_codec_raw_limit_matches_contract_canonical_limit() -> None:
    assert MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES == 2 * 1024 * 1024
    assert MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES == MAX_CANONICAL_JSON_BYTES
    assert MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES == 512 * 1024


@pytest.mark.parametrize("value", [1.0, -2.5, float("nan"), float("inf")])
def test_encoder_rejects_native_float(value: float) -> None:
    with pytest.raises(IntervalForwardArtifactError, match="canonical JSON"):
        encode_interval_forward_artifact(_payload(value=value))


def test_encoder_validates_payload_kind_and_schema() -> None:
    wrong_kind = _payload()
    wrong_kind["kind"] = "WRONG"
    wrong_schema = _payload()
    wrong_schema["schema_version"] = "v2"

    with pytest.raises(IntervalForwardArtifactError, match="payload kind"):
        encode_interval_forward_artifact(wrong_kind)
    with pytest.raises(IntervalForwardArtifactError, match="payload schema"):
        encode_interval_forward_artifact(wrong_schema)


def test_encoder_enforces_raw_and_compressed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = _payload(chunks=["x" * 1_100] * 2_000)
    with pytest.raises(IntervalForwardArtifactError, match="canonical JSON"):
        encode_interval_forward_artifact(oversized)

    monkeypatch.setattr(
        artifact_module,
        "MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES",
        4,
    )
    with pytest.raises(IntervalForwardArtifactError, match="compressed size"):
        encode_interval_forward_artifact(_payload(value="compress me"))


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("schema_version", True, "schema"),
        ("schema_version", 2, "schema"),
        ("kind", "WRONG", "kind"),
        ("codec", "gzip", "codec"),
        ("raw_size", 0, "raw size"),
        ("raw_size", -1, "raw size"),
        ("raw_size", 1.5, "raw size"),
        ("raw_size", True, "raw size"),
        (
            "raw_size",
            MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES + 1,
            "raw size",
        ),
        ("compressed_size", 0, "compressed size"),
        ("compressed_size", -1, "compressed size"),
        ("compressed_size", 1.5, "compressed size"),
        ("compressed_size", True, "compressed size"),
        (
            "compressed_size",
            MAX_INTERVAL_FORWARD_ARTIFACT_COMPRESSED_BYTES + 1,
            "compressed size",
        ),
    ],
)
def test_decoder_rejects_invalid_fixed_metadata_and_declared_sizes(
    field_name: str,
    value: object,
    message: str,
) -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    invalid = replace(encoded, **{field_name: value})
    with pytest.raises(IntervalForwardArtifactError, match=message):
        _decode(invalid)


def test_decoder_rejects_invalid_digest_and_payload_type() -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    with pytest.raises(IntervalForwardArtifactError, match="SHA-256"):
        _decode(replace(encoded, digest_sha256="A" * 64))
    with pytest.raises(IntervalForwardArtifactError, match="digest mismatch"):
        _decode(replace(encoded, digest_sha256="0" * 64))

    non_bytes = cast(bytes, bytearray(encoded.payload))
    with pytest.raises(IntervalForwardArtifactError, match="must be bytes"):
        _decode(replace(encoded, payload=non_bytes))


@pytest.mark.parametrize(
    "payload",
    [bytearray(b"payload"), memoryview(b"payload"), "payload"],
)
def test_decoder_rejects_every_non_bytes_payload(payload: object) -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    with pytest.raises(IntervalForwardArtifactError, match="must be bytes"):
        _decode(replace(encoded, payload=cast(bytes, payload)))


def test_decoder_rejects_compressed_size_mismatch_and_corruption() -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    with pytest.raises(IntervalForwardArtifactError, match="size.*metadata"):
        _decode(replace(encoded, compressed_size=encoded.compressed_size + 1))

    corrupted = b"not-a-zlib-stream"
    with pytest.raises(IntervalForwardArtifactError, match="decompression"):
        _decode(replace(
            encoded,
            payload=corrupted,
            compressed_size=len(corrupted),
        ))


def test_decoder_rejects_trailing_and_incomplete_zlib_streams() -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    trailing_payload = encoded.payload + zlib.compress(b"{}")
    with pytest.raises(IntervalForwardArtifactError, match="trailing"):
        _decode(replace(
            encoded,
            payload=trailing_payload,
            compressed_size=len(trailing_payload),
        ))

    truncated_payload = encoded.payload[:-1]
    with pytest.raises(
        IntervalForwardArtifactError,
        match="incomplete|decompression",
    ):
        _decode(replace(
            encoded,
            payload=truncated_payload,
            compressed_size=len(truncated_payload),
        ))


def test_decoder_accepts_only_a_complete_zlib_wrapped_stream() -> None:
    payload = _payload(value="canonical")
    raw = canonical_interval_forward_artifact_json(payload)
    encoded = encode_interval_forward_artifact(payload)

    gzip_compressor = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    gzip_payload = gzip_compressor.compress(raw) + gzip_compressor.flush()
    raw_compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw_deflate_payload = raw_compressor.compress(raw) + raw_compressor.flush()
    dictionary_compressor = zlib.compressobj(zdict=b"frozen-dictionary")
    dictionary_payload = (
        dictionary_compressor.compress(raw) + dictionary_compressor.flush()
    )

    for invalid_payload in (
        gzip_payload,
        raw_deflate_payload,
        dictionary_payload,
    ):
        with pytest.raises(IntervalForwardArtifactError, match="decompression"):
            _decode(replace(
                encoded,
                payload=invalid_payload,
                compressed_size=len(invalid_payload),
            ))


def test_invalid_metadata_fails_before_decompressor_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = encode_interval_forward_artifact(_payload(value="canonical"))

    def unexpected_decompressor() -> object:
        raise AssertionError("decompressor must not be created")

    monkeypatch.setattr(artifact_module.zlib, "decompressobj", unexpected_decompressor)
    invalid = replace(
        encoded,
        raw_size=MAX_INTERVAL_FORWARD_ARTIFACT_RAW_BYTES + 1,
    )
    with pytest.raises(IntervalForwardArtifactError, match="raw size"):
        _decode(invalid)


def test_decoder_rejects_bomb_and_raw_size_mismatch() -> None:
    bomb_raw = b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY","padding":"' + (
        b"x" * 100_000
    ) + b'","schema_version":"llm-interval-paired-replay-artifact-v1"}'
    bomb = _encoded_raw(bomb_raw)
    with pytest.raises(IntervalForwardArtifactError, match="exceeds"):
        _decode(replace(bomb, raw_size=20))

    encoded = encode_interval_forward_artifact(_payload(value="canonical"))
    with pytest.raises(IntervalForwardArtifactError, match="size.*metadata"):
        _decode(replace(encoded, raw_size=encoded.raw_size + 1))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1"}',
            "duplicate key",
        ),
        (
            b'{"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY"}',
            "not canonical",
        ),
        (
            b'{ "kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1"}',
            "not canonical",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":1.0}',
            "native JSON float",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":1e0}',
            "native JSON float",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":NaN}',
            "non-finite",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":"\\ud800"}',
            "canonical JSON",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":"\\u8bc1"}',
            "not canonical",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
            b'"value":-0}',
            "not canonical",
        ),
        (
            b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
            b'"nested":{"a":1,"a":2},'
            b'"schema_version":"llm-interval-paired-replay-artifact-v1"}',
            "duplicate key",
        ),
    ],
)
def test_decoder_rejects_duplicate_or_noncanonical_json(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(IntervalForwardArtifactError, match=message):
        _decode(_encoded_raw(raw))


def test_decoder_rejects_oversized_json_integer_before_conversion() -> None:
    oversized = str(MAX_CANONICAL_INTEGER_ABS + 1).encode("ascii")
    raw = (
        b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
        b'"schema_version":"llm-interval-paired-replay-artifact-v1",'
        b'"value":' + oversized + b"}"
    )
    with pytest.raises(IntervalForwardArtifactError, match="oversized JSON integer"):
        _decode(_encoded_raw(raw))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff", "valid JSON"),
        (b"\xef\xbb\xbf{}", "valid JSON"),
        (b"{", "valid JSON"),
        (b"{}{}", "valid JSON"),
        (b"[]", "JSON object"),
        (b"null", "JSON object"),
        (b'"value"', "JSON object"),
    ],
)
def test_decoder_rejects_invalid_utf8_json_and_non_object_root(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(IntervalForwardArtifactError, match=message):
        _decode(_encoded_raw(raw))


def test_decoder_validates_payload_identity_after_digest_verification() -> None:
    wrong_kind = (
        b'{"kind":"WRONG",'
        b'"schema_version":"llm-interval-paired-replay-artifact-v1"}'
    )
    wrong_schema = (
        b'{"kind":"LLM_INTERVAL_PAIRED_FORWARD_REPLAY",'
        b'"schema_version":"v2"}'
    )
    with pytest.raises(IntervalForwardArtifactError, match="payload kind"):
        _decode(_encoded_raw(wrong_kind))
    with pytest.raises(IntervalForwardArtifactError, match="payload schema"):
        _decode(_encoded_raw(wrong_schema))


@pytest.mark.parametrize(
    "payload",
    [
        _payload(**{"k" * (MAX_CANONICAL_KEY_BYTES + 1): "value"}),
        _payload(value="x" * (MAX_CANONICAL_STRING_BYTES + 1)),
        _payload(values=[0] * (MAX_CANONICAL_CONTAINER_ITEMS + 1)),
    ],
)
def test_decoder_reapplies_contract_key_string_and_container_limits(
    payload: dict[str, object],
) -> None:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    with pytest.raises(IntervalForwardArtifactError, match="canonical JSON"):
        _decode(_encoded_raw(raw))


def test_decoder_reapplies_contract_depth_and_node_limits() -> None:
    nested: object = "value"
    for _ in range(MAX_CANONICAL_JSON_DEPTH + 1):
        nested = [nested]
    too_deep = _payload(value=nested)
    inner_width = (MAX_CANONICAL_JSON_NODES // MAX_CANONICAL_CONTAINER_ITEMS) + 1
    too_many_nodes = _payload(
        values=[
            [0] * inner_width
            for _ in range(MAX_CANONICAL_CONTAINER_ITEMS)
        ]
    )

    for payload in (too_deep, too_many_nodes):
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with pytest.raises(IntervalForwardArtifactError, match="canonical JSON"):
            _decode(_encoded_raw(raw))


def test_codec_has_no_io_or_execution_dependencies() -> None:
    source = inspect.getsource(artifact_module)
    forbidden = (
        "app.services",
        "sqlalchemy",
        "SessionLocal",
        "BrokerGateway",
        "TradeExecutionService",
        "submit_order",
    )
    assert all(token not in source for token in forbidden)


def test_canonical_encoder_requires_a_mapping_root() -> None:
    non_mapping = cast(Mapping[str, object], [("kind", "value")])
    with pytest.raises(IntervalForwardArtifactError, match="JSON object"):
        canonical_interval_forward_artifact_json(non_mapping)
