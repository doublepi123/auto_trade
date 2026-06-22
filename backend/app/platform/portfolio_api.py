from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import require_api_key
from app.database import get_db
from app.platform.attribution_service import AttributionService
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_service import PortfolioService

router = APIRouter(tags=["portfolio"], dependencies=[Depends(require_api_key())])


def _parse_config(payload: dict[str, Any]) -> PortfolioConfig:
    required = {"name", "symbols", "allocations"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"missing fields: {missing}")
    try:
        return PortfolioConfig(
            name=payload["name"],
            symbols=payload["symbols"],
            allocations={k: Decimal(str(v)) for k, v in payload["allocations"].items()},
            per_symbol_risk_budget={k: Decimal(str(v)) for k, v in payload.get("per_symbol_risk_budget", {}).items()},
            rebalance_threshold_pct=Decimal(str(payload.get("rebalance_threshold_pct", 5))),
            max_gross_exposure=Decimal(str(payload.get("max_gross_exposure", 1.0))),
            max_net_exposure=Decimal(str(payload.get("max_net_exposure", 1.0))),
            enabled=payload.get("enabled", True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/config")
def list_portfolio_configs(db=Depends(get_db)) -> list[dict[str, Any]]:
    svc = PortfolioService(db)
    return [
        {
            "name": c.name,
            "symbols": c.symbols,
            "allocations": {k: float(v) for k, v in c.allocations.items()},
            "per_symbol_risk_budget": {k: float(v) for k, v in c.per_symbol_risk_budget.items()},
            "rebalance_threshold_pct": float(c.rebalance_threshold_pct),
            "max_gross_exposure": float(c.max_gross_exposure),
            "max_net_exposure": float(c.max_net_exposure),
            "enabled": c.enabled,
        }
        for c in svc.list_configs()
    ]


@router.put("/config/{name}")
def save_portfolio_config(name: str, payload: dict[str, Any], db=Depends(get_db)) -> dict[str, Any]:
    if payload.get("name") != name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name mismatch")
    config = _parse_config(payload)
    svc = PortfolioService(db)
    saved = svc.save_config(config)
    return {"name": saved.name, "status": "saved"}


@router.get("/attribution")
def portfolio_attribution(name: str, db=Depends(get_db)) -> dict[str, Any]:
    svc = PortfolioService(db)
    config = svc.get_config(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"portfolio '{name}' not found",
        )
    attribution = AttributionService().attribute(config)
    return attribution
