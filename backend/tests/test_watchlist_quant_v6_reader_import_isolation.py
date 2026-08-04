from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_READER_MODULES = (
    "app.domain.watchlist_quant_v6.evaluator",
    "app.services.watchlist_quant_v6_deadline",
    "app.services.watchlist_quant_v6_evaluation_service",
    "app.services.watchlist_quant_v6_historical_provider",
    "app.services.watchlist_quant_v6_publication_service",
    "app.runner",
    "app.core.broker",
    "app.services.trade_execution_service",
)
_EVALUATOR_EXPORT_NAMES = (
    "QUANT_V6_EVALUATOR_MANIFEST_VERSION",
    "QUANT_V6_EVALUATOR_SOURCE_KEYS",
    "quant_v6_evaluator_digest_sha256",
    "quant_v6_evaluator_manifest",
)


def _run_fresh_python(source: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({
        "AUTO_TRADE_API_KEY": "",
        "AUTO_TRADE_DATABASE_URL": "sqlite://",
        "AUTO_TRADE_ENV": "test",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=_BACKEND_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_reader_api_fresh_import_excludes_execution_and_evaluation_modules(
) -> None:
    source = f"""
import json
import sys

import app.api.watchlist_quant_v6

forbidden = {json.dumps(_FORBIDDEN_READER_MODULES)}
loaded = sorted(
    module_name
    for module_name in sys.modules
    if any(
        module_name == forbidden_name
        or module_name.startswith(forbidden_name + ".")
        for forbidden_name in forbidden
    )
)
print(json.dumps(loaded))
"""
    completed = _run_fresh_python(source)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_package_evaluator_exports_remain_lazy_and_public() -> None:
    source = f"""
import json
import sys

import app.domain.watchlist_quant_v6 as quant_v6

evaluator_module = "app.domain.watchlist_quant_v6.evaluator"
export_names = {json.dumps(_EVALUATOR_EXPORT_NAMES)}
before_access = evaluator_module in sys.modules
values = [getattr(quant_v6, name) for name in export_names]
print(json.dumps({{
    "after_access": evaluator_module in sys.modules,
    "before_access": before_access,
    "callable_exports": [callable(value) for value in values[2:]],
    "exports_present": all(name in quant_v6.__all__ for name in export_names),
    "manifest_version": values[0],
}}))
"""
    completed = _run_fresh_python(source)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "after_access": True,
        "before_access": False,
        "callable_exports": [True, True],
        "exports_present": True,
        "manifest_version": 1,
    }


def test_historical_manifest_lazily_imports_publication_without_cycle() -> None:
    source = """
import json
import sys

from app.services import watchlist_quant_v6_evaluation_service as evaluation

publication_module = "app.services.watchlist_quant_v6_publication_service"
before_manifest = publication_module in sys.modules
manifest = evaluation.quant_v6_historical_evaluator_manifest()
print(json.dumps({
    "after_manifest": publication_module in sys.modules,
    "before_manifest": before_manifest,
    "publication_bound": publication_module in manifest["source_sha256"],
}))
"""
    completed = _run_fresh_python(source)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "after_manifest": True,
        "before_manifest": False,
        "publication_bound": True,
    }
