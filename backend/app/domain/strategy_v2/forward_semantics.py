from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import textwrap
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import app.core.holiday_calendar as holiday_calendar_module
import app.core.market_calendar as market_calendar_module
import app.domain.strategy_v2.engine as strategy_v2_engine_module
import app.domain.strategy_v2.features as strategy_v2_features_module
import app.domain.strategy_v2.forward_replay_artifact as replay_artifact_module
from app.domain.strategy_v2.engine import StrategyV2Engine
from app.domain.strategy_v2.features import (
    BoundaryNeutralCausalTrendPrewarmFeatureEngine,
    CausalTrendPrewarmFeatureEngine,
    StrategyBar,
    boundary_neutral_wilder_adx,
    wilder_adx,
)


FORWARD_EXECUTABLE_SEMANTIC_MANIFEST_VERSION = 2


# Node classes whose ``type_params`` field was added in Python 3.12. The pinned
# digest was produced on Python 3.11, where this field does not exist, so an
# empty ``type_params=[]`` must be omitted to keep the canonical shape stable
# across interpreters. A non-empty ``type_params`` (a real generic) is
# recursively serialized and remains digest-significant.
_TYPE_PARAMS_OMITTING_NODES: tuple[type, ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


class _StripDocstrings(ast.NodeTransformer):
    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            return body[1:]
        return body

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.body = self._without_docstring(node.body)
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.body = self._without_docstring(node.body)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.body = self._without_docstring(node.body)
        return self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> ast.AST:
        node.body = self._without_docstring(node.body)
        return self.generic_visit(node)


def _canonical_ast_dump(node: ast.AST) -> str:
    """Structural canonical serializer fixed to the Python 3.11 dump shape.

    Replicates ``ast.dump(node, annotate_fields=True, include_attributes=False)``
    as it behaved on Python 3.11, without calling native ``ast.dump`` (whose
    output drifts across interpreter versions as new ``_fields`` are added).
    The serializer recursively includes every normal AST ``_fields`` field,
    lists, and scalar ``repr``; it omits optional class-default ``None`` fields
    exactly as 3.11 did, and ignores attributes/locations as today.

    The single runtime-only normalization: omit ``type_params`` for
    ``ast.FunctionDef``, ``ast.AsyncFunctionDef``, and ``ast.ClassDef`` when
    the parsed child value is an empty list (the 3.12 default for non-generic
    definitions). A present ``type_params`` must be a list; non-empty values are
    recursively serialized and remain digest-significant. Unknown future
    fields are included by default so the serializer degrades safely.
    """

    def _format(value: object) -> str:
        if isinstance(value, ast.AST):
            cls = type(value)
            parts: list[str] = []
            for name in value._fields:
                try:
                    child = getattr(value, name)
                except AttributeError as exc:
                    raise ValueError(
                        f"parsed AST missing {cls.__name__}.{name}"
                    ) from exc
                # Normalize the one runtime-only case: an empty type_params
                # list on a def/class node did not exist on 3.11 and must not
                # appear in the canonical shape.
                if (
                    name == "type_params"
                    and isinstance(value, _TYPE_PARAMS_OMITTING_NODES)
                ):
                    if not isinstance(child, list):
                        raise TypeError(
                            "type_params must be a list, got "
                            f"{type(child).__name__}"
                        )
                    if not child:
                        continue
                # Retain 3.11 treatment of optional class-default None fields:
                # omit when the value is None and the class attribute is None.
                if child is None and getattr(cls, name, ...) is None:
                    continue
                formatted = _format(child)
                parts.append(f"{name}={formatted}")
            return f"{cls.__name__}({', '.join(parts)})"
        if isinstance(value, list):
            if not value:
                return "[]"
            return f"[{', '.join(_format(item) for item in value)}]"
        return repr(value)

    return _format(node)


def _canonical_executable_source(source: str) -> str:
    """Canonical AST serialization for a source string, for direct tests.

    Mirrors ``_executable_ast`` but accepts source text so tests can exercise
    the serializer without a live object: parse -> strip docstrings ->
    fix_missing locations -> canonical dump.
    """

    tree = ast.parse(source)
    normalized = _StripDocstrings().visit(tree)
    ast.fix_missing_locations(normalized)
    return _canonical_ast_dump(normalized)


def _executable_ast(value: object) -> str:
    source = textwrap.dedent(inspect.getsource(cast(Any, value)))
    tree = ast.parse(source)
    normalized = _StripDocstrings().visit(tree)
    ast.fix_missing_locations(normalized)
    return _canonical_ast_dump(normalized)


def _probe_bar(index: int, *, day: int, shift: float) -> StrategyBar:
    base = 100.0 + shift + index * 0.035 + 0.22 * math.sin(index / 2.5)
    opened = base - 0.025 * math.cos(index / 3.0)
    return StrategyBar(
        timestamp=datetime(2026, 7, day, 13, 30, tzinfo=timezone.utc)
        + timedelta(minutes=5 * index),
        open=opened,
        high=max(opened, base) + 0.11,
        low=min(opened, base) - 0.09,
        close=base,
        volume=10_000.0 + index,
        symbol="SEMANTIC.US",
        duration_minutes=5,
    )


def _float_hex(value: float | None) -> str | None:
    return None if value is None else float(value).hex()


def _behavior_probe() -> dict[str, object]:
    seed = [_probe_bar(index, day=6, shift=0.0) for index in range(30)]
    positive = [_probe_bar(index, day=7, shift=20.0) for index in range(10)]
    negative = [_probe_bar(index, day=7, shift=-20.0) for index in range(10)]
    positive_prefix = [
        _float_hex(wilder_adx([*seed, *positive[:index]], period=14))
        for index in range(1, len(positive) + 1)
    ]
    neutral_positive_prefix = [
        _float_hex(boundary_neutral_wilder_adx(
            [*seed, *positive[:index]],
            boundary_at=positive[0].timestamp,
            period=14,
        ))
        for index in range(1, len(positive) + 1)
    ]
    neutral_negative_prefix = [
        _float_hex(boundary_neutral_wilder_adx(
            [*seed, *negative[:index]],
            boundary_at=negative[0].timestamp,
            period=14,
        ))
        for index in range(1, len(negative) + 1)
    ]
    return {
        "classic_positive_prefix": positive_prefix,
        "neutral_positive_prefix": neutral_positive_prefix,
        "neutral_negative_prefix": neutral_negative_prefix,
        "neutral_gap_symmetry": (
            neutral_positive_prefix == neutral_negative_prefix
        ),
        "classic_gap_sensitive": (
            _float_hex(wilder_adx([*seed, *positive], period=14))
            != _float_hex(wilder_adx([*seed, *negative], period=14))
        ),
    }


def forward_executable_semantic_manifest(
    *,
    extra_executables: Mapping[str, object] | None = None,
    extra_semantic_constants: Mapping[str, object] | None = None,
) -> dict[str, object]:
    executables: dict[str, object] = {
        "boundary_neutral_feature_engine": (
            BoundaryNeutralCausalTrendPrewarmFeatureEngine
        ),
        "boundary_neutral_wilder_adx": boundary_neutral_wilder_adx,
        "legacy_feature_engine": CausalTrendPrewarmFeatureEngine,
        "legacy_wilder_adx": wilder_adx,
        "market_calendar_module": market_calendar_module,
        "market_holiday_module": holiday_calendar_module,
        "replay_artifact_module": replay_artifact_module,
        "strategy_v2_engine_module": strategy_v2_engine_module,
        "strategy_v2_engine": StrategyV2Engine,
        "strategy_v2_features_module": strategy_v2_features_module,
    }
    if extra_executables is not None:
        overlap = set(executables) & set(extra_executables)
        if overlap:
            raise ValueError("duplicate forward semantic executable label")
        executables.update(extra_executables)
    semantic_constants: dict[str, object] = {
        "artifact_codec": replay_artifact_module.FORWARD_REPLAY_ARTIFACT_CODEC,
        "artifact_kind": replay_artifact_module.FORWARD_REPLAY_ARTIFACT_KIND,
        "artifact_max_compressed_bytes": (
            replay_artifact_module.MAX_FORWARD_REPLAY_ARTIFACT_COMPRESSED_BYTES
        ),
        "artifact_max_json_depth": (
            replay_artifact_module.MAX_FORWARD_REPLAY_ARTIFACT_JSON_DEPTH
        ),
        "artifact_max_raw_bytes": (
            replay_artifact_module.MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES
        ),
        "artifact_role": replay_artifact_module.FORWARD_REPLAY_ARTIFACT_ROLE,
        "artifact_schema_version": (
            replay_artifact_module.FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION
        ),
        "boundary_neutral_algorithm_version": (
            strategy_v2_features_module.BOUNDARY_NEUTRAL_PREWARM_ALGORITHM_VERSION
        ),
        "causal_entry_fill_offset_bars": (
            strategy_v2_engine_module.CAUSAL_ENTRY_FILL_OFFSET_BARS
        ),
    }
    if extra_semantic_constants is not None:
        overlap = set(semantic_constants) & set(extra_semantic_constants)
        if overlap:
            raise ValueError("duplicate forward semantic constant label")
        semantic_constants.update(extra_semantic_constants)
    return {
        "manifest_version": FORWARD_EXECUTABLE_SEMANTIC_MANIFEST_VERSION,
        "behavior_probe": _behavior_probe(),
        "semantic_constants": dict(sorted(semantic_constants.items())),
        "executable_ast": {
            key: _executable_ast(value)
            for key, value in sorted(executables.items())
        },
    }


def forward_executable_semantic_digest(
    *,
    extra_executables: Mapping[str, object] | None = None,
    extra_semantic_constants: Mapping[str, object] | None = None,
) -> str:
    encoded = json.dumps(
        forward_executable_semantic_manifest(
            extra_executables=extra_executables,
            extra_semantic_constants=extra_semantic_constants,
        ),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
