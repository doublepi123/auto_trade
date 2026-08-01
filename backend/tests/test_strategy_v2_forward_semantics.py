from __future__ import annotations

import pytest

import app.domain.strategy_v2.engine as strategy_v2_engine_module
import app.domain.strategy_v2.forward_replay_artifact as replay_artifact_module
from app.domain.strategy_v2.forward_semantics import (
    FORWARD_EXECUTABLE_SEMANTIC_MANIFEST_VERSION,
    forward_executable_semantic_digest,
    forward_executable_semantic_manifest,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


_APPROVED_SEMANTIC_DIGEST = (
    "79a4ec7474dab811b4cadaec97bb41889fc595fa2617f17b44237be62a35f3eb"
)
_APPROVED_NEUTRAL_EVALUATOR_DIGEST = (
    "b636c708537c56e30041ac101c1f631cb58affadadbdd2a660873b34d8ad9a3f"
)
_LEGACY_EVALUATOR_DIGEST = (
    "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
)


def test_forward_semantic_manifest_is_deterministic_and_gap_neutral() -> None:
    first = forward_executable_semantic_manifest()
    second = forward_executable_semantic_manifest()

    assert first == second
    assert first["manifest_version"] == (
        FORWARD_EXECUTABLE_SEMANTIC_MANIFEST_VERSION
    )
    behavior = first["behavior_probe"]
    assert isinstance(behavior, dict)
    assert behavior["neutral_gap_symmetry"] is True
    assert behavior["classic_gap_sensitive"] is True
    assert behavior["classic_positive_prefix"] != (
        behavior["neutral_positive_prefix"]
    )


def test_forward_evaluator_digests_are_pinned_and_independent() -> None:
    assert forward_executable_semantic_digest(
        extra_executables=(
            StrategyV2ShadowService._forward_semantic_executables()
        ),
        extra_semantic_constants=(
            StrategyV2ShadowService._forward_semantic_constants()
        ),
    ) == _APPROVED_SEMANTIC_DIGEST
    assert StrategyV2ShadowService._forward_executable_semantic_digest() == (
        _APPROVED_SEMANTIC_DIGEST
    )
    assert StrategyV2ShadowService._forward_evaluator_digest() == (
        _LEGACY_EVALUATOR_DIGEST
    )
    assert StrategyV2ShadowService._forward_evaluator_digest_for(
        "strategy-v2-causal-trend-prewarm-boundary-neutral-v1"
    ) == _APPROVED_NEUTRAL_EVALUATOR_DIGEST


def test_forward_semantic_dependency_tables_cover_replay_lifecycle() -> None:
    executables = StrategyV2ShadowService._forward_semantic_executables()
    constants = StrategyV2ShadowService._forward_semantic_constants()
    manifest = forward_executable_semantic_manifest(
        extra_executables=executables,
        extra_semantic_constants=constants,
    )

    assert {
        "aggregate_forward_metrics",
        "attach_forward_replay_artifact",
        "baseline_replay_matches",
        "bars_from_persisted_evidence",
        "causal_entry_feature",
        "collect_forward_validation",
        "domain_config",
        "fee_rate",
        "forward_candidate_spec_for",
        "forward_collection_continues_after_maturity",
        "forward_collection_phase",
        "forward_evidence_digest",
        "forward_evidence_duplicate_matches",
        "forward_replay_artifact_is_prune_safe",
        "forward_replay_bundle_payload",
        "forward_replay_input_hash",
        "forward_result_json",
        "forward_source_trace_matches",
        "metrics_from_replay",
        "observation_schedule",
        "persist_forward_exclusion",
        "replay_payload",
        "session_local_features_match",
        "validated_forward_replay_artifact_payload",
        "warmup_daily_from_replay",
    } <= set(executables)
    assert {
        "forward_boundary_neutral_evaluator_version",
        "forward_candidate_versions",
        "forward_finalize_end_minutes",
        "forward_finalize_start_minutes",
        "forward_frozen_collection_identities",
        "forward_frozen_evaluator_digest",
        "forward_incomplete_deadline_minutes",
        "forward_legacy_candidate_version",
        "forward_legacy_evaluator_digest",
        "forward_mature_pairs",
        "forward_ready_pairs",
    } <= set(constants)
    executable_ast = manifest["executable_ast"]
    semantic_constants = manifest["semantic_constants"]
    assert isinstance(executable_ast, dict)
    assert isinstance(semantic_constants, dict)
    assert set(executables) <= set(executable_ast)
    assert set(constants) <= set(semantic_constants)


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        (
            replay_artifact_module,
            "MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES",
        ),
        (
            replay_artifact_module,
            "MAX_FORWARD_REPLAY_ARTIFACT_JSON_DEPTH",
        ),
        (
            strategy_v2_engine_module,
            "CAUSAL_ENTRY_FILL_OFFSET_BARS",
        ),
    ],
)
def test_forward_semantic_digest_tracks_limits_and_fill_offset(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    attribute: str,
) -> None:
    executables = StrategyV2ShadowService._forward_semantic_executables()
    constants = StrategyV2ShadowService._forward_semantic_constants()
    baseline = forward_executable_semantic_digest(
        extra_executables=executables,
        extra_semantic_constants=constants,
    )
    original = getattr(module, attribute)

    monkeypatch.setattr(module, attribute, original + 1)

    assert forward_executable_semantic_digest(
        extra_executables=executables,
        extra_semantic_constants=(
            StrategyV2ShadowService._forward_semantic_constants()
        ),
    ) != baseline


def test_legacy_forward_evaluator_digest_remains_frozen() -> None:
    assert StrategyV2ShadowService._forward_evaluator_digest() == (
        _LEGACY_EVALUATOR_DIGEST
    )
