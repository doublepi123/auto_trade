from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.auth import require_api_key
from app.platform.analytics import PerformanceAnalytics
from app.platform.backtest_service import PlatformBacktestService
from app.platform.bus import EventBus
from app.platform.events import BarEvent
from app.platform.registry import get_default_registry
from app.platform.replay import EventReplayer
from app.platform.runner import PlatformRunner
from app.platform.store import EventStore

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


@router.post("/analyze", dependencies=[Depends(require_api_key())])
def analyze_equity(payload: dict[str, Any]) -> dict[str, Any]:
    if "equity_curve" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing equity_curve")
    equity_raw = payload["equity_curve"]
    if not isinstance(equity_raw, list) or not equity_raw:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="equity_curve must be a non-empty list")
    equity = [float(pt["nav"]) for pt in equity_raw]
    periods = int(payload.get("periods_per_year", 252))
    return PerformanceAnalytics(periods_per_year=periods).analyze(equity)


@router.get("/events", dependencies=[Depends(require_api_key())])
def list_platform_events(
    symbol: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Query persisted platform events with optional symbol/since filtering."""
    if limit < 1 or limit > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be in [1, 10000]",
        )
    since_dt = datetime.fromisoformat(since) if since else None
    events = EventStore().load(since=since_dt, symbol=symbol, limit=limit)
    return {
        "events": [e.to_dict() for e in events],
        "count": len(events),
    }


@router.post("/replay", dependencies=[Depends(require_api_key())])
def replay_platform_events(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically replay persisted events through a fresh paper runner.

    Only BarEvents are fed to the runner (the runner's PaperBroker re-derives
    fills from bars). Pre-existing FillEvents in the window are reported via
    ``fills_in_window`` but not applied, avoiding double-counting.
    """
    required = {"strategy", "params", "symbols"}
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
    strategy_cls = registry.get(payload["strategy"])
    strategy = strategy_cls(params=payload["params"])  # type: ignore[call-arg]
    bus = EventBus()
    runner = PlatformRunner(symbols=payload["symbols"], strategy=strategy, mode="paper", bus=bus)
    symbol = payload.get("symbol")
    since = payload.get("since")
    since_dt = datetime.fromisoformat(since) if since else None
    events = EventReplayer(EventStore()).replay(
        since=since_dt,
        symbol=symbol,
        limit=int(payload.get("limit", 10000)),
    )
    bar_count = 0
    fill_count = 0
    for event in events:
        if isinstance(event, BarEvent):
            runner.on_bar(event)
            bar_count += 1
        elif event.event_type == "fill":
            fill_count += 1
    positions = [
        {"symbol": sym, "quantity": int(pos.get("quantity", 0))}
        for sym, pos in runner._positions.items()
        if int(pos.get("quantity", 0)) != 0
    ]
    return {
        "events_replayed": len(events),
        "bars_replayed": bar_count,
        "fills_in_window": fill_count,
        "reconstructed_positions": positions,
    }
