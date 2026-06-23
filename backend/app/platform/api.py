from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.platform.analytics import PerformanceAnalytics
from app.platform.backtest_run_service import BacktestRunService
from app.platform.backtest_service import PlatformBacktestService
from app.platform.bus import EventBus
from app.platform.data_catalog import DataCatalog
from app.platform.events import BarEvent
from app.platform.factor_research_service import FactorResearchService
from app.platform.montecarlo import MonteCarloAnalyzer
from app.platform.optimizer_service import OptimizerService
from app.platform.registry import get_default_registry
from app.platform.replay import EventReplayer
from app.platform.runner import PlatformRunner
from app.platform.store import EventStore
from app.platform.tca import ConstReferencePriceProvider, TcaAnalyzer
from app.platform.tearsheet import TearsheetBuilder, TearsheetExporter
from app.platform.transaction_service import TransactionService

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


@router.post("/tearsheet", dependencies=[Depends(require_api_key())])
def build_tearsheet(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate a backtest run into a full tearsheet (pyfolio / QuantStats style).

    Accepts the same ``{strategy, params, symbols, bars, initial_cash?}`` shape as
    ``POST /backtest`` plus an optional ``format`` (``json`` default or ``csv``).
    JSON returns the full tearsheet dict; CSV returns ``{"format": "csv", "csv": ...}``.
    """
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
    try:
        initial_cash = Decimal(str(payload.get("initial_cash", 100000)))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid initial_cash",
        ) from exc
    fmt = str(payload.get("format", "json")).lower()
    result = PlatformBacktestService().run(
        strategy_name=payload["strategy"],
        params=payload["params"],
        symbols=payload["symbols"],
        bars=payload["bars"],
        initial_cash=initial_cash,
    )
    tearsheet = TearsheetBuilder().build(result)
    if fmt == "csv":
        return {"format": "csv", "csv": TearsheetExporter.to_csv(tearsheet)}
    return tearsheet


@router.post("/backtest/runs", dependencies=[Depends(require_api_key())])
def create_backtest_run(payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    required = {"name", "strategy", "params", "symbols", "bars"}
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
    try:
        initial_cash = Decimal(str(payload.get("initial_cash", 100000)))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid initial_cash",
        ) from exc
    row = BacktestRunService(db).create(
        name=payload["name"],
        strategy_name=payload["strategy"],
        params=payload["params"],
        symbols=payload["symbols"],
        bars=payload["bars"],
        initial_cash=initial_cash,
    )
    return {
        "id": row.id,
        "name": row.name,
        "strategy": row.strategy,
        "final_nav": row.final_nav,
        "sharpe": row.sharpe,
    }


@router.get("/backtest/runs", dependencies=[Depends(require_api_key())])
def list_backtest_runs(limit: int = 50, db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"runs": BacktestRunService(db).list_runs(limit=limit)}


@router.get("/backtest/runs/compare", dependencies=[Depends(require_api_key())])
def compare_backtest_runs(ids: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        run_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be comma-separated integers",
        ) from exc
    if not run_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids required",
        )
    return {"comparison": BacktestRunService(db).compare(run_ids)}


@router.get("/backtest/runs/{run_id}", dependencies=[Depends(require_api_key())])
def get_backtest_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = BacktestRunService(db).get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run {run_id} not found",
        )
    return run


@router.post("/optimize", dependencies=[Depends(require_api_key())])
def optimize_strategy(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"strategy", "param_grid", "symbols", "bars"}
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
    try:
        initial_cash = Decimal(str(payload.get("initial_cash", 100000)))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid initial_cash",
        ) from exc
    metric = str(payload.get("metric", "sharpe"))
    top_k = int(payload.get("top_k", 10))
    mode = str(payload.get("mode", "grid"))
    svc = OptimizerService()
    if mode == "walk-forward":
        return svc.walk_forward(
            strategy_name=payload["strategy"],
            param_grid=payload["param_grid"],
            symbols=payload["symbols"],
            bars=payload["bars"],
            split_fraction=float(payload.get("split_fraction", 0.5)),
            top_k=top_k,
            metric=metric,
            initial_cash=initial_cash,
        )
    return svc.grid_search(
        strategy_name=payload["strategy"],
        param_grid=payload["param_grid"],
        symbols=payload["symbols"],
        bars=payload["bars"],
        metric=metric,
        top_k=top_k,
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
    result = PerformanceAnalytics(periods_per_year=periods).analyze(equity)
    bench_raw = payload.get("benchmark_equity")
    if isinstance(bench_raw, list) and len(bench_raw) >= 2:
        bench = [float(pt["nav"]) for pt in bench_raw]
        from app.platform.benchmark import BenchmarkAnalytics

        result["benchmark"] = BenchmarkAnalytics(periods_per_year=periods).relative(equity, bench)
    return result


@router.post("/montecarlo", dependencies=[Depends(require_api_key())])
def run_montecarlo(payload: dict[str, Any]) -> dict[str, Any]:
    if "trade_pnls" not in payload or not isinstance(payload["trade_pnls"], list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="missing trade_pnls list",
        )
    pnls = [float(x) for x in payload["trade_pnls"]]
    seed = int(payload.get("seed", 42))
    num_simulations = int(payload.get("num_simulations", 1000))
    horizon = payload.get("horizon")
    horizon_i = int(horizon) if horizon is not None else None
    rt = payload.get("ruin_threshold")
    ruin_threshold = float(rt) if rt is not None else None
    return MonteCarloAnalyzer(seed=seed).analyze(
        pnls,
        num_simulations=num_simulations,
        horizon=horizon_i,
        ruin_threshold=ruin_threshold,
    )


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


@router.get("/bars", dependencies=[Depends(require_api_key())])
def list_platform_bars(
    symbol: str,
    resolution_minutes: int = 1,
    limit: int = 500,
) -> dict[str, Any]:
    """Load historical BarEvents for a symbol with optional time-bucketed resampling."""
    if not symbol:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol required",
        )
    if limit < 1 or limit > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be in [1, 10000]",
        )
    if resolution_minutes < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="resolution_minutes must be >= 1",
        )
    bars = DataCatalog().load_bars(symbol=symbol, limit=limit, resolution_minutes=resolution_minutes)
    return {
        "symbol": symbol,
        "resolution_minutes": resolution_minutes,
        "count": len(bars),
        "bars": [
            {
                "timestamp": b.timestamp.isoformat(),
                "symbol": b.symbol,
                "open": str(b.open),
                "high": str(b.high),
                "low": str(b.low),
                "close": str(b.close),
                "volume": b.volume,
            }
            for b in bars
        ],
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


@router.get("/transactions", dependencies=[Depends(require_api_key())])
def list_transactions(
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Query the per-fill transaction ledger (pyfolio ``transactions``)."""
    if limit < 1 or limit > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be in [1, 10000]",
        )
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    rows = TransactionService().list(symbol=symbol, since=since_dt, until=until_dt, limit=limit)
    return {"transactions": rows, "count": len(rows)}


@router.get("/factors/snapshots", dependencies=[Depends(require_api_key())])
def list_factor_snapshots(
    factor_name: str | None = None,
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Query the factor research warehouse (P196)."""
    if limit < 1 or limit > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be in [1, 10000]",
        )
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    rows = FactorResearchService().list_snapshots(
        factor_name=factor_name,
        symbol=symbol,
        since=since_dt,
        until=until_dt,
        limit=limit,
    )
    return {"snapshots": rows, "count": len(rows)}


@router.get("/factors/ic", dependencies=[Depends(require_api_key())])
def compute_factor_ic(
    factor_name: str,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Compute + persist the IC time series for a factor (P196)."""
    if not factor_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="factor_name is required",
        )
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    return FactorResearchService().compute_ic_series(
        factor_name=factor_name, since=since_dt, until=until_dt
    )


@router.post("/factors/snapshots", dependencies=[Depends(require_api_key())])
def record_factor_snapshots(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a batch of factor snapshots (P196).

    Body: ``{"rows": [{factor_name, symbol, as_of, factor_value,
    forward_return?, horizon_bars?, rank?, context?}, ...]}``.
    """
    rows_raw = payload.get("rows")
    if not isinstance(rows_raw, list) or not rows_raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="rows must be a non-empty list",
        )
    from app.platform.factor_research_service import FactorSnapshotData

    parsed: list[FactorSnapshotData] = []
    for i, item in enumerate(rows_raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"rows[{i}] must be an object")
        for key in ("factor_name", "symbol", "as_of", "factor_value"):
            if key not in item:
                raise HTTPException(status_code=422, detail=f"rows[{i}] missing {key}")
        try:
            parsed.append(
                FactorSnapshotData(
                    factor_name=item["factor_name"],
                    symbol=item["symbol"],
                    as_of=datetime.fromisoformat(item["as_of"]),
                    factor_value=float(item["factor_value"]),
                    forward_return=float(item["forward_return"]) if item.get("forward_return") is not None else None,
                    horizon_bars=int(item.get("horizon_bars", 1)),
                    rank=item.get("rank"),
                    context=item.get("context"),
                )
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"rows[{i}] invalid: {exc}") from exc
    count = FactorResearchService().record_many(parsed)
    return {"recorded": count}


@router.get("/tca", dependencies=[Depends(require_api_key())])
def transaction_cost_analysis(
    symbol: str | None = None,
    since: str | None = None,
    until: str | None = None,
    reference_price: float | None = None,
    bucket: str = "day",
) -> dict[str, Any]:
    """Realized execution-cost (TCA) attribution over the transaction ledger (P199).

    If ``reference_price`` is supplied, every fill is benchmarked against that
    constant (useful for a single-symbol analysis); otherwise slippage against
    an unknown reference is treated as 0 (commission-only).
    """
    if bucket not in ("day", "hour", "none"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bucket must be one of day|hour|none",
        )
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    provider: ConstReferencePriceProvider | None = None
    if reference_price is not None:
        prices = {symbol or "ALL": Decimal(str(reference_price))} if symbol else {}
        provider = ConstReferencePriceProvider(prices)
    analyzer = TcaAnalyzer(reference_provider=provider, bucket=bucket)
    attr = analyzer.analyze(symbol=symbol, since=since_dt, until=until_dt)
    return TcaAnalyzer.to_dict(attr)
