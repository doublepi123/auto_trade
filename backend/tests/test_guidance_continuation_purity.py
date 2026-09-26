"""Purity contract for the guidance_continuation domain package.

Mirrors ``app/domain/AGENTS.md``: allowed imports are stdlib, other domain
modules, and ``app.core`` calendar utilities only.  No services / api /
platform / settings, no SQLAlchemy, no ``datetime.now`` without injection.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "app" / "domain" / "guidance_continuation"

FORBIDDEN_PREFIXES = (
    "app.services",
    "app.api",
    "app.platform",
    "app.config",
    "app.models",
    "app.database",
    "sqlalchemy",
)


def _import_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


class TestPurity:
    def test_no_forbidden_imports(self) -> None:
        violations: list[str] = []
        for path in sorted(PKG.glob("*.py")):
            tree = ast.parse(path.read_text())
            for name in _import_names(tree):
                if name.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.name}: {name}")
        assert not violations, f"forbidden imports: {violations}"

    def test_no_datetime_now_or_utcnow(self) -> None:
        offenders: list[str] = []
        for path in sorted(PKG.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "now",
                    "utcnow",
                    "today",
                }:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"wall-clock reads: {offenders}"
