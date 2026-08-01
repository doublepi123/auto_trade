from __future__ import annotations

import ast
import inspect
import re
from types import ModuleType

import pytest

import app.domain.watchlist_quant_v6 as quant_v6_package
import app.domain.watchlist_quant_v6.assessment as assessment_module
import app.domain.watchlist_quant_v6.evaluator as evaluator_module
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_EVALUATOR_MANIFEST_VERSION,
    QUANT_V6_EVALUATOR_SOURCE_KEYS,
    QUANT_V6_SEMANTIC_DIGEST,
    quant_v6_evaluator_digest_sha256,
    quant_v6_evaluator_manifest,
)
from app.domain.watchlist_quant_v6.artifact import quant_v6_payload_sha256


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXPECTED_SOURCE_KEYS = {
    "app.core.holiday_calendar",
    "app.core.market_calendar",
    "app.domain.watchlist_quant_v6.artifact",
    "app.domain.watchlist_quant_v6.assessment",
    "app.domain.watchlist_quant_v6.evaluator",
    "app.domain.watchlist_quant_v6.semantics",
}


def test_evaluator_manifest_has_exact_source_closure() -> None:
    manifest = quant_v6_evaluator_manifest()
    source_sha256 = manifest["source_sha256"]

    assert manifest["manifest_version"] == QUANT_V6_EVALUATOR_MANIFEST_VERSION
    assert manifest["algorithm_version"] == QUANT_V6_ALGORITHM_VERSION
    assert manifest["semantic_digest"] == QUANT_V6_SEMANTIC_DIGEST
    assert set(QUANT_V6_EVALUATOR_SOURCE_KEYS) == _EXPECTED_SOURCE_KEYS
    assert isinstance(source_sha256, dict)
    assert set(source_sha256) == _EXPECTED_SOURCE_KEYS
    assert "app.domain.watchlist_quant_v6.__init__" not in source_sha256
    assert all(
        type(value) is str and _SHA256_PATTERN.fullmatch(value) is not None
        for value in source_sha256.values()
    )


def test_evaluator_digest_is_cached_canonical_manifest_digest() -> None:
    quant_v6_evaluator_digest_sha256.cache_clear()

    first = quant_v6_evaluator_digest_sha256()
    second = quant_v6_evaluator_digest_sha256()

    assert first == quant_v6_payload_sha256(quant_v6_evaluator_manifest())
    assert second == first
    assert _SHA256_PATTERN.fullmatch(first) is not None
    cache_info = quant_v6_evaluator_digest_sha256.cache_info()
    assert cache_info.misses == 1
    assert cache_info.hits == 1


def test_evaluator_manifest_golden_digest() -> None:
    assert quant_v6_evaluator_digest_sha256() == (
        "856cb6c3a2758c4d33fe88e1852e2ac4ce6de3f59936374c6710671d9bd64515"
    )


def test_source_drift_changes_only_its_source_digest_and_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = quant_v6_evaluator_manifest()
    original_source = evaluator_module._repository_module_source_text

    def _drifted_source(module: ModuleType) -> str:
        source = original_source(module)
        if module is assessment_module:
            return f"{source}\n# deliberate assessment source drift\n"
        return source

    monkeypatch.setattr(
        evaluator_module,
        "_repository_module_source_text",
        _drifted_source,
    )
    drifted = quant_v6_evaluator_manifest()

    baseline_sources = baseline["source_sha256"]
    drifted_sources = drifted["source_sha256"]
    assert isinstance(baseline_sources, dict)
    assert isinstance(drifted_sources, dict)
    assert {
        key
        for key in _EXPECTED_SOURCE_KEYS
        if baseline_sources[key] != drifted_sources[key]
    } == {"app.domain.watchlist_quant_v6.assessment"}
    assert quant_v6_payload_sha256(baseline) != quant_v6_payload_sha256(drifted)


def test_repository_source_normalization_is_cross_platform() -> None:
    canonical = "first = 1\nsecond = 2\n"
    variants = (
        "first = 1\nsecond = 2",
        "first = 1  \nsecond = 2\t\n\n",
        "first = 1\r\nsecond = 2\r\n",
        "first = 1  \rsecond = 2\t\r\r",
    )

    assert evaluator_module._normalize_repository_source_text(canonical) == canonical
    assert all(
        evaluator_module._normalize_repository_source_text(value) == canonical
        for value in variants
    )
    assert len({
        evaluator_module._repository_source_sha256(value)
        for value in (*variants, canonical)
    }) == 1
    assert evaluator_module._repository_source_sha256(
        " first = 1\nsecond = 2\n"
    ) != evaluator_module._repository_source_sha256(canonical)


def test_evaluator_closure_has_no_io_framework_or_order_dependency() -> None:
    forbidden_import_prefixes = (
        "app.api",
        "app.database",
        "app.models",
        "app.services",
        "httpx",
        "longport",
        "requests",
        "sqlalchemy",
    )
    forbidden_named_calls = {
        "cancel_order",
        "compile",
        "eval",
        "exec",
        "open",
        "place_order",
        "read_bytes",
        "read_text",
        "replace_order",
        "submit_order",
        "write_bytes",
        "write_text",
    }
    forbidden_attribute_calls = forbidden_named_calls - {"compile", "eval", "exec"}

    for module in evaluator_module._evaluator_source_modules().values():
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            assert not any(
                name == prefix or name.startswith(f"{prefix}.")
                for name in imported
                for prefix in forbidden_import_prefixes
            )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_named_calls
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in forbidden_attribute_calls


def test_package_initializer_is_safe_reexports_only() -> None:
    tree = ast.parse(inspect.getsource(quant_v6_package))
    allowed_modules = {
        "app.domain.watchlist_quant_v6.artifact",
        "app.domain.watchlist_quant_v6.assessment",
        "app.domain.watchlist_quant_v6.evaluator",
        "app.domain.watchlist_quant_v6.semantics",
    }
    imported_names: set[str] = set()
    exported_names: set[str] = set()

    for statement in tree.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and type(statement.value.value) is str
        ):
            continue
        if isinstance(statement, ast.ImportFrom):
            assert statement.level == 0
            assert statement.module in allowed_modules
            assert all(alias.name != "*" for alias in statement.names)
            imported_names.update(
                alias.asname or alias.name for alias in statement.names
            )
            continue
        if isinstance(statement, ast.Assign):
            assert len(statement.targets) == 1
            target = statement.targets[0]
            assert isinstance(target, ast.Name) and target.id == "__all__"
            assert isinstance(statement.value, ast.List)
            assert all(
                isinstance(item, ast.Constant) and type(item.value) is str
                for item in statement.value.elts
            )
            exported_names = {
                item.value
                for item in statement.value.elts
                if isinstance(item, ast.Constant) and type(item.value) is str
            }
            continue
        pytest.fail(
            f"unsafe package initializer statement: {type(statement).__name__}"
        )

    assert exported_names == imported_names
