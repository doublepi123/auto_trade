"""Platform analytics module catalog API (GET /api/platform-catalog/*).

Read-only introspection over the ``app.platform`` analytics package. All
responses are derived purely from the source tree (parsed with :mod:`ast`,
never imported) so the endpoints are safe to call from any context and have no
database or broker dependency.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.services.platform_catalog_service import PlatformCatalogService

router = APIRouter(
    prefix="/api/platform-catalog",
    tags=["platform-catalog"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# response schemas (local to this router — see AGENTS.md "schemas in router")
# ----------------------------------------------------------------------


class ModuleSummary(BaseModel):
    name: str = Field(..., description="Module filename without the .py suffix")
    category: str = Field(..., description="Inferred category (Factor, Regime, …)")
    description: str = Field("", description="First paragraph of the module docstring")
    function_count: int = Field(0, ge=0, description="Top-level function definitions")
    class_count: int = Field(0, ge=0, description="Top-level class definitions")


class ModuleFunction(BaseModel):
    name: str
    args: str = Field("", description="Rendered argument list (positional + kw)")
    return_type: str = Field("", description="Return annotation, empty when absent")


class ModuleDetail(BaseModel):
    name: str
    category: str
    description: str
    functions: list[ModuleFunction]
    classes: list[str]
    imports: list[str]
    line_count: int = Field(0, ge=0)


class CategoryCount(BaseModel):
    category: str
    count: int = Field(0, ge=0)


# ----------------------------------------------------------------------
# endpoints (sync handlers per project convention)
# ----------------------------------------------------------------------


@router.get("/modules", response_model=list[ModuleSummary])
def list_modules(
    category: str | None = Query(
        default=None,
        description="Filter by inferred category (case-insensitive exact match)",
    ),
    search: str | None = Query(
        default=None,
        description="Case-insensitive substring filter against name or description",
    ),
) -> list[dict[str, Any]]:
    """List analytics module summaries, optionally filtered.

    Filters compose: when both ``category`` and ``search`` are supplied, only
    modules matching both are returned. An empty result is a valid response
    (no matching modules).
    """
    service = PlatformCatalogService()
    modules = service.list_modules()

    if category:
        wanted = category.strip().lower()
        modules = [m for m in modules if str(m["category"]).lower() == wanted]

    if search:
        needle = search.strip().lower()
        if needle:
            modules = [
                m
                for m in modules
                if needle in str(m["name"]).lower()
                or needle in str(m.get("description", "")).lower()
            ]

    return modules


@router.get("/modules/{module_name}", response_model=ModuleDetail)
def get_module_detail(module_name: str) -> dict[str, Any]:
    """Return detailed metadata for a single analytics module.

    Raises 404 when the module file does not exist (or is the excluded
    ``__init__``), so callers can distinguish "not found" from an empty detail.
    """
    detail = PlatformCatalogService().get_module_detail(module_name)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"platform module '{module_name}' not found",
        )
    return detail


@router.get("/categories", response_model=list[CategoryCount])
def list_categories() -> list[dict[str, Any]]:
    """Return per-category module counts, sorted by count desc then name."""
    return PlatformCatalogService().list_categories()
