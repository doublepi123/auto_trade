"""Tests for PlatformCatalogService and the /api/platform-catalog router."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.platform_catalog import router as platform_catalog_router
from app.services.platform_catalog_service import (
    PlatformCatalogService,
    _infer_category,
)


# ----------------------------------------------------------------------
# shared fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def service() -> PlatformCatalogService:
    return PlatformCatalogService()


@pytest.fixture
def client() -> TestClient:
    """Standalone FastAPI app mounting only the catalog router.

    Avoids pulling in the full lifespan/runner wiring of ``app.main.app`` —
    the catalog is source-tree-only and has no DB dependency, so a minimal app
    is faster and isolates failures.
    """
    app = FastAPI()
    app.include_router(platform_catalog_router)
    return TestClient(app)


# ----------------------------------------------------------------------
# service: list_modules
# ----------------------------------------------------------------------


def test_list_modules_returns_non_empty(service: PlatformCatalogService) -> None:
    """The platform package ships 258+ modules, so the catalog must not be empty."""
    modules = service.list_modules()
    assert isinstance(modules, list)
    assert len(modules) > 0

    # Every summary must carry the documented fields with sane types.
    required_keys = {"name", "category", "description", "function_count", "class_count"}
    for module in modules:
        assert required_keys <= set(module), f"missing keys in {module}"
        assert isinstance(module["name"], str) and module["name"]
        assert isinstance(module["category"], str) and module["category"]
        assert isinstance(module["description"], str)
        assert isinstance(module["function_count"], int) and module["function_count"] >= 0
        assert isinstance(module["class_count"], int) and module["class_count"] >= 0


def test_list_modules_excludes_dunder_files(service: PlatformCatalogService) -> None:
    """``__init__.py`` and pycache must never appear in the catalog."""
    modules = service.list_modules()
    names = {m["name"] for m in modules}
    assert "__init__" not in names
    # Sorted ascending by name for stable pagination.
    sorted_names = [m["name"] for m in modules]
    assert sorted_names == sorted(sorted_names)


def test_list_modules_includes_known_module(service: PlatformCatalogService) -> None:
    """``risk_metrics`` is a known platform module used in detail tests below."""
    names = {m["name"] for m in service.list_modules()}
    assert "risk_metrics" in names
    risk_entry = next(m for m in service.list_modules() if m["name"] == "risk_metrics")
    assert risk_entry["category"] == "Risk"


# ----------------------------------------------------------------------
# service: category inference
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("factor_ic", "Factor"),
        ("regime_hmm", "Regime"),
        ("volatility_regime", "Volatility"),
        ("vol_targeting", "Volatility"),
        ("risk_metrics", "Risk"),
        ("backtest_diagnostics", "Backtest"),
        ("portfolio_allocator", "Portfolio"),
        ("signal_decay", "Signal"),
        ("execution_algorithms", "Execution"),
        ("drawdown_analysis", "Drawdown"),
        ("correlation_network", "Correlation"),
        ("heston", "General"),
        ("analyzers", "General"),
    ],
)
def test_infer_category(stem: str, expected: str) -> None:
    assert _infer_category(stem) == expected


# ----------------------------------------------------------------------
# service: get_module_detail
# ----------------------------------------------------------------------


def test_get_module_detail_risk_metrics(service: PlatformCatalogService) -> None:
    detail = service.get_module_detail("risk_metrics")
    assert detail is not None
    assert detail["name"] == "risk_metrics"
    assert detail["category"] == "Risk"
    assert isinstance(detail["description"], str)
    assert detail["line_count"] > 0
    # risk_metrics.py defines multiple top-level functions (historical_var, etc.)
    assert len(detail["functions"]) > 0
    function_names = {fn["name"] for fn in detail["functions"]}
    assert "historical_var" in function_names

    # Function entries must carry the rendered signature shape.
    fn = next(f for f in detail["functions"] if f["name"] == "historical_var")
    assert isinstance(fn["args"], str)
    assert isinstance(fn["return_type"], str)

    assert isinstance(detail["classes"], list)
    assert isinstance(detail["imports"], list)
    assert len(detail["imports"]) > 0


def test_get_module_detail_returns_none_for_missing(service: PlatformCatalogService) -> None:
    assert service.get_module_detail("this_module_does_not_exist_xyz") is None


def test_get_module_detail_returns_none_for_dunder(service: PlatformCatalogService) -> None:
    assert service.get_module_detail("__init__") is None


def test_get_module_detail_signatures_match_ast(service: PlatformCatalogService) -> None:
    """The rendered args/return_type must round-trip against the raw AST."""
    detail = service.get_module_detail("risk_metrics")
    assert detail is not None

    source = (service.platform_dir / "risk_metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    expected = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for fn in detail["functions"]:
        assert fn["name"] in expected, f"{fn['name']} not in source"
        returns_node = expected[fn["name"]].returns
        assert fn["return_type"] == (ast.unparse(returns_node) if returns_node is not None else "")


# ----------------------------------------------------------------------
# service: list_categories
# ----------------------------------------------------------------------


def test_list_categories_aggregates_counts(service: PlatformCatalogService) -> None:
    categories = service.list_categories()
    assert len(categories) > 0
    total = sum(c["count"] for c in categories)
    assert total == len(service.list_modules())
    # Sorted by count desc, then category name asc.
    keys = [(-c["count"], c["category"]) for c in categories]
    assert keys == sorted(keys)


# ----------------------------------------------------------------------
# router: GET /modules
# ----------------------------------------------------------------------


def test_api_list_modules_returns_non_empty(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules")
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0


def test_api_list_modules_category_filter(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules", params={"category": "Risk"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    assert all(m["category"] == "Risk" for m in data)
    # risk_metrics must appear under the Risk category.
    assert any(m["name"] == "risk_metrics" for m in data)


def test_api_list_modules_category_filter_case_insensitive(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules", params={"category": "risk"})
    assert response.status_code == 200
    assert len(response.json()) > 0


def test_api_list_modules_category_filter_unknown_returns_empty(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules", params={"category": "Nonexistent"})
    assert response.status_code == 200
    assert response.json() == []


def test_api_list_modules_search_filter(client: TestClient) -> None:
    # Search for a substring guaranteed to appear in some module name/description.
    response = client.get("/api/platform-catalog/modules", params={"search": "regime"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    # Every result must contain "regime" in name or description (lowercased).
    for module in data:
        blob = f"{module['name']} {module.get('description', '')}".lower()
        assert "regime" in blob


def test_api_list_modules_search_and_category_compose(client: TestClient) -> None:
    response = client.get(
        "/api/platform-catalog/modules",
        params={"category": "Regime", "search": "regime"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    assert all(m["category"] == "Regime" for m in data)


# ----------------------------------------------------------------------
# router: GET /modules/{module_name}
# ----------------------------------------------------------------------


def test_api_get_module_detail_ok(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules/risk_metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "risk_metrics"
    assert data["category"] == "Risk"
    assert data["line_count"] > 0
    assert len(data["functions"]) > 0


def test_api_get_module_detail_not_found(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/modules/no_such_module_xyz")
    assert response.status_code == 404


# ----------------------------------------------------------------------
# router: GET /categories
# ----------------------------------------------------------------------


def test_api_list_categories(client: TestClient) -> None:
    response = client.get("/api/platform-catalog/categories")
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    total = sum(c["count"] for c in data)
    # Cross-check against the live modules list.
    modules_response = client.get("/api/platform-catalog/modules")
    assert modules_response.status_code == 200
    assert total == len(modules_response.json())


# ----------------------------------------------------------------------
# robustness: synthetic module parsing (no side effects on real package)
# ----------------------------------------------------------------------


def test_service_handles_synthetic_directory(tmp_path: Path) -> None:
    """A service pointed at an arbitrary dir parses only the .py files there."""
    (tmp_path / "factor_demo.py").write_text(
        '"""Demo factor module."""\n\n\ndef alpha(x: float) -> float:\n    return x\n\n\nclass Factor:\n    pass\n',
        encoding="utf-8",
    )
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def bad(:\n", encoding="utf-8")

    svc = PlatformCatalogService(platform_dir=tmp_path)
    modules = svc.list_modules()
    names = {m["name"] for m in modules}
    # __init__ excluded; broken.py surfaces with zeroed metrics.
    assert names == {"factor_demo", "broken"}
    broken = next(m for m in modules if m["name"] == "broken")
    assert broken["function_count"] == 0
    assert broken["category"] == "General"

    detail = svc.get_module_detail("factor_demo")
    assert detail is not None
    assert detail["description"] == "Demo factor module."
    assert detail["category"] == "Factor"
    assert len(detail["functions"]) == 1
    assert detail["functions"][0]["name"] == "alpha"
    assert "x: float" in detail["functions"][0]["args"]
    assert detail["functions"][0]["return_type"] == "float"
    assert detail["classes"] == ["Factor"]
    assert detail["line_count"] > 0


def test_service_resolves_real_package_when_dir_unset() -> None:
    """Default construction must resolve the real ``app.platform`` package dir."""
    svc = PlatformCatalogService()
    directory = svc.platform_dir
    pkg = importlib.import_module("app.platform")
    assert directory.resolve() == Path(list(pkg.__path__)[0]).resolve()
