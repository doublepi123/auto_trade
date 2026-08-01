from __future__ import annotations

import pytest

from app.domain.watchlist_quant_v6 import (
    MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS,
    MAX_QUANT_V6_ARTIFACT_INTEGER_ABS,
    MAX_QUANT_V6_ARTIFACT_JSON_NODES,
    MAX_QUANT_V6_ARTIFACT_KEY_BYTES,
    MAX_QUANT_V6_ARTIFACT_STRING_BYTES,
    QuantV6ArtifactError,
    canonical_quant_v6_json,
)


def test_canonical_codec_rejects_oversized_scalar_inputs() -> None:
    with pytest.raises(QuantV6ArtifactError, match="string size"):
        canonical_quant_v6_json({
            "value": "x" * (MAX_QUANT_V6_ARTIFACT_STRING_BYTES + 1),
        })
    with pytest.raises(QuantV6ArtifactError, match="oversized key"):
        canonical_quant_v6_json({
            "k" * (MAX_QUANT_V6_ARTIFACT_KEY_BYTES + 1): "value",
        })
    with pytest.raises(QuantV6ArtifactError, match="integer limit"):
        canonical_quant_v6_json({
            "value": MAX_QUANT_V6_ARTIFACT_INTEGER_ABS + 1,
        })


def test_canonical_codec_rejects_wide_or_high_node_payloads() -> None:
    with pytest.raises(QuantV6ArtifactError, match="container item"):
        canonical_quant_v6_json({
            "values": [0] * (MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS + 1),
        })

    shared = ["x"] * MAX_QUANT_V6_ARTIFACT_CONTAINER_ITEMS
    repeats = MAX_QUANT_V6_ARTIFACT_JSON_NODES // len(shared) + 1
    with pytest.raises(QuantV6ArtifactError, match="JSON node"):
        canonical_quant_v6_json({"values": [shared] * repeats})


def test_canonical_codec_bounds_json_escape_amplification() -> None:
    # The source string is within its input limit, while JSON escaping each NUL
    # expands it beyond the 2 MiB artifact envelope.
    with pytest.raises(QuantV6ArtifactError, match="raw size"):
        canonical_quant_v6_json({"value": "\x00" * 400_000})
