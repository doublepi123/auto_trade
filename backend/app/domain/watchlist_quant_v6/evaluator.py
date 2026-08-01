"""Cross-interpreter source manifest for the frozen quant-v6 evaluator."""

from __future__ import annotations

import hashlib
import inspect
import sys
from collections.abc import Mapping
from functools import lru_cache
from types import ModuleType

import app.core.holiday_calendar as holiday_calendar_module
import app.core.market_calendar as market_calendar_module
import app.domain.watchlist_quant_v6.artifact as artifact_module
import app.domain.watchlist_quant_v6.assessment as assessment_module
import app.domain.watchlist_quant_v6.semantics as semantics_module
from app.domain.watchlist_quant_v6.artifact import quant_v6_payload_sha256
from app.domain.watchlist_quant_v6.semantics import (
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_SEMANTIC_DIGEST,
)


QUANT_V6_EVALUATOR_MANIFEST_VERSION = 1
QUANT_V6_EVALUATOR_SOURCE_KEYS = (
    "app.core.holiday_calendar",
    "app.core.market_calendar",
    "app.domain.watchlist_quant_v6.artifact",
    "app.domain.watchlist_quant_v6.assessment",
    "app.domain.watchlist_quant_v6.evaluator",
    "app.domain.watchlist_quant_v6.semantics",
)


def _normalize_repository_source_text(source: str) -> str:
    """Normalize source without interpreter-specific parsing or compilation."""
    if type(source) is not str:
        raise TypeError("repository source must be text")
    unix_source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unix_source.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    normalized = "\n".join(lines)
    return f"{normalized}\n"


def _repository_source_sha256(source: str) -> str:
    normalized = _normalize_repository_source_text(source)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _repository_module_source_text(module: ModuleType) -> str:
    try:
        return inspect.getsource(module)
    except (OSError, TypeError) as exc:
        raise RuntimeError(
            f"repository source is unavailable for {module.__name__}"
        ) from exc


def _evaluator_source_modules() -> Mapping[str, ModuleType]:
    modules: dict[str, ModuleType] = {
        "app.core.holiday_calendar": holiday_calendar_module,
        "app.core.market_calendar": market_calendar_module,
        "app.domain.watchlist_quant_v6.artifact": artifact_module,
        "app.domain.watchlist_quant_v6.assessment": assessment_module,
        "app.domain.watchlist_quant_v6.evaluator": sys.modules[__name__],
        "app.domain.watchlist_quant_v6.semantics": semantics_module,
    }
    if tuple(sorted(modules)) != QUANT_V6_EVALUATOR_SOURCE_KEYS:
        raise RuntimeError("quant-v6 evaluator source closure is incomplete")
    return modules


def quant_v6_evaluator_manifest() -> dict[str, object]:
    """Return the exact executable-source closure for quant-v6 evidence."""
    source_modules = _evaluator_source_modules()
    return {
        "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
        "manifest_version": QUANT_V6_EVALUATOR_MANIFEST_VERSION,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "source_sha256": {
            key: _repository_source_sha256(
                _repository_module_source_text(source_modules[key])
            )
            for key in QUANT_V6_EVALUATOR_SOURCE_KEYS
        },
    }


@lru_cache(maxsize=1)
def quant_v6_evaluator_digest_sha256() -> str:
    """Return the canonical, process-cached digest of the evaluator manifest."""
    return quant_v6_payload_sha256(quant_v6_evaluator_manifest())


__all__ = [
    "QUANT_V6_EVALUATOR_MANIFEST_VERSION",
    "QUANT_V6_EVALUATOR_SOURCE_KEYS",
    "quant_v6_evaluator_digest_sha256",
    "quant_v6_evaluator_manifest",
]
