from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.auth import require_api_key
from app.platform.backtest_service import PlatformBacktestService
from app.platform.registry import get_default_registry
from app.platform.runner import PlatformRunner

router = APIRouter()


def _runner_snapshot(runner: PlatformRunner) -> dict[str, Any]:
    """Derive a read-only diagnostics snapshot dict from a PlatformRunner.

    Exposes mode, tracked symbols, non-zero positions, and any open (non-filled)
    orders from the attached PaperBroker (paper/backtest modes only).
    """
    positions = [
        {"symbol": sym, "quantity": int(pos.get("quantity", 0))}
        for sym, pos in runner._positions.items()
        if int(pos.get("quantity", 0)) != 0
    ]
    open_orders: list[dict[str, Any]] = []
    broker = getattr(runner, "_broker", None)
    if broker is not None:
        for state in list(broker._orders.values()):
            if state.status in ("SUBMITTED", "PARTIAL_FILLED"):
                intent = state.intent
                open_orders.append(
                    {
                        "broker_order_id": state.order_id,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "quantity": intent.quantity,
                        "filled_quantity": state.filled_quantity,
                        "order_type": intent.order_type,
                        "limit_price": float(intent.limit_price) if intent.limit_price is not None else None,
                        "status": state.status,
                    }
                )
    return {
        "mode": runner.mode,
        "symbols": list(runner.symbols),
        "positions": positions,
        "open_orders": open_orders,
    }


@router.get("/strategies")
def list_strategies() -> list[dict[str, Any]]:
    registry = get_default_registry()
    return [
        {"name": m.name, "version": m.version, "parameter_schema": m.parameter_schema}
        for m in registry.list()
    ]


@router.get("/snapshot", dependencies=[Depends(require_api_key())])
def platform_snapshot(request: Request) -> dict[str, Any]:
    runner: PlatformRunner | None = getattr(request.app.state, "platform_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="platform runner not enabled",
        )
    return _runner_snapshot(runner)


@router.post("/backtest", dependencies=[Depends(require_api_key())])
def run_platform_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"strategy", "params", "symbols", "bars"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"missing fields: {missing}",
        )
    registry = get_default_registry()
    if payload["strategy"] not in {m.name for m in registry.list()}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy '{payload['strategy']}' not found",
        )
    if not payload["symbols"] or not payload["bars"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbols and bars must be non-empty",
        )
    try:
        initial_cash = Decimal(str(payload.get("initial_cash", 100000)))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid initial_cash",
        ) from exc
    service = PlatformBacktestService()
    return service.run(
        strategy_name=payload["strategy"],
        params=payload["params"],
        symbols=payload["symbols"],
        bars=payload["bars"],
        initial_cash=initial_cash,
    )
