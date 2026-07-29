"""Platform analytics module catalog service (read-only introspection).

Surfaces the 258+ analytics modules living under ``backend/app/platform/`` as a
queryable catalog. Each module file is parsed with Python's :mod:`ast` module
(never imported — avoids side effects, broker-SDK imports, heavy deps) so the
catalog is safe to call from any read-only API path.

Categories are inferred from the module filename prefix (``factor_*``,
``regime_*``, …); a ``General`` bucket catches everything else. The catalog
also exposes per-module detail (functions, classes, imports, docstring, line
count) for documentation / discovery UIs.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

__all__ = ["PlatformCatalogService"]


# Ordered prefix → category map. Order matters: the first matching prefix
# wins, so more specific prefixes (e.g. ``volatility_``) must come before the
# generic ``vol_`` shortcut. Kept module-private to discourage external mutation.
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("factor_", "Factor"),
    ("regime_", "Regime"),
    ("volatility_", "Volatility"),
    ("vol_", "Volatility"),
    ("risk_", "Risk"),
    ("backtest_", "Backtest"),
    ("portfolio_", "Portfolio"),
    ("signal_", "Signal"),
    ("execution_", "Execution"),
    ("drawdown_", "Drawdown"),
    ("correlation_", "Correlation"),
]

# Filename stem exclusion set: top-level package files and bytecode caches are
# not analytics modules and would distort counts/line numbers if listed.
_EXCLUDED_STEMS: frozenset[str] = frozenset({"__init__"})


def _infer_category(stem: str) -> str:
    """Map a module filename stem to its catalog category.

    The match is case-insensitive on the leading prefix so that both
    ``factor_ic.py`` and ``FactorIC.py`` resolve to ``Factor``. Falls back to
    ``General`` for anything that does not match a known prefix.
    """
    lowered = stem.lower()
    for prefix, category in _CATEGORY_PREFIXES:
        if lowered.startswith(prefix):
            return category
    return "General"


def _resolve_package_dir(package_name: str) -> Path | None:
    """Return the on-disk directory of an importable package, or ``None``.

    Uses :mod:`importlib` so the resolution works regardless of how the
    process was started (pytest, uvicorn, ad-hoc script). Returns ``None`` if
    the package is not importable or has no ``__path__`` (e.g. a namespace
    package without files), so callers can apply a fallback.
    """
    import importlib

    try:
        pkg = importlib.import_module(package_name)
    except ImportError:
        return None
    pkg_path = getattr(pkg, "__path__", None)
    if not pkg_path:
        return None
    return Path(list(pkg_path)[0])


class PlatformCatalogService:
    """Read-only introspection over the ``app.platform`` analytics package.

    The service resolves the package directory lazily on first use (and caches
    it) so importing the module has no filesystem cost. Every method is pure
    with respect to the database — there is no ``Session`` dependency because
    the catalog is sourced entirely from the source tree.
    """

    def __init__(self, platform_dir: Path | None = None) -> None:
        self._platform_dir = platform_dir

    # ------------------------------------------------------------------
    # path resolution
    # ------------------------------------------------------------------

    @property
    def platform_dir(self) -> Path:
        """Resolve (and cache) the on-disk ``app/platform`` directory.

        Uses :mod:`importlib` so the catalog follows the installed package
        location rather than a hard-coded relative path; falls back to a path
        relative to this service file when the package metadata is unavailable.
        """
        if self._platform_dir is not None:
            return self._platform_dir
        resolved = _resolve_package_dir("app.platform")
        if resolved is not None:
            self._platform_dir = resolved
        else:
            # Defensive fallback: two levels up from this service file
            # (services/ -> app/ -> app/platform/).
            self._platform_dir = Path(__file__).resolve().parent.parent / "platform"
        return self._platform_dir

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def list_modules(self) -> list[dict[str, Any]]:
        """Return a sorted summary of every analytics module in the package.

        Each entry contains: ``name``, ``category``, ``description``,
        ``function_count``, ``class_count``. Sorted by name (ascending) so the
        catalog is stable for pagination / diffing.
        """
        summaries: list[dict[str, Any]] = []
        for path in self._iter_module_files():
            stem = path.stem
            if stem in _EXCLUDED_STEMS:
                continue
            tree = self._safe_parse(path)
            if tree is None:
                # Unparseable file: still surface it with zeroed metrics so the
                # catalog is exhaustive, but skip __init__/pycache entirely.
                summaries.append(
                    {
                        "name": stem,
                        "category": _infer_category(stem),
                        "description": "",
                        "function_count": 0,
                        "class_count": 0,
                    }
                )
                continue
            summaries.append(
                {
                    "name": stem,
                    "category": _infer_category(stem),
                    "description": self._extract_docstring(tree),
                    "function_count": self._count_top_level(tree, ast.FunctionDef),
                    "class_count": self._count_top_level(tree, ast.ClassDef),
                }
            )
        summaries.sort(key=lambda item: item["name"])
        return summaries

    def get_module_detail(self, module_name: str) -> dict[str, Any] | None:
        """Return detailed metadata for a single module, or ``None`` if absent.

        Detail payload: ``name``, ``category``, ``description``, ``functions``,
        ``classes``, ``imports``, ``line_count``. Functions are returned as a
        list of ``{name, args, return_type}`` dicts derived from the AST
        signature (positional + keyword-only args flattened for readability).
        """
        if not module_name or module_name in _EXCLUDED_STEMS:
            return None
        path = self.platform_dir / f"{module_name}.py"
        if not path.is_file():
            return None

        tree = self._safe_parse(path)
        if tree is None:
            return {
                "name": module_name,
                "category": _infer_category(module_name),
                "description": "",
                "functions": [],
                "classes": [],
                "imports": [],
                "line_count": self._count_lines(path),
            }

        functions = [_function_signature(node) for node in tree.body if isinstance(node, ast.FunctionDef)]
        classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        imports = _collect_imports(tree)

        return {
            "name": module_name,
            "category": _infer_category(module_name),
            "description": self._extract_docstring(tree),
            "functions": functions,
            "classes": classes,
            "imports": imports,
            "line_count": self._count_lines(path),
        }

    def list_categories(self) -> list[dict[str, Any]]:
        """Aggregate module counts per category, sorted by count (desc) then name."""
        counts: dict[str, int] = {}
        for module in self.list_modules():
            category = module["category"]
            counts[category] = counts.get(category, 0) + 1
        categories = [{"category": name, "count": int(count)} for name, count in counts.items()]
        categories.sort(key=lambda item: (-int(item["count"]), item["category"]))
        return categories

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _iter_module_files(self) -> list[Path]:
        """Yield ``*.py`` files (excluding ``__init__.py`` / pycache)."""
        directory = self.platform_dir
        if not directory.is_dir():
            return []
        return sorted(
            p for p in directory.glob("*.py")
            if p.stem not in _EXCLUDED_STEMS and p.is_file()
        )

    def _safe_parse(self, path: Path) -> ast.Module | None:
        """Parse a source file with :mod:`ast`, returning ``None`` on failure.

        Syntax errors in a single analytics module must never break the whole
        catalog; we surface such modules with zeroed metrics instead.
        """
        try:
            source = path.read_text(encoding="utf-8")
            return ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return None

    @staticmethod
    def _extract_docstring(tree: ast.Module) -> str:
        doc = ast.get_docstring(tree)
        # Trim to the first paragraph to keep list payloads compact; long
        # doctrings inflate the summary response and hurt paginated UIs.
        if not doc:
            return ""
        first_para = doc.split("\n\n", 1)[0]
        # Collapse internal newlines/whitespace inside the paragraph.
        return " ".join(part.strip() for part in first_para.splitlines()).strip()

    @staticmethod
    def _count_top_level(tree: ast.Module, node_type: type[ast.AST]) -> int:
        """Count top-level AST nodes of a given type (skips nested defs)."""
        return sum(1 for node in tree.body if isinstance(node, node_type))

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            # ``splitlines`` is more robust than counting ``\n`` for files that
            # end without a trailing newline.
            return len(path.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            return 0


# ----------------------------------------------------------------------
# module-private AST extraction helpers (no ``self`` — pure functions)
# ----------------------------------------------------------------------


def _format_arg(arg: ast.arg) -> str:
    """Render a single argument including its annotation (if any)."""
    if arg.annotation is None:
        return arg.arg
    return f"{arg.arg}: {_unparse(arg.annotation)}"


def _function_signature(node: ast.FunctionDef) -> dict[str, Any]:
    """Extract ``{name, args, return_type}`` from a top-level function def.

    ``args`` is rendered as a comma-joined string of ``name`` / ``name: annot``
    fragments covering positional, ``*args``, keyword-only and ``**kwargs``;
    ``return_type`` is the annotation string or ``""`` when absent.
    """
    parts: list[str] = []
    a = node.args
    # Positional / pos-or-keyword arguments.
    defaults_offset = len(a.args) - len(a.defaults)
    for idx, arg in enumerate(a.args):
        parts.append(_format_arg(arg))
        # Attach a default value if one exists for this positional arg.
        default_index = idx - defaults_offset
        if default_index >= 0:
            parts[-1] = f"{parts[-1]}={_unparse(a.defaults[default_index])}"
    # *args collector.
    if a.vararg is not None:
        parts.append(f"*{_format_arg(a.vararg)}")
    elif a.kwonlyargs:
        # Bare ``*`` separator is required by Python when kwonlyargs exist
        # without a vararg; surface it so the rendered signature is valid.
        parts.append("*")
    for idx, arg in enumerate(a.kwonlyargs):
        rendered = _format_arg(arg)
        kw_default = a.kw_defaults[idx]
        if kw_default is not None:
            rendered = f"{rendered}={_unparse(kw_default)}"
        parts.append(rendered)
    # **kwargs collector.
    if a.kwarg is not None:
        parts.append(f"**{_format_arg(a.kwarg)}")

    return_type = _unparse(node.returns) if node.returns is not None else ""
    return {
        "name": node.name,
        "args": ", ".join(parts),
        "return_type": return_type,
    }


def _collect_imports(tree: ast.Module) -> list[str]:
    """Collect a de-duplicated, sorted list of import statements as text."""
    seen: set[str] = set()
    out: list[str] = []
    for node in tree.body:
        text: str | None = None
        if isinstance(node, ast.Import):
            text = ", ".join(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            level_dots = "." * (node.level or 0)
            text = f"from {level_dots}{module} import {names}"
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return sorted(out)


def _unparse(node: ast.AST) -> str:
    """Render an AST node back to source. Uses stdlib :func:`ast.unparse`.

    Wrapped so callers don't need to guard :class:`AttributeError` on Python
    < 3.9 (we require 3.11+, but the indirection also lets us swap in a
    fallback renderer later without touching every call site).
    """
    return ast.unparse(node)
