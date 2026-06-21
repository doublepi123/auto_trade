from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.platform.registry import get_default_registry

router = APIRouter()


@router.get("/strategies")
def list_strategies() -> list[dict[str, Any]]:
    registry = get_default_registry()
    return [
        {"name": m.name, "version": m.version, "parameter_schema": m.parameter_schema}
        for m in registry.list()
    ]
