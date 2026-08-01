from __future__ import annotations

import ast
import hashlib
import sys

import pytest

import app.domain.strategy_v2.engine as strategy_v2_engine_module
import app.domain.strategy_v2.forward_replay_artifact as replay_artifact_module
from app.domain.strategy_v2.forward_semantics import (
    FORWARD_EXECUTABLE_SEMANTIC_MANIFEST_VERSION,
    _canonical_ast_dump,
    _canonical_executable_source,
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


def test_pinned_full_semantic_digest_is_runtime_stable() -> None:
    # The approved digest was produced on Python 3.11 and must remain stable
    # across supported interpreters without changing the pin.
    assert forward_executable_semantic_digest(
        extra_executables=(
            StrategyV2ShadowService._forward_semantic_executables()
        ),
        extra_semantic_constants=(
            StrategyV2ShadowService._forward_semantic_constants()
        ),
    ) == _APPROVED_SEMANTIC_DIGEST


def test_type_params_literal_string_is_preserved_in_canonical_output() -> None:
    # A function returning the literal ", type_params=[]" must keep that
    # literal in the canonical representation; it is string data, not the
    # AST field. Changing only the literal must change the canonical output.
    source = "def f():\n    return ', type_params=[]'\n"
    canonical = _canonical_executable_source(source)
    assert ", type_params=[]" in canonical
    # The FunctionDef itself must NOT carry a type_params field (non-generic).
    # The field would appear as a sibling after decorator_list; the literal
    # appears only inside the Constant value, so check the field separator
    # sequence rather than the bare substring.
    assert "decorator_list=[], type_params=" not in canonical

    altered = _canonical_executable_source("def f():\n    return ', type_params=[X]'\n")
    assert canonical != altered
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() != (
        hashlib.sha256(altered.encode("utf-8")).hexdigest()
    )


def test_empty_python311_fields_remain_in_canonical_output() -> None:
    # Representative empty list fields that existed on Python 3.11 must still
    # appear in the canonical shape; only empty type_params is normalized away.
    canonical = _canonical_executable_source("def f():\n    pass\n")
    assert "posonlyargs=[]" in canonical
    assert "kwonlyargs=[]" in canonical
    assert "defaults=[]" in canonical
    assert "decorator_list=[]" in canonical
    assert "type_ignores=[]" in canonical
    # The normalized field must be absent for a non-generic definition.
    assert "type_params" not in canonical


def test_canonical_ast_dump_rejects_missing_declared_field() -> None:
    tree = ast.parse("value = 1\n")
    assignment = tree.body[0]
    assert isinstance(assignment, ast.Assign)
    del assignment.value

    with pytest.raises(ValueError, match=r"Assign\.value"):
        _canonical_ast_dump(tree)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="PEP 695 generic syntax requires Python 3.12+",
)
def test_nonempty_type_params_remains_for_generic_sync_function() -> None:
    # On Python >=3.12, a non-empty type_params must remain digest-significant.
    canonical_t = _canonical_executable_source("def f[T]():\n    pass\n")
    assert "type_params=[TypeVar(name='T')]" in canonical_t

    canonical_u = _canonical_executable_source("def f[U]():\n    pass\n")
    assert "type_params=[TypeVar(name='U')]" in canonical_u
    assert canonical_t != canonical_u


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="PEP 695 generic syntax requires Python 3.12+",
)
def test_nonempty_type_params_remains_for_generic_async_function() -> None:
    canonical = _canonical_executable_source("async def f[T]():\n    pass\n")
    assert "AsyncFunctionDef(" in canonical
    assert "type_params=[TypeVar(name='T')]" in canonical


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="PEP 695 generic syntax requires Python 3.12+",
)
def test_nonempty_type_params_remains_for_generic_class() -> None:
    canonical = _canonical_executable_source("class C[T]:\n    pass\n")
    assert "ClassDef(" in canonical
    assert "type_params=[TypeVar(name='T')]" in canonical


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="native ast.dump comparison is specific to Python 3.12",
)
def test_canonical_output_matches_native_ast_dump_on_python_312_for_generics() -> None:
    # On Python 3.12 the serializer must agree with native ast.dump whenever
    # type_params is non-empty (the only divergence is the empty-list omission).
    for source in (
        "def f[T]():\n    pass\n",
        "async def f[T]():\n    pass\n",
        "class C[T]:\n    pass\n",
    ):
        tree = ast.parse(source)
        native = ast.dump(tree, annotate_fields=True, include_attributes=False)
        assert _canonical_executable_source(source) == native
