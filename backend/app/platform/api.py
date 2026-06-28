from __future__ import annotations

import math
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
from app.platform.change_point import detect_change_points
from app.platform.concept_drift import concept_drift_report
from app.platform.cycle_detection import detect_cycles
from app.platform.data_catalog import DataCatalog
from app.platform.events import BarEvent
from app.platform.factor_ic import factor_ic_report
from app.platform.factor_momentum import factor_momentum_report
from app.platform.factor_research_service import FactorResearchService
from app.platform.feature_extraction import feature_extraction_report
from app.platform.feature_orthogonalization import orthogonalization_report
from app.platform.fractional_differencing import (
    fractional_adf_stat,
    fractional_difference,
    fractional_difference_ffd,
    fractional_weights,
)
from app.platform.montecarlo import MonteCarloAnalyzer
from app.platform.multitimeframe_coherence import multitimeframe_coherence_report
from app.platform.optimizer_service import OptimizerService
from app.platform.registry import get_default_registry
from app.platform.replay import EventReplayer
from app.platform.rolling_features import rolling_feature_report
from app.platform.runner import PlatformRunner
from app.platform.spectral_analysis import spectral_report
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


def _first_valid_index(values: list[float | None]) -> int | None:
    for idx, value in enumerate(values):
        if value is not None:
            return idx
    return None


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _fractional_series(payload: dict[str, Any]) -> list[float]:
    # Kept as a thin alias for backward readability at the fractional endpoint;
    # the validation logic lives in :func:`_numeric_series`.
    return _numeric_series(payload)


@router.post("/fractional-differencing", dependencies=[Depends(require_api_key())])
def run_fractional_differencing(payload: dict[str, Any]) -> dict[str, Any]:
    """Fractionally difference a numeric series for feature engineering."""
    mode = str(payload.get("mode", "ffd")).lower()
    if mode not in {"ffd", "expanding"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode must be 'ffd' or 'expanding'",
        )
    try:
        series = _fractional_series(payload)
        d = _finite_number(payload.get("d", 0.4), "d")
        threshold = _finite_number(payload.get("threshold", 1e-2), "threshold")
        weights = fractional_weights(d, threshold)
        output = fractional_difference_ffd(series, d, threshold) if mode == "ffd" else fractional_difference(series, d, threshold)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    first_valid = _first_valid_index(output)
    present = [value for value in output if value is not None]
    return {
        "mode": mode,
        "d": d,
        "threshold": threshold,
        "n_weights": len(weights),
        "n_output": len(present),
        "adf_stat": fractional_adf_stat(present),
        "first_valid_index": first_valid,
        "weights": weights,
        "output": output,
    }


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


# ---------------------------------------------------------------------------
# P211 / P212 — risk-metrics + portfolio-optimize endpoints
# ---------------------------------------------------------------------------


def _to_returns(payload: Any) -> list[float]:
    """Parse a list of return floats from a payload (either a list of numbers or of dicts)."""
    if not isinstance(payload, list) or not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="returns must be a non-empty list",
        )
    if isinstance(payload[0], dict):
        return [float(pt.get("return", pt.get("r", 0.0))) for pt in payload]
    return [float(x) for x in payload]


def _to_equity(payload: Any) -> list[float]:
    if not isinstance(payload, list) or not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="equity_curve must be a non-empty list",
        )
    if isinstance(payload[0], dict):
        return [float(pt.get("nav", pt.get("equity", 0.0))) for pt in payload]
    return [float(x) for x in payload]


@router.post("/risk-metrics", dependencies=[Depends(require_api_key())])
def compute_risk_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """P211: VaR/CVaR + drawdown + pain + fat-tail diagnostics on a return series.

    Accepts ``{"returns": [...]}`` (or ``{"equity_curve": [...]}`` — internally
    converted to returns). Returns a single JSON object covering all metrics.
    """
    if "returns" in payload:
        returns = _to_returns(payload["returns"])
    elif "equity_curve" in payload:
        equity = _to_equity(payload["equity_curve"])
        if len(equity) < 2:
            raise HTTPException(status_code=422, detail="equity_curve too short")
        returns = [(equity[i] / equity[i - 1]) - 1.0 for i in range(1, len(equity))]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="missing 'returns' or 'equity_curve'",
        )

    confidence_levels_raw = payload.get("confidence_levels", [0.90, 0.95, 0.99])
    if not isinstance(confidence_levels_raw, list) or not confidence_levels_raw:
        raise HTTPException(status_code=422, detail="confidence_levels must be a non-empty list")
    confidence_levels = [float(c) for c in confidence_levels_raw]
    periods_per_year = int(payload.get("periods_per_year", 252))

    # VaR / CVaR
    from app.platform.risk_metrics import risk_metrics as _var_metrics

    var_report = _var_metrics(returns, confidence_levels=confidence_levels)

    # Drawdown analysis (needs equity, derive if absent)
    from app.platform.drawdown_analysis import drawdown_summary
    from app.platform.pain_metrics import pain_metrics_report
    from app.platform.fat_tail import fat_tail_report

    if "equity_curve" in payload:
        equity = _to_equity(payload["equity_curve"])
    else:
        # build equity from returns starting at 1.0
        eq = [1.0]
        for r in returns:
            eq.append(eq[-1] * (1.0 + r))
        equity = eq

    drawdown = drawdown_summary(equity)
    pain = pain_metrics_report(equity, periods_per_year=periods_per_year)
    tail = fat_tail_report(returns)

    # Risk ratios (Sharpe/Sortino/Omega, etc.)
    from app.platform.risk_ratios import all_ratios

    benchmark = payload.get("benchmark")
    bench_returns = _to_returns(benchmark) if benchmark is not None else None
    ratios = all_ratios(returns, benchmark=bench_returns, periods_per_year=periods_per_year)

    return {
        "var": var_report,
        "drawdown": drawdown,
        "pain": pain,
        "tail": tail,
        "ratios": ratios,
    }


@router.post("/portfolio-optimize", dependencies=[Depends(require_api_key())])
def optimize_portfolio(payload: dict[str, Any]) -> dict[str, Any]:
    """P212: portfolio optimization endpoint.

    Request body:

    - ``returns_panel`` (required): ``{symbol: [r1, r2, ...]}`` of period returns.
    - ``method`` (optional): ``"min_variance" | "max_sharpe" | "hrp" | "black_litterman"`` (default ``"max_sharpe"``).
    - ``mean_returns`` (optional): ``{symbol: μ}`` override.
    - ``risk_free`` (optional): float, default 0.0.
    - ``market_weights`` (optional): ``{symbol: w}`` for Black-Litterman.
    - ``views`` (optional): list of ``{"assets": {…}, "expected_return": float, "confidence": float}``.

    Returns the chosen method's weights plus the constructed portfolio's
    expected return, volatility, and Sharpe ratio (for sanity-checking).
    """
    if "returns_panel" not in payload or not isinstance(payload["returns_panel"], dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="missing returns_panel dict",
        )
    returns_panel: dict[str, list[float]] = {
        s: [float(x) for x in v] for s, v in payload["returns_panel"].items() if v
    }
    if not returns_panel:
        raise HTTPException(status_code=422, detail="returns_panel is empty")

    method = str(payload.get("method", "max_sharpe")).lower()
    risk_free = float(payload.get("risk_free", 0.0))
    mean_returns_input = payload.get("mean_returns") or {}
    mean_returns = {s: float(v) for s, v in mean_returns_input.items()} if isinstance(mean_returns_input, dict) else {}
    result_turnover: float | None = None
    result_risk_contributions: dict[str, float] | None = None

    # Always compute a shrunk covariance for diagnostics & HRP fallback.
    from app.platform.covariance import ledoit_wolf_shrinkage, portfolio_variance
    from app.platform.mean_variance import (
        efficient_frontier,
        max_sharpe_weights,
        min_variance_weights,
    )
    from app.platform.hrp import hrp_weights

    cov, delta = ledoit_wolf_shrinkage(returns_panel)

    if method == "min_variance":
        weights = min_variance_weights(cov=cov)
    elif method == "hrp":
        weights = hrp_weights(returns=returns_panel)
    elif method == "turnover":
        prev_weights = payload.get("prev_weights")
        if not isinstance(prev_weights, dict):
            raise HTTPException(status_code=422, detail="turnover requires prev_weights dict")
        from app.platform.turnover_optimization import (
            turnover_aware_optimize,
            turnover_penalty,
        )

        prev_w = {s: float(v) for s, v in prev_weights.items()}
        gamma = float(payload.get("gamma", 1.0))
        delta_cap = payload.get("delta_cap")
        delta_cap = float(delta_cap) if delta_cap is not None else None
        lam = float(payload.get("lam", 1.0))
        active = list(returns_panel.keys())
        if not mean_returns:
            mean_returns = {s: sum(returns_panel[s]) / len(returns_panel[s]) for s in active if returns_panel[s]}
        cov_active = {(a, b): cov.get((a, b), 0.0) for a in active for b in active}
        prev_active = {s: prev_w.get(s, 0.0) for s in active}
        weights = turnover_aware_optimize(
            prev_active, cov_active, mean_returns, gamma=gamma, delta_cap=delta_cap,
            risk_free=risk_free, lam=lam,
        )
        result_turnover = turnover_penalty(weights, prev_active)
    elif method == "risk_budgeting":
        from app.platform.risk_budgeting import risk_budgeting

        budgets = payload.get("budgets")
        budgets = {s: float(v) for s, v in budgets.items()} if isinstance(budgets, dict) else None
        active = list(returns_panel.keys())
        cov_active = {(a, b): cov.get((a, b), 0.0) for a in active for b in active}
        try:
            rb_result = risk_budgeting(cov_active, budgets)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        weights = {s: rb_result["weights"].get(s, 0.0) for s in active}
        result_risk_contributions = rb_result["relative_risk_contributions"]
    elif method == "black_litterman":
        if "market_weights" not in payload:
            raise HTTPException(
                status_code=422,
                detail="black_litterman requires market_weights",
            )
        from app.platform.black_litterman import (
            View,
            black_litterman,
            market_implied_returns,
        )
        market_weights = {s: float(v) for s, v in payload["market_weights"].items()}
        views_raw = payload.get("views") or []
        if not isinstance(views_raw, list):
            raise HTTPException(status_code=422, detail="views must be a list")
        views = [
            View(
                assets={s: float(w) for s, w in v["assets"].items()},
                expected_return=float(v["expected_return"]),
                confidence=float(v.get("confidence", 0.5)),
            )
            for v in views_raw
            if isinstance(v, dict) and "assets" in v and "expected_return" in v
        ]
        prior = market_implied_returns(market_weights, cov, risk_aversion=float(payload.get("risk_aversion", 2.5)))
        posterior_returns, _post_cov = black_litterman(prior, cov, views, tau=float(payload.get("tau", 0.05)))
        # Use posterior returns + sample cov to find max-Sharpe tangency.
        # If posterior doesn't include every active symbol, fill in the prior.
        active = list(returns_panel.keys())
        mu = {s: posterior_returns.get(s, prior.get(s, 0.0)) for s in active}
        cov_active = {(a, b): cov.get((a, b), 0.0) for a in active for b in active}
        weights = max_sharpe_weights(mu, cov_active, risk_free=risk_free)
    else:  # default: max_sharpe
        active = list(returns_panel.keys())
        if not mean_returns:
            # default μ = sample mean
            mean_returns = {s: sum(returns_panel[s]) / len(returns_panel[s]) for s in active if returns_panel[s]}
        cov_active = {(a, b): cov.get((a, b), 0.0) for a in active for b in active}
        weights = max_sharpe_weights(mean_returns, cov_active, risk_free=risk_free)

    # Diagnostics: expected return / volatility / Sharpe of the chosen portfolio.
    # For BL we use the posterior expected returns (consistent with the weights,
    # which were derived from posterior μ); for the other methods we use the
    # sample mean that drove the optimization. The volatility always comes from
    # the (shrunk) covariance used throughout.
    port_var = portfolio_variance(cov, weights)
    port_vol = math.sqrt(max(port_var, 0.0)) if port_var is not None else 0.0
    symbols = list(returns_panel.keys())
    if method == "black_litterman":
        diag_mu = {
            s: posterior_returns.get(s, prior.get(s, sum(returns_panel[s]) / len(returns_panel[s])))
            for s in symbols
            if returns_panel[s]
        }
    else:
        diag_mu = {
            s: (mean_returns.get(s, 0.0) if mean_returns else sum(returns_panel[s]) / len(returns_panel[s]))
            for s in symbols
            if returns_panel[s]
        }
    port_return = sum(weights.get(s, 0.0) * diag_mu.get(s, 0.0) for s in symbols)
    sharpe = (port_return - risk_free) / port_vol if port_vol > 0 else 0.0

    return {
        "method": method,
        "weights": weights,
        "expected_return": port_return,
        "volatility": port_vol,
        "sharpe": sharpe,
        "shrinkage_intensity": delta,
        "shrinkage_target": "constant_correlation",
        "turnover": result_turnover,
        "risk_contributions": result_risk_contributions,
    }


# ---------------------------------------------------------------------------
# P214 — combinatorial purged cross-validation splitter
# ---------------------------------------------------------------------------


@router.post("/cpcv", dependencies=[Depends(require_api_key())])
def cpcv_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P214: enumerate combinatorial purged cross-validation IS/OOS splits.

    Body: ``{n_samples: int, n_groups: int, k_test: int, purge?: int, embargo?: int}``.
    422 on invalid config. Returns ``splits`` (list of ``{train_idx, test_idx}``),
    ``summary``, and ``oos_paths`` (disjoint OOS backtest paths).
    """
    from app.platform.cpcv import CpcvConfig, cpcv_oos_paths, cpcv_split, cpcv_summary

    n_samples = payload.get("n_samples")
    if not isinstance(n_samples, int) or n_samples < 0:
        raise HTTPException(status_code=422, detail="n_samples must be a non-negative int")
    n_groups = payload.get("n_groups")
    if not isinstance(n_groups, int):
        raise HTTPException(status_code=422, detail="n_groups must be an int")
    k_test = payload.get("k_test")
    if not isinstance(k_test, int):
        raise HTTPException(status_code=422, detail="k_test must be an int")
    purge = int(payload.get("purge", 0))
    embargo = int(payload.get("embargo", 0))
    config = CpcvConfig(n_groups=n_groups, k_test=k_test, purge=purge, embargo=embargo)
    try:
        splits = cpcv_split(n_samples, config)
        summary = cpcv_summary(n_samples, config)
        paths = cpcv_oos_paths(n_samples, config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "splits": [
            {"train_idx": list(s.train_idx), "test_idx": list(s.test_idx)} for s in splits
        ],
        "summary": summary,
        "oos_paths": paths,
    }


# ---------------------------------------------------------------------------
# P215 — returns-based style analysis endpoint
# ---------------------------------------------------------------------------


@router.post("/style-analysis", dependencies=[Depends(require_api_key())])
def style_analysis_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P215: Sharpe (1992) returns-based style analysis.

    Body: ``{"returns": [...], "factor_returns": {style: [...]},
    "constraint": "sum_le_one"|"sum_eq_one"|"none",
    "periods_per_year": 252}``. 422 on empty/short/invalid inputs. Returns
    weights, R², RSS, tracking error, annualized tracking error, sum of
    weights, constraint, and iterations.
    """
    from app.platform.style_analysis import style_analysis

    if "returns" not in payload or not isinstance(payload["returns"], list) or not payload["returns"]:
        raise HTTPException(status_code=422, detail="returns must be a non-empty list")
    fr = payload.get("factor_returns")
    if not isinstance(fr, dict) or not fr:
        raise HTTPException(status_code=422, detail="factor_returns must be a non-empty dict")
    factor_returns: dict[str, list[float]] = {}
    for name, series in fr.items():
        if not isinstance(series, list) or len(series) < 2:
            raise HTTPException(status_code=422, detail=f"factor '{name}' series too short")
        factor_returns[name] = [float(x) for x in series]
    returns = [float(x) for x in payload["returns"]]
    constraint = str(payload.get("constraint", "sum_le_one"))
    periods_per_year = int(payload.get("periods_per_year", 252))
    try:
        res = style_analysis(
            returns,
            factor_returns,
            constraint=constraint,
            periods_per_year=periods_per_year,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P213 — market regime detection endpoint
# ---------------------------------------------------------------------------


@router.post("/regime", dependencies=[Depends(require_api_key())])
def regime_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P213: classify the current market regime (BULL/BEAR/SIDEWAYS).

    Body: ``{"closes": [...], "highs"? : [...], "lows"?: [...], "short_period"?: int,
    "long_period"?: int, "adx_period"?: int, "periods_per_year"?: int}``. 422 if
    closes missing/empty/too-short.
    """
    from app.platform.regime import RegimeConfig, regime_report

    closes = payload.get("closes")
    if not isinstance(closes, list) or not closes:
        raise HTTPException(status_code=422, detail="closes must be a non-empty list")
    closes = [float(x) for x in closes]
    highs = payload.get("highs")
    lows = payload.get("lows")
    if highs is not None:
        if not isinstance(highs, list) or len(highs) != len(closes):
            raise HTTPException(status_code=422, detail="highs must match closes length")
        highs = [float(x) for x in highs]
    if lows is not None:
        if not isinstance(lows, list) or len(lows) != len(closes):
            raise HTTPException(status_code=422, detail="lows must match closes length")
        lows = [float(x) for x in lows]
    cfg = RegimeConfig(
        short_period=int(payload.get("short_period", 20)),
        long_period=int(payload.get("long_period", 50)),
        adx_period=int(payload.get("adx_period", 14)),
        periods_per_year=int(payload.get("periods_per_year", 252)),
    )
    if len(closes) < cfg.min_bars:
        raise HTTPException(status_code=422, detail=f"closes too short (need >= {cfg.min_bars})")
    try:
        return regime_report(closes, highs, lows, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# P218 — trade MFE/MAE excursion endpoint
# ---------------------------------------------------------------------------


@router.post("/trade-excursion", dependencies=[Depends(require_api_key())])
def trade_excursion_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P218: per-trade MFE/MAE + holding-period summary.

    Body: ``{"trades": [{entry_time, exit_time?, side, entry_price, exit_price?,
    symbol?}], "bars": [{timestamp, open, high, low, close, volume, symbol?}]}``.
    422 if trades/bars missing or not lists.
    """
    from app.platform.trade_excursion import analyze_trades, TradeExcursionInput

    trades_raw = payload.get("trades")
    bars_raw = payload.get("bars")
    if not isinstance(trades_raw, list):
        raise HTTPException(status_code=422, detail="trades must be a list")
    if not isinstance(bars_raw, list):
        raise HTTPException(status_code=422, detail="bars must be a list")
    # Ensure each bar dict has a source (default MARKET) for BarEvent.from_dict.
    bars: list[dict[str, Any]] = []
    for b in bars_raw:
        if not isinstance(b, dict):
            raise HTTPException(status_code=422, detail="each bar must be a dict")
        if "source" not in b:
            b = {**b, "source": "market"}
        bars.append(b)
    trades: list[TradeExcursionInput] = []
    for t in trades_raw:
        if not isinstance(t, dict) or "entry_time" not in t or "side" not in t or "entry_price" not in t:
            raise HTTPException(status_code=422, detail="each trade needs entry_time, side, entry_price")
        from datetime import date, datetime as _dt

        def _to_dt(x: Any) -> _dt:
            if isinstance(x, _dt):
                return x
            if isinstance(x, date):
                return _dt(x.year, x.month, x.day)
            return _dt.fromisoformat(x)
        trades.append(TradeExcursionInput(
            entry_time=_to_dt(t["entry_time"]),
            exit_time=_to_dt(t["exit_time"]) if t.get("exit_time") else None,
            side=str(t["side"]),
            entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]) if t.get("exit_price") is not None else None,
            symbol=t.get("symbol"),
            quantity=t.get("quantity"),
            trade_id=t.get("trade_id"),
        ))
    try:
        per_trade, summary = analyze_trades(trades, bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "trades": [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "mfe": t.mfe,
                "mae": t.mae,
                "mfe_pct": t.mfe_pct,
                "mae_pct": t.mae_pct,
                "realized_pnl_pct": t.realized_pnl_pct,
                "holding_bars": t.holding_bars,
            }
            for t in per_trade
        ],
        "summary": {
            "num_trades": summary.num_trades,
            "num_closed": summary.num_closed,
            "num_open": summary.num_open,
            "avg_holding_bars": summary.avg_holding_bars,
            "median_holding_bars": summary.median_holding_bars,
            "avg_mfe_pct": summary.avg_mfe_pct,
            "avg_mae_pct": summary.avg_mae_pct,
            "median_mfe_pct": summary.median_mfe_pct,
            "median_mae_pct": summary.median_mae_pct,
            "mfe_mae_ratio": summary.mfe_mae_ratio,
            "expectancy": summary.expectancy,
        },
    }


# ---------------------------------------------------------------------------
# P219 — implementation shortfall endpoint
# ---------------------------------------------------------------------------


@router.post("/shortfall", dependencies=[Depends(require_api_key())])
def shortfall_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P219: Perold implementation shortfall decomposition.

    Body: ``{"order": {symbol, side, ordered_quantity, limit_price?,
    arrival_price?, benchmark?, benchmark_price?, fees?}, "fills": [{quantity,
    price, commission?}], "arrival_price"?: ..., "benchmark"?: ...}``.
    422 on missing order / unknown side / missing arrival / close benchmark
    without price.
    """
    from app.platform.shortfall import ShortfallFill, ShortfallOrder, implementation_shortfall

    order_raw = payload.get("order")
    if not isinstance(order_raw, dict):
        raise HTTPException(status_code=422, detail="order is required")
    try:
        order = ShortfallOrder(
            symbol=str(order_raw["symbol"]),
            side=str(order_raw["side"]),
            ordered_quantity=Decimal(str(order_raw["ordered_quantity"])),
            limit_price=Decimal(str(order_raw["limit_price"])) if order_raw.get("limit_price") is not None else None,
            arrival_price=Decimal(str(order_raw["arrival_price"])) if order_raw.get("arrival_price") is not None else None,
            benchmark=str(order_raw.get("benchmark", "arrival")),
            benchmark_price=Decimal(str(order_raw["benchmark_price"])) if order_raw.get("benchmark_price") is not None else None,
            fees=Decimal(str(order_raw.get("fees", 0))),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid order: {exc}")
    fills_raw = payload.get("fills") or []
    if not isinstance(fills_raw, list):
        raise HTTPException(status_code=422, detail="fills must be a list")
    fills = [
        ShortfallFill(
            quantity=Decimal(str(f["quantity"])),
            price=Decimal(str(f["price"])),
            commission=Decimal(str(f.get("commission", 0))),
        )
        for f in fills_raw if isinstance(f, dict)
    ]
    arrival = Decimal(str(payload["arrival_price"])) if payload.get("arrival_price") is not None else None
    bench = payload.get("benchmark")
    bench_price = Decimal(str(payload["benchmark_price"])) if payload.get("benchmark_price") is not None else None
    try:
        b = implementation_shortfall(order, fills, arrival_price=arrival, benchmark=bench,
                                     benchmark_price=bench_price)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return b.to_dict()


# ---------------------------------------------------------------------------
# P220 — returns calendar endpoint
# ---------------------------------------------------------------------------


@router.post("/returns-calendar", dependencies=[Depends(require_api_key())])
def returns_calendar_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P220: monthly / yearly / weekday / streak returns calendar tables.

    Body: ``{"returns": [...]}`` or ``{"equity_curve": [...]}`` (converted to
    returns) plus optional ``{"dates": [iso-str|{date:...}]}``. 422 when neither
    returns nor equity_curve is provided.
    """
    from app.platform.returns_analysis import returns_calendar_dict

    if "returns" in payload:
        returns = _to_returns(payload["returns"])
    elif "equity_curve" in payload:
        equity = _to_equity(payload["equity_curve"])
        if len(equity) < 2:
            raise HTTPException(status_code=422, detail="equity_curve too short")
        returns = [(equity[i] / equity[i - 1]) - 1.0 for i in range(1, len(equity))]
    else:
        raise HTTPException(status_code=422, detail="missing 'returns' or 'equity_curve'")
    dates = payload.get("dates")
    try:
        return returns_calendar_dict(returns, dates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# P221 — scenario stress report endpoint
# ---------------------------------------------------------------------------


@router.post("/stress-report", dependencies=[Depends(require_api_key())])
def stress_report_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P221: aggregate macro stress scenarios into a single report.

    Body: ``{"positions": {symbol: [qty, price]}, "betas"?: {symbol: beta},
    "base_nav"?: float, "confidence_levels"?: [..], "capital_buffer"?: float}``.
    422 if positions missing/empty.
    """
    from app.platform.stress_report import build_stress_report

    positions_raw = payload.get("positions")
    if not isinstance(positions_raw, dict) or not positions_raw:
        raise HTTPException(status_code=422, detail="positions must be a non-empty dict")
    positions: dict[str, tuple[int, Decimal]] = {}
    for sym, val in positions_raw.items():
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            raise HTTPException(status_code=422, detail=f"position {sym} must be [qty, price]")
        positions[sym] = (int(val[0]), Decimal(str(val[1])))
    betas_raw = payload.get("betas") or {}
    betas = {s: Decimal(str(v)) for s, v in betas_raw.items()} if isinstance(betas_raw, dict) else None
    base_nav = Decimal(str(payload["base_nav"])) if payload.get("base_nav") is not None else None
    conf = payload.get("confidence_levels")
    confidence_levels = [float(c) for c in conf] if isinstance(conf, list) else None
    capital_buffer = Decimal(str(payload["capital_buffer"])) if payload.get("capital_buffer") is not None else None
    try:
        return build_stress_report(
            positions, betas=betas, base_nav=base_nav,
            confidence_levels=confidence_levels, capital_buffer=capital_buffer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# P222 — walk-forward parameter stability endpoint
# ---------------------------------------------------------------------------


@router.post("/stability", dependencies=[Depends(require_api_key())])
def stability_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P222: walk-forward parameter stability diagnostics.

    Body: ``{"wf_results": [{params, in_sample_sharpe, out_of_sample_sharpe}],
    "metric"?: "sharpe", "higher_is_better"?: true, "ratio_cap"?: 4.0,
    "ratio_floor"?: 1e-6}``. 422 if wf_results missing/empty/not-list.
    """
    from app.platform.stability_analysis import analyze_stability

    wf = payload.get("wf_results")
    if not isinstance(wf, list) or not wf:
        raise HTTPException(status_code=422, detail="wf_results must be a non-empty list")
    metric = str(payload.get("metric", "sharpe"))
    higher_is_better = bool(payload.get("higher_is_better", True))
    ratio_cap = float(payload.get("ratio_cap", 4.0))
    ratio_floor = float(payload.get("ratio_floor", 1e-6))
    try:
        return analyze_stability(wf, metric=metric, higher_is_better=higher_is_better,
                                  ratio_cap=ratio_cap, ratio_floor=ratio_floor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# P223 — cointegration & pairs trading diagnostics endpoint
# ---------------------------------------------------------------------------


@router.post("/cointegration", dependencies=[Depends(require_api_key())])
def cointegration_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P223: Engle-Granger cointegration + OU half-life + z-score for two series.

    Body: ``{"y": [...], "x": [...], "zscore_window"?: int}``.
    422 if y/x missing, length mismatch, or <2 points.
    """
    from app.platform.cointegration import cointegration_analysis

    y = payload.get("y")
    x = payload.get("x")
    if not isinstance(y, list) or not isinstance(x, list):
        raise HTTPException(status_code=422, detail="y and x must be lists")
    if len(y) != len(x) or len(y) < 2:
        raise HTTPException(status_code=422, detail="y and x must be equal-length lists with >=2 points")
    zsw = payload.get("zscore_window")
    zsw_int = int(zsw) if zsw is not None else None
    try:
        res = cointegration_analysis([float(v) for v in y], [float(v) for v in x], zscore_window=zsw_int)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P224 — Kelly criterion & bet-sizing endpoint
# ---------------------------------------------------------------------------


@router.post("/kelly", dependencies=[Depends(require_api_key())])
def kelly_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P224: Kelly-optimal bet sizing.

    Body either ``{"win_prob", "win_size", "loss_size"}`` (binary bet) or
    ``{"returns": [...]}`` (continuous Kelly from a return series).
    Optional ``bankroll_units`` adds a risk-of-ruin estimate. 422 on bad inputs.
    """
    from app.platform.kelly import (
        fractional_kelly,
        kelly_binary,
        kelly_from_returns,
        risk_of_ruin,
    )

    if "returns" in payload:
        rs = payload["returns"]
        if not isinstance(rs, list) or len(rs) < 2:
            raise HTTPException(status_code=422, detail="returns must be a list with >=2 points")
        try:
            f = kelly_from_returns([float(x) for x in rs])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"method": "continuous", "full_kelly": f, "half_kelly": 0.5 * f}

    wp = payload.get("win_prob")
    ws = payload.get("win_size")
    ls = payload.get("loss_size")
    if wp is None or ws is None or ls is None:
        raise HTTPException(status_code=422, detail="provide (win_prob, win_size, loss_size) or returns")
    try:
        wp_f, ws_f, ls_f = float(wp), float(ws), float(ls)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="win_prob/win_size/loss_size must be numeric")
    try:
        rep = fractional_kelly(wp_f, ws_f, ls_f)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = rep.to_dict()
    bu = payload.get("bankroll_units")
    if bu is not None:
        try:
            out["risk_of_ruin"] = risk_of_ruin(wp_f, ws_f, ls_f, bankroll_units=float(bu))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    return out


# ---------------------------------------------------------------------------
# P225 — volatility forecasting endpoint
# ---------------------------------------------------------------------------


@router.post("/volatility", dependencies=[Depends(require_api_key())])
def volatility_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P225: EWMA + GARCH(1,1) (+ Parkinson) volatility forecast.

    Body: ``{"returns": [...], "highs"?: [...], "lows"?: [...],
    "lam"?: 0.94, "alpha"?: 0.10, "beta"?: 0.85}``. 422 if returns missing/short.
    """
    from app.platform.volatility_models import volatility_report

    rs = payload.get("returns")
    if not isinstance(rs, list) or len(rs) < 2:
        raise HTTPException(status_code=422, detail="returns must be a list with >=2 points")
    highs = payload.get("highs")
    lows = payload.get("lows")
    if (highs is None) != (lows is None):
        raise HTTPException(status_code=422, detail="highs and lows must be provided together")
    try:
        rep = volatility_report(
            [float(x) for x in rs],
            highs=[float(x) for x in highs] if highs is not None else None,
            lows=[float(x) for x in lows] if lows is not None else None,
            lam=float(payload.get("lam", 0.94)),
            alpha=float(payload.get("alpha", 0.10)),
            beta=float(payload.get("beta", 0.85)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P226 — microstructure VPIN / OFI endpoint
# ---------------------------------------------------------------------------


@router.post("/microstructure", dependencies=[Depends(require_api_key())])
def microstructure_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P226: VPIN + order-flow imbalance + Kyle's lambda from bar data.

    Body: ``{"volumes": [...], "opens": [...], "closes": [...],
    "bucket_size"?: float, "window"?: int}``. 422 on missing/mismatched/empty.
    """
    from app.platform.microstructure import kyle_lambda, order_flow_imbalance, vpin

    vols = payload.get("volumes")
    opens = payload.get("opens")
    closes = payload.get("closes")
    if not isinstance(vols, list) or not isinstance(opens, list) or not isinstance(closes, list):
        raise HTTPException(status_code=422, detail="volumes, opens, closes must be lists")
    n = len(vols)
    if n == 0 or n != len(opens) or n != len(closes):
        raise HTTPException(status_code=422, detail="volumes/opens/closes must be equal-length non-empty")
    bs = payload.get("bucket_size")
    win = int(payload.get("window", 50))
    try:
        v = vpin(
            [float(x) for x in vols],
            [float(x) for x in opens],
            [float(x) for x in closes],
            bucket_size=float(bs) if bs is not None else None,
            window=win,
        )
        ofi = order_flow_imbalance(
            [float(x) for x in vols],
            [float(x) for x in opens],
            [float(x) for x in closes],
        )
        lam = kyle_lambda(
            [float(x) for x in vols],
            [float(x) for x in opens],
            [float(x) for x in closes],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "vpin": v.to_dict(),
        "ofi": ofi,
        "latest_ofi": ofi[-1] if ofi else 0.0,
        "kyle_lambda": lam,
    }


# ---------------------------------------------------------------------------
# P227 — Almgren-Chriss optimal execution endpoint
# ---------------------------------------------------------------------------


@router.post("/execution-cost", dependencies=[Depends(require_api_key())])
def execution_cost_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P227: Almgren-Chriss optimal execution trajectory + cost/risk.

    Body: ``{"total_shares": float, "n_slices": int, "eta"?: 0.1,
    "sigma"?: 0.3, "risk_aversion"?: 0.0}``. 422 on missing/bad inputs.
    """
    from app.platform.execution_cost import almgren_chriss, efficient_frontier

    if "total_shares" not in payload or "n_slices" not in payload:
        raise HTTPException(status_code=422, detail="total_shares and n_slices are required")
    try:
        ts = float(payload["total_shares"])
        ns = int(payload["n_slices"])
        eta = float(payload.get("eta", 0.1))
        sigma = float(payload.get("sigma", 0.3))
        ra = float(payload.get("risk_aversion", 0.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid numeric inputs")
    try:
        res = almgren_chriss(ts, ns, eta=eta, sigma=sigma, risk_aversion=ra)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = res.to_dict()
    if payload.get("frontier"):
        try:
            out["frontier"] = efficient_frontier(ts, ns, eta=eta, sigma=sigma)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    return out


# ---------------------------------------------------------------------------
# P228 — Hawkes self-exciting process endpoint
# ---------------------------------------------------------------------------


@router.post("/hawkes", dependencies=[Depends(require_api_key())])
def hawkes_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P228: Hawkes process branching ratio + log-likelihood from event times.

    Body: ``{"events": [t1, t2, ...], "mu"?: float, "kappa"?: float,
    "beta"?: float}``. Defaults estimated from the event times. 422 on bad input.
    """
    from app.platform.hawkes import fit_hawkes

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=422, detail="events must be a non-empty list")
    mu = payload.get("mu")
    kappa = payload.get("kappa")
    beta = payload.get("beta")
    try:
        fit = fit_hawkes(
            [float(t) for t in events],
            mu=float(mu) if mu is not None else None,
            kappa=float(kappa) if kappa is not None else None,
            beta=float(beta) if beta is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return fit.to_dict()


# ---------------------------------------------------------------------------
# P229 — historical scenario stress endpoint
# ---------------------------------------------------------------------------


@router.post("/historical-stress", dependencies=[Depends(require_api_key())])
def historical_stress_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P229: apply historical episode library to the current book.

    Body: ``{"positions": {symbol: [qty, price]}, "episodes"?: [{name, returns, description?}],
    "capital_buffer"?: float, "confidence"?: 0.95}``. 422 on missing/empty positions.
    """
    from app.platform.historical_scenarios import (
        HistoricalEpisode,
        HistoricalScenarioLibrary,
        historical_stress_report,
    )

    positions = payload.get("positions")
    if not isinstance(positions, dict) or not positions:
        raise HTTPException(status_code=422, detail="positions must be a non-empty dict")
    pos: dict[str, tuple[float, float]] = {}
    for sym, vp in positions.items():
        if not isinstance(vp, (list, tuple)) or len(vp) != 2:
            raise HTTPException(status_code=422, detail="each position must be [qty, price]")
        pos[sym] = (float(vp[0]), float(vp[1]))
    eps_raw = payload.get("episodes")
    if eps_raw:
        lib = HistoricalScenarioLibrary()
        for ep in eps_raw:
            if not isinstance(ep, dict) or "name" not in ep or "returns" not in ep:
                raise HTTPException(status_code=422, detail="each episode needs name and returns")
            lib.add_episode(HistoricalEpisode(
                name=str(ep["name"]),
                returns={k: float(v) for k, v in ep["returns"].items()},
                description=str(ep.get("description", "")),
            ))
    else:
        lib = HistoricalScenarioLibrary.with_defaults()
    try:
        rep = historical_stress_report(
            pos,
            library=lib,
            capital_buffer=float(payload.get("capital_buffer", 0.0)),
            confidence=float(payload.get("confidence", 0.95)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P230 — factor risk decomposition endpoint
# ---------------------------------------------------------------------------


@router.post("/factor-risk", dependencies=[Depends(require_api_key())])
def factor_risk_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P230: cross-sectional factor risk decomposition (Barra-style).

    Body: ``{"weights": {sym: w}, "exposures": {sym: {factor: beta}},
    "factor_cov": {fi: {fj: cov}}, "idio_var": {sym: var}}``. 422 on missing/empty.
    """
    from app.platform.factor_risk import factor_risk_decomposition

    w = payload.get("weights")
    exp = payload.get("exposures")
    fc = payload.get("factor_cov")
    iv = payload.get("idio_var")
    if not isinstance(w, dict) or not w:
        raise HTTPException(status_code=422, detail="weights must be a non-empty dict")
    if not isinstance(fc, dict) or not fc:
        raise HTTPException(status_code=422, detail="factor_cov must be a non-empty dict")
    try:
        res = factor_risk_decomposition(
            weights={k: float(v) for k, v in w.items()},
            exposures={k: {fk: float(fv) for fk, fv in (v or {}).items()}
                       for k, v in (exp or {}).items()},
            factor_cov={fi: {fj: float(x) for fj, x in (row or {}).items()}
                        for fi, row in fc.items()},
            idio_var={k: float(v) for k, v in (iv or {}).items()},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P231 — parameter importance & sensitivity endpoint
# ---------------------------------------------------------------------------


@router.post("/sensitivity", dependencies=[Depends(require_api_key())])
def sensitivity_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P231: fANOVA-style parameter importance from grid/walk-forward records.

    Body: ``{"records": [{"params": {axis: value}, "metric": float}]}``. 422 on
    missing/empty/not-list.
    """
    from app.platform.sensitivity import parameter_importance

    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=422, detail="records must be a non-empty list")
    try:
        rep = parameter_importance(records)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P232 — extreme value theory endpoint
# ---------------------------------------------------------------------------


@router.post("/evt", dependencies=[Depends(require_api_key())])
def evt_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P232: GPD peaks-over-threshold tail VaR/CVaR.

    Body: ``{"losses": [...], "threshold": float, "confidence_levels"?: [0.99, 0.999]}``.
    422 on missing/empty/bad inputs.
    """
    from app.platform.extreme_value import evt_report

    losses = payload.get("losses")
    if not isinstance(losses, list) or not losses:
        raise HTTPException(status_code=422, detail="losses must be a non-empty list")
    if "threshold" not in payload:
        raise HTTPException(status_code=422, detail="threshold is required")
    try:
        thr = float(payload["threshold"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="threshold must be numeric")
    cls_raw = payload.get("confidence_levels", [0.99, 0.999])
    if not isinstance(cls_raw, list) or not cls_raw:
        raise HTTPException(status_code=422, detail="confidence_levels must be a non-empty list")
    cls = [float(c) for c in cls_raw]
    try:
        rep = evt_report([float(l) for l in losses], threshold=thr, confidence_levels=cls)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ===========================================================================
# P233-P242 — risk-research IV + causal/diagnostics endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# P233 — causal discovery endpoint
# ---------------------------------------------------------------------------


@router.post("/causal-analysis", dependencies=[Depends(require_api_key())])
def causal_analysis_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P233: Granger causality + PCMCI-style lag screening for two series.

    Body: ``{"x": [...], "y": [...], "max_lag": int, "z"?: [...],
    "mode"?: "granger"|"partial"|"lead_lag"}`` (default ``"lead_lag"``).
    422 on missing/empty/mismatched/bad inputs.
    """
    from app.platform.causal_analysis import (
        granger_causality,
        lead_lag_summary,
        partial_correlation_lag,
    )

    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, list) or not isinstance(y, list):
        raise HTTPException(status_code=422, detail="x and y must be lists")
    if not x or not y:
        raise HTTPException(status_code=422, detail="x and y must be non-empty")
    if len(x) != len(y):
        raise HTTPException(status_code=422, detail="x and y must be equal-length")
    if "max_lag" not in payload:
        raise HTTPException(status_code=422, detail="max_lag is required")
    try:
        max_lag = int(payload["max_lag"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="max_lag must be an integer")
    z = payload.get("z")
    if z is not None:
        if not isinstance(z, list):
            raise HTTPException(status_code=422, detail="z must be a list")
        if len(z) != len(x):
            raise HTTPException(status_code=422, detail="z must match x/y length")
    mode = str(payload.get("mode", "lead_lag"))
    try:
        if mode == "granger":
            rep = granger_causality([float(v) for v in x], [float(v) for v in y], max_lag)
            return rep.to_dict()
        if mode == "partial":
            out = partial_correlation_lag(
                [float(v) for v in x],
                [float(v) for v in y],
                [float(v) for v in z] if z is not None else None,
                max_lag,
            )
            return {"max_lag": max_lag, "partial_correlations": {str(k): v for k, v in out.items()}}
        rep = lead_lag_summary([float(v) for v in x], [float(v) for v in y], max_lag)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P234 — HMM regime endpoint
# ---------------------------------------------------------------------------


@router.post("/regime-hmm", dependencies=[Depends(require_api_key())])
def regime_hmm_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P234: Gaussian-emission Hidden Markov regime (Baum-Welch + Viterbi).

    Body: ``{"returns": [...], "n_states"?: 2, "n_iter"?: 50, "tol"?: 1e-5}``.
    422 on missing/empty/bad inputs.
    """
    from app.platform.regime_hmm import fit_hmm

    rs = payload.get("returns")
    if not isinstance(rs, list) or not rs:
        raise HTTPException(status_code=422, detail="returns must be a non-empty list")
    try:
        n_states = int(payload.get("n_states", 2))
        n_iter = int(payload.get("n_iter", 50))
        tol = float(payload.get("tol", 1e-5))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="n_states/n_iter/tol must be numeric")
    try:
        rep = fit_hmm(
            [float(v) for v in rs],
            n_states=n_states,
            n_iter=n_iter,
            tol=tol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P235 — copula tail-dependence endpoint
# ---------------------------------------------------------------------------


@router.post("/copula", dependencies=[Depends(require_api_key())])
def copula_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P235: empirical + Gumbel/Clayton copula tail-dependence for two series.

    Body: ``{"x": [...], "y": [...]}`` (>=10 paired samples, non-constant).
    422 on missing/empty/mismatched inputs.
    """
    from app.platform.copula import tail_dependence_coeffs

    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, list) or not isinstance(y, list):
        raise HTTPException(status_code=422, detail="x and y must be lists")
    if not x or not y:
        raise HTTPException(status_code=422, detail="x and y must be non-empty")
    if len(x) != len(y):
        raise HTTPException(status_code=422, detail="x and y must be equal-length")
    try:
        rep = tail_dependence_coeffs([float(v) for v in x], [float(v) for v in y])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P236 — drawdown forecast endpoint
# ---------------------------------------------------------------------------


@router.post("/drawdown-forecast", dependencies=[Depends(require_api_key())])
def drawdown_forecast_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P236: drawdown forecast + recovery-time distribution (Burke/Johansen).

    Body: ``{"series": [...], "horizon_bars": int, "confidence"?: 0.95,
    "input_mode"?: "equity"|"returns"}``. 422 on missing/empty/bad inputs.
    """
    from app.platform.drawdown_forecast import drawdown_forecast_report

    series = payload.get("series")
    if not isinstance(series, list) or not series:
        raise HTTPException(status_code=422, detail="series must be a non-empty list")
    if "horizon_bars" not in payload:
        raise HTTPException(status_code=422, detail="horizon_bars is required")
    try:
        horizon = int(payload["horizon_bars"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="horizon_bars must be an integer")
    input_mode = str(payload.get("input_mode", "equity"))
    if input_mode not in ("equity", "returns"):
        raise HTTPException(status_code=422, detail="input_mode must be 'equity' or 'returns'")
    try:
        conf = float(payload.get("confidence", 0.95))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="confidence must be numeric")
    try:
        rep = drawdown_forecast_report(
            [float(v) for v in series],
            horizon_bars=horizon,
            confidence=conf,
            input_mode=input_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P237 — liquidity metrics endpoint
# ---------------------------------------------------------------------------


@router.post("/liquidity-metrics", dependencies=[Depends(require_api_key())])
def liquidity_metrics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P237: Amihud illiquidity + Roll spread + Pastor-Stambaugh (+ Corwin-Schultz).

    Body: ``{"returns": [...], "volumes"?: [...], "market_returns"?: [...],
    "highs"?: [...], "lows"?: [...]}``. 422 on missing/empty returns.
    """
    from app.platform.liquidity_metrics import liquidity_report

    rs = payload.get("returns")
    if not isinstance(rs, list) or not rs:
        raise HTTPException(status_code=422, detail="returns must be a non-empty list")
    extras: dict[str, Any] = {}
    for key in ("volumes", "market_returns", "highs", "lows"):
        v = payload.get(key)
        if v is not None:
            if not isinstance(v, list):
                raise HTTPException(status_code=422, detail=f"{key} must be a list")
            if len(v) != len(rs):
                raise HTTPException(status_code=422, detail=f"{key} must match returns length")
            extras[key] = [float(x) for x in v]
    try:
        rep = liquidity_report([float(x) for x in rs], **extras)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P238 — momentum / reversal factor endpoint
# ---------------------------------------------------------------------------


@router.post("/momentum-factors", dependencies=[Depends(require_api_key())])
def momentum_factors_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P238: Jegadeesh-Titman momentum / De-Bondt reversal factor library.

    Body: ``{"price_panel": {asset: [...]}}, "mode"?: "momentum"|"reversal",
    "lookback"?: int, "holding"?: int, "n_long"?: int, "n_short"?: int}``.
    422 on missing/empty/bad inputs.
    """
    from app.platform.momentum_factors import momentum_factor, reversal_factor

    pp = payload.get("price_panel")
    if not isinstance(pp, dict) or not pp:
        raise HTTPException(status_code=422, detail="price_panel must be a non-empty dict")
    try:
        lookback = int(payload.get("lookback", 12))
        holding = int(payload.get("holding", 1))
        n_long = int(payload.get("n_long", 3))
        n_short = int(payload.get("n_short", 3))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lookback/holding/n_long/n_short must be integers")
    panel = {k: [float(v) for v in vs] for k, vs in pp.items()}
    mode = str(payload.get("mode", "momentum"))
    try:
        if mode == "reversal":
            rep = reversal_factor(panel, lookback=lookback, holding=holding, n_long=n_long, n_short=n_short)
        else:
            rep = momentum_factor(panel, lookback=lookback, holding=holding, n_long=n_long, n_short=n_short)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P239 — portfolio decomposition endpoint
# ---------------------------------------------------------------------------


@router.post("/portfolio-decomposition", dependencies=[Depends(require_api_key())])
def portfolio_decomposition_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P239: regress portfolio returns onto factor returns (contribution attribution).

    Body: ``{"returns": [...], "factor_returns": {factor: [...]}}``.
    422 on missing/empty/mismatched inputs.
    """
    from app.platform.portfolio_decomposition import returns_to_factors

    rs = payload.get("returns")
    fr = payload.get("factor_returns")
    if not isinstance(rs, list) or not rs:
        raise HTTPException(status_code=422, detail="returns must be a non-empty list")
    if not isinstance(fr, dict) or not fr:
        raise HTTPException(status_code=422, detail="factor_returns must be a non-empty dict")
    factor_returns = {k: [float(v) for v in vs] for k, vs in fr.items()}
    try:
        rep = returns_to_factors([float(v) for v in rs], factor_returns)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P240 — Superior Predictive Ability test endpoint
# ---------------------------------------------------------------------------


@router.post("/spa-test", dependencies=[Depends(require_api_key())])
def spa_test_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P240: Hansen-White Superior Predictive Ability test (deterministic block bootstrap).

    Body: ``{"benchmark_lf": [...], "model_lfs": [[...], ...], "B"?: 100,
    "block_length"?: 5}``. 422 on missing/empty/mismatched inputs.
    """
    from app.platform.spa_test import spa_test

    blf = payload.get("benchmark_lf")
    mlf = payload.get("model_lfs")
    if not isinstance(blf, list) or not blf:
        raise HTTPException(status_code=422, detail="benchmark_lf must be a non-empty list")
    if not isinstance(mlf, list) or not mlf:
        raise HTTPException(status_code=422, detail="model_lfs must be a non-empty list")
    n = len(blf)
    model_lfs: list[list[float]] = []
    for i, m in enumerate(mlf):
        if not isinstance(m, list) or not m:
            raise HTTPException(status_code=422, detail=f"model_lfs[{i}] must be a non-empty list")
        if len(m) != n:
            raise HTTPException(status_code=422, detail=f"model_lfs[{i}] length must match benchmark_lf")
        model_lfs.append([float(v) for v in m])
    try:
        B = int(payload.get("B", 100))
        block_length = int(payload.get("block_length", 5))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="B and block_length must be integers")
    try:
        rep = spa_test([float(v) for v in blf], model_lfs, B=B, block_length=block_length)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P241 — execution quality scorecard endpoint
# ---------------------------------------------------------------------------


@router.post("/execution-quality", dependencies=[Depends(require_api_key())])
def execution_quality_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P241: execution-quality scorecard (fill ratio, slippage dist, reversion).

    Body: ``{"fills": [{price, benchmark_price, quantity, ...}],
    "benchmark_prices"?: [...], "post_fill_prices"?: [[...]],
    "window"?: 5, "adverse_threshold_bps"?: 5.0}``. 422 on missing/empty inputs.
    """
    from app.platform.execution_quality import execution_scorecard

    fills = payload.get("fills")
    if not isinstance(fills, list) or not fills:
        raise HTTPException(status_code=422, detail="fills must be a non-empty list")
    bps = payload.get("benchmark_prices")
    pfp = payload.get("post_fill_prices")
    if bps is not None:
        if not isinstance(bps, list) or len(bps) != len(fills):
            raise HTTPException(status_code=422, detail="benchmark_prices must match fills length")
    if pfp is not None:
        if not isinstance(pfp, list) or len(pfp) != len(fills):
            raise HTTPException(status_code=422, detail="post_fill_prices must match fills length")
    try:
        window = int(payload.get("window", 5))
        adverse_threshold_bps = float(payload.get("adverse_threshold_bps", 5.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="window/adverse_threshold_bps must be numeric")
    try:
        rep = execution_scorecard(
            fills,
            benchmark_prices=bps,
            post_fill_prices=pfp,
            window=window,
            adverse_threshold_bps=adverse_threshold_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P242 — portfolio diversification metrics endpoint
# ---------------------------------------------------------------------------


@router.post("/diversification", dependencies=[Depends(require_api_key())])
def diversification_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P242: portfolio diversification metrics (effective N, diversification ratio, DR).

    Body: ``{"weights": [...], "sigmas": [...], "cov": [[...], ...]}``.
    422 on missing/empty/mismatched inputs.
    """
    from app.platform.diversification import diversification_report

    w = payload.get("weights")
    s = payload.get("sigmas")
    cov = payload.get("cov")
    if not isinstance(w, list) or not w:
        raise HTTPException(status_code=422, detail="weights must be a non-empty list")
    if not isinstance(s, list) or len(s) != len(w):
        raise HTTPException(status_code=422, detail="sigmas must match weights length")
    if not isinstance(cov, list) or len(cov) != len(w):
        raise HTTPException(status_code=422, detail="cov must be a square matrix matching weights length")
    for row in cov:
        if not isinstance(row, list) or len(row) != len(w):
            raise HTTPException(status_code=422, detail="cov must be a square matrix matching weights length")
    try:
        rep = diversification_report(
            [float(x) for x in w],
            [float(x) for x in s],
            [[float(x) for x in row] for row in cov],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return rep.to_dict()


# ---------------------------------------------------------------------------
# P243 — European option pricing + Greeks endpoint
# ---------------------------------------------------------------------------


@router.post("/options-pricing", dependencies=[Depends(require_api_key())])
def options_pricing_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P243: Black-Scholes-Merton European call/put price + full Greeks.

    Body: ``{"option_type": "call"|"put", "spot": S, "strike": K,
    "time_to_expiry": T, "risk_free": r, "volatility": sigma,
    "dividend_yield": q?}``. 422 on missing/invalid inputs.
    """
    from app.platform.options_pricing import option_price

    ot = payload.get("option_type")
    if ot not in ("call", "put"):
        raise HTTPException(status_code=422, detail="option_type must be 'call' or 'put'")
    try:
        spot = float(payload["spot"])
        strike = float(payload["strike"])
        t = float(payload["time_to_expiry"])
        r = float(payload["risk_free"])
        sigma = float(payload["volatility"])
        q = float(payload.get("dividend_yield", 0.0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="spot/strike/time_to_expiry/risk_free/volatility must be numbers")
    try:
        res = option_price(ot, spot, strike, t, r, sigma, q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P244 — implied volatility + SVI endpoint
# ---------------------------------------------------------------------------


@router.post("/implied-volatility", dependencies=[Depends(require_api_key())])
def implied_volatility_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P244: invert Black-Scholes for σ, or fit a raw-SVI slice.

    Mode 1 (single IV): ``{"mode": "iv", "option_type", "price", "spot",
    "strike", "time_to_expiry", "risk_free", "dividend_yield"?}`` →
    ``implied_vol``.
    Mode 2 (SVI fit): ``{"mode": "svi", "log_moneyness": [...],
    "implied_vols": [...], "time_to_expiry": T}`` → SVI params + rms.
    422 on missing/invalid inputs.
    """
    from app.platform.implied_volatility import implied_volatility, svi_fit

    mode = payload.get("mode", "iv")
    if mode == "svi":
        ks = payload.get("log_moneyness")
        ivs = payload.get("implied_vols")
        t = payload.get("time_to_expiry")
        if not isinstance(ks, list) or not isinstance(ivs, list) or len(ks) != len(ivs):
            raise HTTPException(status_code=422, detail="log_moneyness and implied_vols must be equal-length lists")
        if len(ks) < 5:
            raise HTTPException(status_code=422, detail="at least 5 points required to fit SVI")
        if t is None:
            raise HTTPException(status_code=422, detail="time_to_expiry must be a number")
        try:
            t = float(t)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="time_to_expiry must be a number")
        try:
            fit = svi_fit([float(x) for x in ks], [float(x) for x in ivs], t)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"mode": "svi", **fit.to_dict()}
    # default: single IV
    ot = payload.get("option_type")
    if ot not in ("call", "put"):
        raise HTTPException(status_code=422, detail="option_type must be 'call' or 'put'")
    try:
        price = float(payload["price"])
        spot = float(payload["spot"])
        strike = float(payload["strike"])
        t = float(payload["time_to_expiry"])
        r = float(payload["risk_free"])
        q = float(payload.get("dividend_yield", 0.0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="price/spot/strike/time_to_expiry/risk_free must be numbers")
    try:
        iv = implied_volatility(ot, price, spot, strike, t, r, q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "mode": "iv",
        "option_type": ot,
        "price": price,
        "implied_vol": iv,
        "spot": spot,
        "strike": strike,
        "time_to_expiry": t,
        "risk_free": r,
        "dividend_yield": q,
    }


# ---------------------------------------------------------------------------
# P245 — Kalman filter + RTS smoother endpoint
# ---------------------------------------------------------------------------


@router.post("/kalman-filter", dependencies=[Depends(require_api_key())])
def kalman_filter_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P245: forward Kalman filter + optional RTS smoother.

    Body: ``{"observations": [[z], ...], "F": [[...]], "H": [[...]],
    "Q": [[...]], "R": [[...]], "x0": [...], "P0": [[...]],
    "smooth": bool?, "B": [[...]]?, "u": [[...]]?}``. 422 on invalid inputs.
    """
    from app.platform.kalman_filter import kalman_filter, rts_smoother

    obs = payload.get("observations")
    if not isinstance(obs, list) or not obs:
        raise HTTPException(status_code=422, detail="observations must be a non-empty list of vectors")
    for row in obs:
        if not isinstance(row, list):
            raise HTTPException(status_code=422, detail="each observation must be a list")
    F = payload.get("F")
    H = payload.get("H")
    Q = payload.get("Q")
    R = payload.get("R")
    x0 = payload.get("x0")
    P0 = payload.get("P0")
    for name, val in (("F", F), ("H", H), ("Q", Q), ("R", R), ("P0", P0)):
        if not isinstance(val, list) or not val or not isinstance(val[0], list):
            raise HTTPException(status_code=422, detail=f"{name} must be a non-empty matrix")
    if not isinstance(x0, list) or not x0:
        raise HTTPException(status_code=422, detail="x0 must be a non-empty vector")
    B = payload.get("B")
    u = payload.get("u")
    if (B is None) != (u is None):
        raise HTTPException(status_code=422, detail="B and u must be supplied together")
    if not isinstance(F, list) or not isinstance(H, list) or not isinstance(Q, list) \
            or not isinstance(R, list) or not isinstance(x0, list) or not isinstance(P0, list):
        raise HTTPException(status_code=422, detail="F/H/Q/R/x0/P0 must be lists")
    F_: list[list[float]] = F  # type: ignore[assignment]
    H_: list[list[float]] = H  # type: ignore[assignment]
    Q_: list[list[float]] = Q  # type: ignore[assignment]
    R_: list[list[float]] = R  # type: ignore[assignment]
    P0_: list[list[float]] = P0  # type: ignore[assignment]
    x0_: list[float] = x0  # type: ignore[assignment]
    obs_: list[list[float]] = obs  # type: ignore[assignment]
    try:
        Fm = [[float(x) for x in row] for row in F_]
        Hm = [[float(x) for x in row] for row in H_]
        Qm = [[float(x) for x in row] for row in Q_]
        Rm = [[float(x) for x in row] for row in R_]
        x0v = [float(x) for x in x0_]
        P0m = [[float(x) for x in row] for row in P0_]
        obsv = [[float(x) for x in row] for row in obs_]
        Bm = [[float(x) for x in row] for row in B] if B is not None else None
        uv = [[float(x) for x in row] for row in u] if u is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="matrix/vector entries must be numeric")
    try:
        res = kalman_filter(obsv, Fm, Hm, Qm, Rm, x0v, P0m, B=Bm, u=uv)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = res.to_dict()
    if payload.get("smooth"):
        try:
            sm = rts_smoother(res, Fm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        out["smoothed_means"] = sm.smoothed_means
        out["smoothed_covs"] = sm.smoothed_covs
    return out


# ---------------------------------------------------------------------------
# P246 — stochastic processes / SDE endpoint
# ---------------------------------------------------------------------------


@router.post("/stochastic-processes", dependencies=[Depends(require_api_key())])
def stochastic_processes_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P246: simulate GBM / OU / CIR / Merton-JD and return analytic moments.

    Body: ``{"process": "gbm"|"ou"|"cir"|"merton_jd", ...params, "horizon": T,
    "n_steps": N, "seed": s?, "include_moments": bool?}``. 422 on invalid.
    """
    from app.platform import stochastic_processes as sp

    proc = payload.get("process")
    if proc not in ("gbm", "ou", "cir", "merton_jd"):
        raise HTTPException(status_code=422, detail="process must be one of gbm/ou/cir/merton_jd")
    try:
        horizon = float(payload["horizon"])
        n_steps = int(payload["n_steps"])
        seed = int(payload.get("seed", 0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="horizon/n_steps/seed must be numbers")
    try:
        if proc == "gbm":
            res = sp.gbm_simulate(
                float(payload["s0"]), float(payload["mu"]), float(payload["sigma"]),
                horizon, n_steps, seed=seed,
            )
            mom = sp.gbm_moments(float(payload["s0"]), float(payload["mu"]), float(payload["sigma"]), horizon)
        elif proc == "ou":
            res = sp.ou_simulate(
                float(payload["x0"]), float(payload["kappa"]), float(payload["theta"]),
                float(payload["sigma"]), horizon, n_steps, seed=seed,
            )
            mom = sp.ou_moments(float(payload["x0"]), float(payload["kappa"]), float(payload["theta"]),
                                float(payload["sigma"]), horizon)
        elif proc == "cir":
            res = sp.cir_simulate(
                float(payload["r0"]), float(payload["kappa"]), float(payload["theta"]),
                float(payload["sigma"]), horizon, n_steps, seed=seed,
            )
            mom = sp.cir_moments(float(payload["r0"]), float(payload["kappa"]), float(payload["theta"]),
                                 float(payload["sigma"]), horizon)
        else:  # merton_jd
            res = sp.merton_jd_simulate(
                float(payload["s0"]), float(payload["mu"]), float(payload["sigma"]),
                float(payload["jump_lambda"]), float(payload["jump_mean"]),
                float(payload["jump_std"]), horizon, n_steps, seed=seed,
            )
            mom = sp.merton_jd_moments(
                float(payload["s0"]), float(payload["mu"]), float(payload["sigma"]),
                float(payload["jump_lambda"]), float(payload["jump_mean"]),
                float(payload["jump_std"]), horizon,
            )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing parameter: {exc.args[0]}")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = res.to_dict()
    if payload.get("include_moments", True):
        out["moments"] = mom
    return out


# ---------------------------------------------------------------------------
# P247 — statistical-arbitrage signals endpoint
# ---------------------------------------------------------------------------


@router.post("/stat-arb-signals", dependencies=[Depends(require_api_key())])
def stat_arb_signals_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P247: distance-method stat-arb signals + z-score + OU half-life.

    Body: ``{"y": [...], "x": [...], "entry": 2.0?, "exit": 0.5?,
    "window": int?}``. 422 on missing/unequal/empty/invalid thresholds.
    """
    from app.platform.stat_arb_signals import stat_arb_signals

    y = payload.get("y")
    x = payload.get("x")
    if not isinstance(y, list) or not isinstance(x, list):
        raise HTTPException(status_code=422, detail="y and x must be lists")
    if len(y) != len(x):
        raise HTTPException(status_code=422, detail="y and x must have equal length")
    if not y:
        raise HTTPException(status_code=422, detail="y and x must be non-empty")
    try:
        entry = float(payload.get("entry", 2.0))
        exit_ = float(payload.get("exit", 0.5))
        window = payload.get("window")
        window_i = int(window) if window is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="entry/exit/window must be numbers")
    try:
        res = stat_arb_signals(
            [float(v) for v in y], [float(v) for v in x],
            entry=entry, exit=exit_, window=window_i,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P248 — robust statistics endpoint
# ---------------------------------------------------------------------------


@router.post("/robust-statistics", dependencies=[Depends(require_api_key())])
def robust_statistics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P248: robust location/scale + optional Theil-Sen regression.

    Body: ``{"xs": [...]}`` plus optional ``{"y": [...], "x": [...]}`` for
    Theil-Sen, and ``alpha?``, ``huber_k?``. 422 on missing/empty/invalid.
    """
    from app.platform.robust_statistics import robust_stats

    xs = payload.get("xs")
    if not isinstance(xs, list) or not xs:
        raise HTTPException(status_code=422, detail="xs must be a non-empty list")
    y = payload.get("y")
    x = payload.get("x")
    if (y is None) != (x is None):
        raise HTTPException(status_code=422, detail="y and x must be supplied together")
    try:
        alpha = float(payload.get("alpha", 0.1))
        huber_k = float(payload.get("huber_k", 1.345))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="alpha/huber_k must be numbers")
    try:
        xs_f = [float(v) for v in xs]
        y_f = [float(v) for v in y] if y is not None else None
        x_f = [float(v) for v in x] if x is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="inputs must be numeric")
    try:
        res = robust_stats(xs_f, y=y_f, x=x_f, alpha=alpha, huber_k=huber_k)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P249 — bandit strategy selection endpoint
# ---------------------------------------------------------------------------


@router.post("/bandits", dependencies=[Depends(require_api_key())])
def bandits_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P249: simulate a multi-armed bandit over K arms.

    Body: ``{"algorithm": "epsilon_greedy"|"ucb1"|"thompson_beta"|
    "thompson_gaussian", "true_means": [...], "n_steps": N, "seed": s?,
    "epsilon": 0.1?, "sigmas": [...]?}``. 422 on invalid.
    """
    from app.platform.bandits import simulate

    alg = payload.get("algorithm")
    if alg not in ("epsilon_greedy", "ucb1", "thompson_beta", "thompson_gaussian"):
        raise HTTPException(status_code=422, detail="algorithm must be epsilon_greedy/ucb1/thompson_beta/thompson_gaussian")
    means = payload.get("true_means")
    if not isinstance(means, list) or not means:
        raise HTTPException(status_code=422, detail="true_means must be a non-empty list")
    try:
        n_steps = int(payload["n_steps"])
        seed = int(payload.get("seed", 0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="n_steps/seed must be integers")
    try:
        epsilon = float(payload.get("epsilon", 0.1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="epsilon must be a number")
    sigmas = payload.get("sigmas")
    try:
        sigmas_f = [float(s) for s in sigmas] if sigmas is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="sigmas must be numeric")
    try:
        res = simulate(alg, [float(m) for m in means], n_steps, seed=seed,
                       epsilon=epsilon, sigmas=sigmas_f)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P250 — LOESS / LOWESS endpoint
# ---------------------------------------------------------------------------


@router.post("/loess", dependencies=[Depends(require_api_key())])
def loess_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P250: locally-weighted linear regression (LOWESS) with robust iterations.

    Body: ``{"x": [...], "y": [...], "bandwidth": 0.3?, "iterations": 2?}``.
    422 on missing/unequal/empty/invalid bandwidth.
    """
    from app.platform.loess import lowess

    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, list) or not isinstance(y, list):
        raise HTTPException(status_code=422, detail="x and y must be lists")
    if len(x) != len(y):
        raise HTTPException(status_code=422, detail="x and y must have equal length")
    if len(x) < 2:
        raise HTTPException(status_code=422, detail="need at least 2 points")
    try:
        bandwidth = float(payload.get("bandwidth", 0.3))
        iterations = int(payload.get("iterations", 2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="bandwidth/iterations must be numbers")
    try:
        res = lowess([float(v) for v in x], [float(v) for v in y],
                     bandwidth=bandwidth, iterations=iterations)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P251 — smart order routing endpoint
# ---------------------------------------------------------------------------


@router.post("/smart-order-routing", dependencies=[Depends(require_api_key())])
def smart_order_routing_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P251: plan a multi-venue best-execution order routing.

    Body: ``{"side": "buy"|"sell", "quantity": Q, "venues": [
    {"venue", "bid", "bid_size", "ask", "ask_size", "fee_per_share"?, "tick_size"?},
    ...]}``. 422 on missing/invalid inputs.
    """
    from app.platform.smart_order_routing import route_order

    side = payload.get("side")
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail="side must be 'buy' or 'sell'")
    qty = payload.get("quantity")
    venues = payload.get("venues")
    if not isinstance(venues, list) or not venues:
        raise HTTPException(status_code=422, detail="venues must be a non-empty list")
    if qty is None:
        raise HTTPException(status_code=422, detail="quantity must be an integer")
    try:
        quantity = int(qty)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="quantity must be an integer")
    try:
        res = route_order(side, quantity, venues)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P252 — vine copula endpoint
# ---------------------------------------------------------------------------


@router.post("/vine-copula", dependencies=[Depends(require_api_key())])
def vine_copula_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P252: fit a vine copula (C-vine / D-vine) to a multi-asset panel.

    Body: ``{"data": [[asset0], [asset1], ...], "structure": "c-vine"|"d-vine"?,
    "family": "gaussian"|"gumbel"|"clayton"?}``. 422 on invalid.
    """
    from app.platform.vine_copula import vine_copula

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise HTTPException(status_code=422, detail="data must be a non-empty list of series")
    for s in data:
        if not isinstance(s, list):
            raise HTTPException(status_code=422, detail="each series must be a list")
    structure = payload.get("structure", "c-vine")
    if structure not in ("c-vine", "d-vine"):
        raise HTTPException(status_code=422, detail="structure must be 'c-vine' or 'd-vine'")
    family = payload.get("family")
    if family is not None and family not in ("gaussian", "gumbel", "clayton"):
        raise HTTPException(status_code=422, detail="family must be gaussian/gumbel/clayton or omitted")
    try:
        res = vine_copula(
            [[float(v) for v in s] for s in data],
            structure=structure,
            family=family,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P253 — American options (CRR binomial tree) endpoint
# ---------------------------------------------------------------------------


@router.post("/american-options", dependencies=[Depends(require_api_key())])
def american_options_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P253: American/European option via the CRR binomial tree.

    Body: ``{"option_type": "call"|"put", "spot": S, "strike": K,
    "time_to_expiry": T, "risk_free": r, "volatility": sigma, "steps": N?,
    "dividend_yield": q?, "exercise": "american"|"european"?}``. 422 on invalid.
    """
    from app.platform.american_options import binomial_price

    ot = payload.get("option_type")
    if ot not in ("call", "put"):
        raise HTTPException(status_code=422, detail="option_type must be 'call' or 'put'")
    try:
        spot = float(payload["spot"])
        strike = float(payload["strike"])
        t = float(payload["time_to_expiry"])
        r = float(payload["risk_free"])
        sigma = float(payload["volatility"])
        q = float(payload.get("dividend_yield", 0.0))
        steps = int(payload.get("steps", 200))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="spot/strike/time_to_expiry/risk_free/volatility must be numbers")
    exercise = payload.get("exercise", "american")
    if exercise not in ("american", "european"):
        raise HTTPException(status_code=422, detail="exercise must be 'american' or 'european'")
    try:
        res = binomial_price(ot, spot, strike, t, r, sigma, steps=steps,
                             dividend_yield=q, exercise=exercise)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P254 — Heston stochastic volatility (moment-matched QMC) endpoint
# ---------------------------------------------------------------------------


@router.post("/heston", dependencies=[Depends(require_api_key())])
def heston_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P254: price a European option under the Heston SV model (QMC).

    Body: ``{"option_type": "call"|"put", "spot", "strike", "time_to_expiry",
    "risk_free", "v0", "kappa", "theta", "sigma", "rho",
    "n_paths"?, "n_steps"?, "seed"?, "moment_match"?}``. 422 on invalid.
    """
    from app.platform.heston import heston_quasi_monte_carlo

    ot = payload.get("option_type")
    if ot not in ("call", "put"):
        raise HTTPException(status_code=422, detail="option_type must be 'call' or 'put'")
    try:
        spot = float(payload["spot"])
        strike = float(payload["strike"])
        t = float(payload["time_to_expiry"])
        r = float(payload["risk_free"])
        v0 = float(payload["v0"])
        kappa = float(payload["kappa"])
        theta = float(payload["theta"])
        sigma = float(payload["sigma"])
        rho = float(payload["rho"])
        n_paths = int(payload.get("n_paths", 20000))
        n_steps = int(payload.get("n_steps", 64))
        seed = int(payload.get("seed", 0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="numeric parameters required (spot/strike/time_to_expiry/risk_free/v0/kappa/theta/sigma/rho)")
    mm = payload.get("moment_match", True)
    try:
        res = heston_quasi_monte_carlo(
            ot, spot, strike, t, r, v0, kappa, theta, sigma, rho,
            n_paths=n_paths, n_steps=n_steps, seed=seed, moment_match=bool(mm),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P255 — Nelson-Siegel-Svensson yield curve endpoint
# ---------------------------------------------------------------------------


@router.post("/yield-curve", dependencies=[Depends(require_api_key())])
def yield_curve_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P255: fit a Nelson-Siegel-Svensson yield curve + evaluate at requested maturities.

    Body: ``{"maturities": [...], "yields": [...], "evaluate_maturities": [...]?}``.
    Returns the NSS fit params + rms + the curve sampled at ``evaluate_maturities``
    (defaults to ``maturities``). 422 on invalid inputs.
    """
    from app.platform.nelson_siegel import fit_nss, nelson_siegel_svensson_rate

    ms = payload.get("maturities")
    ys = payload.get("yields")
    if not isinstance(ms, list) or not isinstance(ys, list) or len(ms) != len(ys):
        raise HTTPException(status_code=422, detail="maturities and yields must be equal-length lists")
    if len(ms) < 2:
        raise HTTPException(status_code=422, detail="need at least 2 points")
    if any((not isinstance(m, (int, float))) or m < 0 for m in ms):
        raise HTTPException(status_code=422, detail="maturities must be non-negative numbers")
    try:
        fit = fit_nss([float(m) for m in ms], [float(y) for y in ys])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = fit.to_dict()
    eval_ms = payload.get("evaluate_maturities", ms)
    if not isinstance(eval_ms, list):
        raise HTTPException(status_code=422, detail="evaluate_maturities must be a list")
    out["curve"] = [
        {"maturity": float(m),
         "zero_rate": nelson_siegel_svensson_rate(float(m), fit.beta0, fit.beta1, fit.beta2, fit.beta3, fit.tau1, fit.tau2)}
        for m in eval_ms
    ]
    return out


# ---------------------------------------------------------------------------
# P256 — fixed-income analytics endpoint
# ---------------------------------------------------------------------------


@router.post("/fixed-income", dependencies=[Depends(require_api_key())])
def fixed_income_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P256: bond analytics (YTM / duration / convexity) + optional forward rate.

    Body: ``{"price", "face", "coupon", "periods"}`` plus optional
    ``{"spot_short", "spot_long", "short_maturity", "long_maturity"}`` for a
    forward rate. 422 on invalid inputs.
    """
    from app.platform.fixed_income import bond_analytics, forward_rate

    try:
        price = float(payload["price"])
        face = float(payload["face"])
        coupon = float(payload["coupon"])
        periods = int(payload["periods"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="price/face/coupon/periods must be numbers")
    try:
        res = bond_analytics(price, face, coupon, periods)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out = res.to_dict()
    if "spot_short" in payload and "spot_long" in payload:
        try:
            ss = float(payload["spot_short"])
            sl = float(payload["spot_long"])
            sm = float(payload["short_maturity"])
            lm = float(payload["long_maturity"])
            out["forward_rate"] = forward_rate(ss, sl, sm, lm)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    return out


# ---------------------------------------------------------------------------
# P257 — PCA (cyclic Jacobi) endpoint
# ---------------------------------------------------------------------------


@router.post("/pca", dependencies=[Depends(require_api_key())])
def pca_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P257: principal component analysis (Jacobi eigen-decomposition).

    Body: ``{"data": [[...], ...]}`` (rows = samples, equal-length features),
    optional ``"n_components": k``. Returns eigenvalues, eigenvectors,
    explained/cumulative variance ratios, and the projection. 422 on invalid.
    """
    from app.platform.pca import pca

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise HTTPException(status_code=422, detail="data must be a non-empty matrix (list of rows)")
    p = len(data[0])
    for row in data:
        if not isinstance(row, list) or len(row) != p:
            raise HTTPException(status_code=422, detail="all rows must be equal-length lists")
    n_components = payload.get("n_components")
    k = None
    if n_components is not None:
        try:
            k = int(n_components)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="n_components must be an integer")
    try:
        res = pca([[float(v) for v in row] for row in data], n_components=k)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return res.to_dict()


# ---------------------------------------------------------------------------
# P259 — Spectral analysis (naive DFT periodogram) endpoint
# ---------------------------------------------------------------------------


def _numeric_series(payload: dict[str, Any], *, field: str = "series") -> list[float]:
    """Validate a ``field`` payload entry as a non-empty finite-number list.

    Mirrors :func:`_fractional_series` for any numeric-series endpoint. Raises
    ``ValueError`` / ``TypeError`` which the caller converts into HTTP 422.
    """
    raw_series = payload.get(field)
    if not isinstance(raw_series, list):
        raise ValueError(f"{field} must be a non-empty list of finite numbers")
    series = [_finite_number(value, f"{field} entries") for value in raw_series]
    if not series:
        raise ValueError(f"{field} must be non-empty")
    if len(series) > 5000:
        raise ValueError(f"{field} must contain at most 5000 values")
    return series


@router.post("/spectral-analysis", dependencies=[Depends(require_api_key())])
def spectral_analysis_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P259: naive DFT periodogram, spectral entropy, and band-energy share.

    Body: ``{"series": [...], "sample_rate": float, "bands": [[low, high], ...]}``
    where ``bands`` is optional. Returns the dominant non-DC frequency, the
    Shannon spectral entropy (normalised to ``[0, 1]``), per-band energy share,
    and the full periodogram / frequency grid. HTTP 422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        sample_rate = _finite_number(payload.get("sample_rate", 1.0), "sample_rate")
        bands_raw = payload.get("bands")
        bands: list[tuple[float, float]] | None = None
        if bands_raw is not None:
            if not isinstance(bands_raw, list):
                raise ValueError("bands must be a list of [low, high] pairs")
            bands = []
            for idx, pair in enumerate(bands_raw):
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError(f"bands[{idx}] must be a [low, high] pair")
                bands.append((
                    _finite_number(pair[0], f"bands[{idx}] low"),
                    _finite_number(pair[1], f"bands[{idx}] high"),
                ))
        report = spectral_report(series, sample_rate=sample_rate, bands=bands)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return report.to_dict()


# ---------------------------------------------------------------------------
# P260 — Cycle detection (autocorrelation / Ljung-Box / seasonal strength)
# ---------------------------------------------------------------------------


@router.post("/cycle-detection", dependencies=[Depends(require_api_key())])
def cycle_detection_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P260: detect candidate cycle periods via autocorrelation.

    Body: ``{"series": [...], "min_period": int, "max_period": int}`` where
    ``min_period`` (default 2) and ``max_period`` (default ``min(N//2, 24)``)
    bound the scanned lag range. Returns cycle candidates ranked by composite
    score, a normalised ``seasonal_strength`` in ``[0, 1]``, and the
    Ljung-Box portmanteau statistic. HTTP 422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        min_period_raw = payload.get("min_period", 2)
        max_period_raw = payload.get("max_period")
        # Validate the optional int bounds. Booleans are a subclass of int in
        # Python; reject them explicitly so ``True`` is not silently accepted
        # as period 1.
        if isinstance(min_period_raw, bool):
            raise TypeError("min_period must be an int")
        if not isinstance(min_period_raw, int):
            raise TypeError("min_period must be an int")
        if max_period_raw is not None:
            if isinstance(max_period_raw, bool) or not isinstance(max_period_raw, int):
                raise TypeError("max_period must be an int or null")
        result = detect_cycles(
            series,
            min_period=min_period_raw,
            max_period=max_period_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P261 — Change-point detection (binary segmentation) endpoint
# ---------------------------------------------------------------------------


@router.post("/change-point", dependencies=[Depends(require_api_key())])
def change_point_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P261: detect change points via recursive binary segmentation.

    Body: ``{"series": [...], "min_size": int, "max_points": int,
    "threshold": float}`` where ``min_size`` (default 5), ``max_points``
    (default 3) and ``threshold`` (default 0.0) are optional. Returns the
    ranked change points (mean/variance effect sizes + combined score), the
    strongest change index, a normalised ``confidence`` in ``[0, 1]``, and
    the contiguous ``segments`` implied by the detected change points. HTTP
    422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        min_size_raw = payload.get("min_size", 5)
        max_points_raw = payload.get("max_points", 3)
        threshold_raw = payload.get("threshold", 0.0)
        # Validate the optional int parameters; booleans are a subclass of int
        # in Python and are rejected explicitly so ``True`` is not silently
        # accepted as ``min_size=1``.
        if isinstance(min_size_raw, bool) or not isinstance(min_size_raw, int):
            raise TypeError("min_size must be an int")
        if isinstance(max_points_raw, bool) or not isinstance(max_points_raw, int):
            raise TypeError("max_points must be an int")
        threshold = _finite_number(threshold_raw, "threshold")
        result = detect_change_points(
            series,
            min_size=min_size_raw,
            max_points=max_points_raw,
            threshold=threshold,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P262 — Entropy & complexity diagnostics endpoint
# ---------------------------------------------------------------------------


@router.post("/entropy-complexity", dependencies=[Depends(require_api_key())])
def entropy_complexity_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P262: Shannon / sample / permutation entropies plus a Hurst exponent.

    Body: ``{"series": [...], "bins"?: int, "sample_m"?: int,
    "permutation_order"?: int}`` where ``bins`` (default 10) bounds the Shannon
    histogram, ``sample_m`` (default 2) is the sample-entropy template length,
    and ``permutation_order`` (default 3) is the ordinal-pattern order. Returns
    the four metrics, ``n`` (series length), and ``approximation`` (estimator
    family). HTTP 422 on invalid input.
    """
    from app.platform.entropy_complexity import entropy_complexity_report

    try:
        series = _numeric_series(payload)
        bins_raw = payload.get("bins", 10)
        if isinstance(bins_raw, bool) or not isinstance(bins_raw, int):
            raise TypeError("bins must be an int")
        sample_m_raw = payload.get("sample_m", 2)
        if isinstance(sample_m_raw, bool) or not isinstance(sample_m_raw, int):
            raise TypeError("sample_m must be an int")
        permutation_order_raw = payload.get("permutation_order", 3)
        if isinstance(permutation_order_raw, bool) or not isinstance(permutation_order_raw, int):
            raise TypeError("permutation_order must be an int")
        result = entropy_complexity_report(
            series,
            bins=bins_raw,
            sample_m=sample_m_raw,
            permutation_order=permutation_order_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P263 — Rolling feature report (mean / std / zscore / skew / kurtosis / ewma / beta)
# ---------------------------------------------------------------------------


@router.post("/rolling-features", dependencies=[Depends(require_api_key())])
def rolling_features_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P263: trailing-window rolling statistics for a scalar series.

    Body: ``{"series": [...], "window"?: int, "alpha"?: float,
    "benchmark"?: [...]}`` where ``window`` (default 5) is the trailing
    window size, ``alpha`` (default 0.2) is the EWMA smoothing factor in
    ``(0, 1]``, and ``benchmark`` (optional) is a same-length series used
    to compute a rolling regression beta. Returns length-``N`` lists for
    ``mean``, ``std``, ``zscore``, ``skew``, ``kurtosis`` and ``ewma``
    (with leading ``None`` warm-up entries for the rolling stats), plus
    ``beta`` — either a length-``N`` list or ``None`` when no benchmark
    was supplied. HTTP 422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        window_raw = payload.get("window", 5)
        # Reject booleans explicitly: bool subclasses int in Python and
        # ``True`` would otherwise be silently accepted as ``window=1``.
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise TypeError("window must be an int")
        alpha = _finite_number(payload.get("alpha", 0.2), "alpha")
        benchmark_raw = payload.get("benchmark")
        benchmark: list[float] | None = None
        if benchmark_raw is not None:
            if not isinstance(benchmark_raw, list):
                raise ValueError("benchmark must be a list of finite numbers")
            benchmark = [
                _finite_number(value, "benchmark entries")
                for value in benchmark_raw
            ]
            if not benchmark:
                raise ValueError("benchmark must be non-empty")
        result = rolling_feature_report(
            series,
            window=window_raw,
            alpha=alpha,
            benchmark=benchmark,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P264 — factor IC analysis endpoint
# ---------------------------------------------------------------------------


def _factor_ic_series(payload: dict[str, Any], field: str) -> list[float]:
    """Validate a ``field`` payload entry as a finite-number list (P264 helper).

    Reuses the platform's shared :func:`_finite_number` so the bool / NaN
    rejection contract matches every other numeric-series endpoint. Raises
    ``ValueError`` / ``TypeError`` which the caller converts into HTTP 422.
    """
    raw = payload.get(field)
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a non-empty list of finite numbers")
    if not raw:
        raise ValueError(f"{field} must be a non-empty list")
    return [_finite_number(v, f"{field} entries") for v in raw]


@router.post("/factor-ic", dependencies=[Depends(require_api_key())])
def factor_ic_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P264: single-period cross-sectional factor IC analysis.

    Body: ``{"factor": [...], "forward_returns": [...], "n_quantiles"?: int}``
    where ``factor`` and ``forward_returns`` are aligned 1-to-1 and
    ``n_quantiles`` (default 5) is in ``[2, len]``. Returns the Pearson IC,
    Spearman / rank IC, single-period ICIR approximation, per-quantile
    bucket decomposition (count + mean return), and the top-minus-bottom
    quantile spread. HTTP 422 on invalid input.
    """
    try:
        factor = _factor_ic_series(payload, "factor")
        forward_returns = _factor_ic_series(payload, "forward_returns")
        n_quantiles_raw = payload.get("n_quantiles", 5)
        # Reject booleans explicitly: bool subclasses int in Python and ``True``
        # would otherwise be silently accepted as ``n_quantiles=1``.
        if isinstance(n_quantiles_raw, bool) or not isinstance(n_quantiles_raw, int):
            raise TypeError("n_quantiles must be an int")
        result = factor_ic_report(
            factor, forward_returns, n_quantiles=n_quantiles_raw
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P265 — feature orthogonalization endpoint
# ---------------------------------------------------------------------------


def _panel_field(payload: dict[str, Any], *, field: str = "panel") -> dict[str, list[float]]:
    """Validate a ``{feature: series}`` panel payload entry.

    By default reads ``payload["panel"]``; pass ``field`` to reuse the same
    validator for a differently-named panel entry (e.g. ``"signals"``).
    Mirrors :func:`_finite_number`'s bool / non-finite rejection so the
    panel-validation contract matches every other numeric-series endpoint.
    Raises :class:`ValueError` / :class:`TypeError` which the caller converts
    into HTTP 422.
    """
    raw = payload.get(field)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a non-empty dict of feature series")
    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in raw.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"{field}['{name}'] must be a list of finite numbers")
        vector = [_finite_number(v, f"{field}['{name}'] entries") for v in series]
        if not vector:
            raise ValueError(f"{field}['{name}'] must be a non-empty series")
        if length is None:
            length = len(vector)
        elif len(vector) != length:
            raise ValueError(f"{field} feature series must have equal length")
        validated[str(name)] = vector
    return validated


@router.post("/feature-orthogonalization", dependencies=[Depends(require_api_key())])
def feature_orthogonalization_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P265: Gram-Schmidt + correlation-prune + VIF de-correlation report.

    Body: ``{"panel": {feature: [floats]}, "target"?: [floats],
    "threshold"?: float}`` where ``threshold`` (default ``0.95``) bounds
    correlation pruning in ``[0, 1]``. Returns the orthogonalized features
    for the kept set, the kept/dropped partition, simplified VIF scores over
    the full panel, pairwise Pearson correlations (labelled ``"A|B"``), and
    ``residualized`` (the target residualized against the kept features, or
    ``null`` when no target was supplied). HTTP 422 on invalid input.
    """
    from app.platform.feature_orthogonalization import orthogonalization_report

    try:
        panel = _panel_field(payload)
        target_raw = payload.get("target")
        target: list[float] | None = None
        if target_raw is not None:
            if isinstance(target_raw, (str, dict)) or not isinstance(target_raw, list):
                raise ValueError("target must be a list of finite numbers")
            target = [_finite_number(v, "target entries") for v in target_raw]
            if not target:
                raise ValueError("target must be a non-empty series")
        threshold = _finite_number(payload.get("threshold", 0.95), "threshold")
        result = orthogonalization_report(panel, target=target, threshold=threshold)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P266 — Signal combination
# ---------------------------------------------------------------------------


@router.post("/signal-combination", dependencies=[Depends(require_api_key())])
def signal_combination_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P266: combine a panel of equal-length signals into a single composite.

    Body: ``{"signals": {name: [floats]}, "weights"?: {name: float},
    "method"?: "zscore"|"rank"|"raw"}`` where ``signals`` is a non-empty dict
    of equal-length finite numeric series. ``weights`` (optional) keys must
    match the signal names and have a positive absolute sum; when omitted
    equal weights are used (``|w|`` summing to 1). ``method`` defaults to
    ``"zscore"``. Returns the combined series, the normalized weights, the
    per-signal transformed (standardized) series, the method and the signal
    count. HTTP 422 on invalid input.
    """
    from app.platform.signal_combination import combine_signals

    try:
        signals = _panel_field(payload, field="signals")
        method = str(payload.get("method", "zscore"))
        weights_raw = payload.get("weights")
        weights: dict[str, float] | None = None
        if weights_raw is not None:
            if not isinstance(weights_raw, dict):
                raise ValueError("weights must be a mapping of name to number")
            weights = {}
            for name, value in weights_raw.items():
                weights[str(name)] = _finite_number(value, f"weights['{name}']")
        result = combine_signals(signals, weights=weights, method=method)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P267 — Backtest diagnostics
# ---------------------------------------------------------------------------


@router.post("/backtest-diagnostics", dependencies=[Depends(require_api_key())])
def backtest_diagnostics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P267: trade-level diagnostics (expectancy, payoff, streaks, bootstrap CI).

    Body: ``{"trades": [float, ...], "n_bootstrap"?: int, "seed"?: int}`` where
    ``trades`` is a non-empty list of per-trade PnL values. A trade ``> 0`` is a
    win, ``< 0`` a loss, and ``== 0`` a neutral (counted in ``expectancy`` /
    ``n_trades`` but excluded from win/loss tallies and resetting any active
    streak). Returns expectancy, profit factor, payoff ratio, win/loss rates,
    max win/loss streaks, a deterministic percentile-bootstrap 95 % CI on
    expectancy, and the trade count. HTTP 422 on invalid input.
    """
    from app.platform.backtest_diagnostics import backtest_diagnostics_report

    try:
        trades_raw = payload.get("trades")
        if isinstance(trades_raw, (str, dict)) or not isinstance(trades_raw, list):
            raise ValueError("trades must be a non-empty list of finite numbers")
        trades = [_finite_number(v, "trades entries") for v in trades_raw]
        if not trades:
            raise ValueError("trades must be a non-empty list of finite numbers")
        n_bootstrap_raw = payload.get("n_bootstrap", 1000)
        if isinstance(n_bootstrap_raw, bool) or not isinstance(n_bootstrap_raw, int):
            raise ValueError("n_bootstrap must be an int >= 1")
        n_bootstrap = n_bootstrap_raw
        seed_raw = payload.get("seed")
        if seed_raw is not None and (isinstance(seed_raw, bool) or not isinstance(seed_raw, int)):
            raise ValueError("seed must be None or an int")
        seed: int | None = seed_raw
        result = backtest_diagnostics_report(trades, n_bootstrap=n_bootstrap, seed=seed)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P268 — OHLCV data-quality diagnostics endpoint
# ---------------------------------------------------------------------------


@router.post("/data-quality", dependencies=[Depends(require_api_key())])
def data_quality_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P268: OHLCV bar data-quality diagnostics.

    Body: ``{"bars": [{timestamp, open, high, low, close, volume?}, ...],
    "expected_interval_seconds"?: float, "stale_window"?: int,
    "jump_threshold"?: float}``. Returns ``n_bars``, ``issue_count``,
    ``critical_count``, ``warning_count``, ``issues`` (each with
    ``index``/``field``/``severity``/``message``) and ``is_clean``.
    HTTP 422 on missing/empty/invalid bars or parameters.
    """
    from app.platform.data_quality import data_quality_report

    try:
        bars_raw = payload.get("bars")
        # Reject dicts / strings / scalars up front: dict is iterable but
        # iterating yields keys, which would silently produce nonsense. bool
        # subclasses int and is clearly not a bar list.
        if (
            isinstance(bars_raw, (dict, str, bytes))
            or isinstance(bars_raw, bool)
            or not isinstance(bars_raw, (list, tuple))
        ):
            raise ValueError("bars must be a non-empty list of bar dicts")
        bars: list[dict[str, Any]] = []
        for i, raw in enumerate(bars_raw):
            # Each bar MUST be a dict. Never coerce (e.g. ``dict(b)``):
            # that would accept JSON arrays of key/value pairs (or any other
            # iterable of pairs) as if they were valid bars, silently bypassing
            # the strict schema enforced by data_quality_report.
            if not isinstance(raw, dict) or isinstance(raw, (str, bytes)):
                raise ValueError(f"bars[{i}] must be a dict")
            bars.append(raw)
        expected_interval = payload.get("expected_interval_seconds")
        stale_window_raw = payload.get("stale_window", 3)
        # Reject booleans explicitly: bool subclasses int in Python and
        # ``True`` would otherwise be silently accepted as ``stale_window=1``.
        if isinstance(stale_window_raw, bool) or not isinstance(stale_window_raw, int):
            raise ValueError("stale_window must be a positive int")
        jump_threshold_raw = payload.get("jump_threshold", 0.2)
        result = data_quality_report(
            bars,
            expected_interval_seconds=expected_interval,
            stale_window=stale_window_raw,
            jump_threshold=jump_threshold_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P269–P278 — factor research and strategy diagnostics endpoints
# ---------------------------------------------------------------------------


def _nullable_numeric_panel(payload: dict[str, Any], *, field: str = "panel") -> dict[str, list[float | None]]:
    raw = payload.get(field)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a non-empty dict of feature series")
    out: dict[str, list[float | None]] = {}
    for name, series in raw.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list) or not series:
            raise ValueError(f"{field}['{name}'] must be a non-empty list")
        values: list[float | None] = []
        for value in series:
            values.append(None if value is None else _finite_number(value, f"{field}['{name}'] entries"))
        out[str(name)] = values
    return out


@router.post("/factor-turnover", dependencies=[Depends(require_api_key())])
def factor_turnover_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_turnover import factor_turnover_report

    try:
        snapshots_raw = payload.get("snapshots")
        if not isinstance(snapshots_raw, list):
            raise ValueError("snapshots must be a list of factor snapshots")
        snapshots: list[dict[str, float]] = []
        for i, snapshot in enumerate(snapshots_raw):
            if not isinstance(snapshot, dict):
                raise ValueError(f"snapshots[{i}] must be a dict")
            snapshots.append({str(name): _finite_number(value, f"snapshots[{i}]['{name}']") for name, value in snapshot.items()})
        result = factor_turnover_report(snapshots, bucket_fraction=_finite_number(payload.get("bucket_fraction", 0.2), "bucket_fraction"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-decay", dependencies=[Depends(require_api_key())])
def factor_decay_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_decay import factor_decay_report

    try:
        factor = _factor_ic_series(payload, "factor")
        forward_returns = _panel_field(payload, field="forward_returns")
        result = factor_decay_report(factor, forward_returns)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-quantiles", dependencies=[Depends(require_api_key())])
def factor_quantiles_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_quantiles import factor_quantile_report

    try:
        n_quantiles_raw = payload.get("n_quantiles", 5)
        if isinstance(n_quantiles_raw, bool) or not isinstance(n_quantiles_raw, int):
            raise ValueError("n_quantiles must be an int")
        result = factor_quantile_report(
            _factor_ic_series(payload, "factor"),
            _factor_ic_series(payload, "forward_returns"),
            n_quantiles=n_quantiles_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/ic-diagnostics", dependencies=[Depends(require_api_key())])
def ic_diagnostics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.ic_diagnostics import ic_diagnostics_report

    try:
        result = ic_diagnostics_report(_factor_ic_series(payload, "ic_series"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-data-quality", dependencies=[Depends(require_api_key())])
def factor_data_quality_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_data_quality import factor_data_quality_report

    try:
        stale_window_raw = payload.get("stale_window", 3)
        if isinstance(stale_window_raw, bool) or not isinstance(stale_window_raw, int):
            raise ValueError("stale_window must be an int")
        result = factor_data_quality_report(
            _nullable_numeric_panel(payload),
            stale_window=stale_window_raw,
            outlier_z=_finite_number(payload.get("outlier_z", 3.0), "outlier_z"),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/signal-persistence", dependencies=[Depends(require_api_key())])
def signal_persistence_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.signal_persistence import signal_persistence_report

    try:
        max_lag_raw = payload.get("max_lag", 5)
        if isinstance(max_lag_raw, bool) or not isinstance(max_lag_raw, int):
            raise ValueError("max_lag must be an int")
        signal = _numeric_series(payload, field="signal")
        if len(signal) < 3:
            raise ValueError("signal must contain at least 3 values")
        result = signal_persistence_report(signal, max_lag=max_lag_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/strategy-quality", dependencies=[Depends(require_api_key())])
def strategy_quality_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.strategy_quality import strategy_quality_report

    try:
        result = strategy_quality_report(_numeric_series(payload, field="trades"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/regime-performance", dependencies=[Depends(require_api_key())])
def regime_performance_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.regime_performance import regime_performance_report

    try:
        regimes = payload.get("regimes")
        if not isinstance(regimes, list):
            raise ValueError("regimes must be a list")
        result = regime_performance_report(_numeric_series(payload, field="returns"), regimes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/strategy-diversification", dependencies=[Depends(require_api_key())])
def strategy_diversification_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.strategy_diversification import strategy_diversification_report

    try:
        result = strategy_diversification_report(
            _panel_field(payload, field="strategies"),
            redundancy_threshold=_finite_number(payload.get("redundancy_threshold", 0.9), "redundancy_threshold"),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/backtest-confidence", dependencies=[Depends(require_api_key())])
def backtest_confidence_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.backtest_confidence import backtest_confidence_report

    try:
        n_bootstrap_raw = payload.get("n_bootstrap", 1000)
        window_raw = payload.get("window", 20)
        seed_raw = payload.get("seed")
        if isinstance(n_bootstrap_raw, bool) or not isinstance(n_bootstrap_raw, int):
            raise ValueError("n_bootstrap must be an int")
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int")
        if seed_raw is not None and (isinstance(seed_raw, bool) or not isinstance(seed_raw, int)):
            raise ValueError("seed must be None or an int")
        result = backtest_confidence_report(
            _numeric_series(payload, field="returns"),
            n_bootstrap=n_bootstrap_raw,
            seed=seed_raw,
            window=window_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P279–P288 — ML research pipeline endpoints
# ---------------------------------------------------------------------------


@router.post("/forecast-diagnostics", dependencies=[Depends(require_api_key())])
def forecast_diagnostics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.forecast_diagnostics import forecast_diagnostics_report

    try:
        benchmark = payload.get("benchmark")
        if benchmark is not None:
            benchmark = [_finite_number(v, "benchmark entries") for v in benchmark]
        n_buckets_raw = payload.get("n_buckets", 5)
        if isinstance(n_buckets_raw, bool) or not isinstance(n_buckets_raw, int):
            raise ValueError("n_buckets must be an int")
        result = forecast_diagnostics_report(_numeric_series(payload, field="predictions"), _numeric_series(payload, field="actuals"), benchmark=benchmark, n_buckets=n_buckets_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/triple-barrier-labels", dependencies=[Depends(require_api_key())])
def triple_barrier_labels_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.triple_barrier import triple_barrier_report

    try:
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        max_holding = payload.get("max_holding_bars", 5)
        if isinstance(max_holding, bool) or not isinstance(max_holding, int):
            raise ValueError("max_holding_bars must be an int")
        result = triple_barrier_report(_numeric_series(payload, field="prices"), events, profit_take_pct=_finite_number(payload.get("profit_take_pct", 0.02), "profit_take_pct"), stop_loss_pct=_finite_number(payload.get("stop_loss_pct", 0.01), "stop_loss_pct"), max_holding_bars=max_holding)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/sample-uniqueness", dependencies=[Depends(require_api_key())])
def sample_uniqueness_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.sample_uniqueness import sample_uniqueness_report

    try:
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        result = sample_uniqueness_report(events, time_decay=_finite_number(payload.get("time_decay", 1.0), "time_decay"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/bar-builder", dependencies=[Depends(require_api_key())])
def bar_builder_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.bar_builder import build_bars

    try:
        ticks = payload.get("ticks")
        if not isinstance(ticks, list):
            raise ValueError("ticks must be a list")
        result = build_bars(ticks, mode=str(payload.get("mode", "tick")), threshold=_finite_number(payload.get("threshold", 100), "threshold"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-neutralization", dependencies=[Depends(require_api_key())])
def factor_neutralization_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_neutralization import neutralize_factor

    try:
        factor = _dict_float_field(payload, "factor")
        groups = payload.get("groups")
        if groups is not None and not isinstance(groups, dict):
            raise ValueError("groups must be a dict")
        exposures = payload.get("exposures")
        result = neutralize_factor(factor, method=str(payload.get("method", "market_demean")), groups=groups, exposures=exposures)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-tearsheet", dependencies=[Depends(require_api_key())])
def factor_tearsheet_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_tearsheet import factor_tearsheet_report

    try:
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("records must be a list")
        n_quantiles = payload.get("n_quantiles", 5)
        if isinstance(n_quantiles, bool) or not isinstance(n_quantiles, int):
            raise ValueError("n_quantiles must be an int")
        result = factor_tearsheet_report(records, n_quantiles=n_quantiles, bucket_fraction=_finite_number(payload.get("bucket_fraction", 0.2), "bucket_fraction"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/feature-pipeline", dependencies=[Depends(require_api_key())])
def feature_pipeline_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.feature_pipeline import run_feature_pipeline

    try:
        price_panel = _panel_field(payload, field="price_panel")
        features = payload.get("features")
        if not isinstance(features, list):
            raise ValueError("features must be a list")
        result = run_feature_pipeline(price_panel, features)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/signal-backtest", dependencies=[Depends(require_api_key())])
def signal_backtest_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.signal_backtest import signal_backtest_report

    try:
        entries = payload.get("entries")
        exits = payload.get("exits")
        targets = payload.get("target_positions")
        result = signal_backtest_report(_numeric_series(payload, field="prices"), entries=entries, exits=exits, target_positions=targets, size=_finite_number(payload.get("size", 1.0), "size"), initial_cash=_finite_number(payload.get("initial_cash", 10000.0), "initial_cash"), fee_bps=_finite_number(payload.get("fee_bps", 0.0), "fee_bps"), slippage_bps=_finite_number(payload.get("slippage_bps", 0.0), "slippage_bps"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/rolling-tearsheet", dependencies=[Depends(require_api_key())])
def rolling_tearsheet_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.rolling_tearsheet import rolling_tearsheet_report

    try:
        benchmark_raw = payload.get("benchmark")
        benchmark = None if benchmark_raw is None else [_finite_number(v, "benchmark entries") for v in benchmark_raw]
        windows_raw = payload.get("windows")
        if windows_raw is None:
            windows = None
        else:
            if not isinstance(windows_raw, list):
                raise ValueError("windows must be a list")
            windows = []
            for value in windows_raw:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("windows must contain ints")
                windows.append(value)
        periods_raw = payload.get("periods_per_year", 252)
        if isinstance(periods_raw, bool) or not isinstance(periods_raw, int):
            raise ValueError("periods_per_year must be an int")
        periods_per_year = periods_raw
        result = rolling_tearsheet_report(_numeric_series(payload, field="returns"), benchmark=benchmark, windows=windows, periods_per_year=periods_per_year)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/portfolio-constraints", dependencies=[Depends(require_api_key())])
def portfolio_constraints_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.portfolio_constraints import portfolio_constraints_report

    try:
        result = portfolio_constraints_report(_dict_float_field(payload, "weights"), prev_weights=_optional_float_dict(payload.get("prev_weights")), groups=payload.get("groups"), adv=_optional_float_dict(payload.get("adv")), nav=_finite_number(payload.get("nav", 1.0), "nav"), constraints=_optional_float_dict(payload.get("constraints")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P289–P298 — cross-asset research endpoints
# ---------------------------------------------------------------------------


@router.post("/cross-sectional-dispersion", dependencies=[Depends(require_api_key())])
def cross_sectional_dispersion_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.cross_sectional_dispersion import cross_sectional_dispersion_report

    try:
        result = cross_sectional_dispersion_report(_dict_float_field(payload, "returns"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/variance-risk-premium", dependencies=[Depends(require_api_key())])
def variance_risk_premium_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.variance_risk_premium import variance_risk_premium_report

    try:
        periods = payload.get("periods_per_year", 252)
        if isinstance(periods, bool) or not isinstance(periods, int):
            raise ValueError("periods_per_year must be an int")
        result = variance_risk_premium_report(_numeric_series(payload, field="returns"), _numeric_series(payload, field="implied_vols"), periods_per_year=periods)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/pretrade-cost", dependencies=[Depends(require_api_key())])
def pretrade_cost_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.pretrade_cost import pretrade_cost_report

    try:
        result = pretrade_cost_report(order_qty=_finite_number(payload.get("order_qty"), "order_qty"), adv=_finite_number(payload.get("adv"), "adv"), price=_finite_number(payload.get("price"), "price"), spread_bps=_finite_number(payload.get("spread_bps", 0.0), "spread_bps"), volatility=_finite_number(payload.get("volatility", 0.0), "volatility"), impact_coefficient=_finite_number(payload.get("impact_coefficient", 0.1), "impact_coefficient"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/ensemble-blending", dependencies=[Depends(require_api_key())])
def ensemble_blending_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.ensemble_blending import ensemble_blending_report

    try:
        result = ensemble_blending_report(_panel_field(payload, field="predictions_panel"), _numeric_series(payload, field="actuals"), redundancy_threshold=_finite_number(payload.get("redundancy_threshold", 0.95), "redundancy_threshold"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/option-implied-moments", dependencies=[Depends(require_api_key())])
def option_implied_moments_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.option_implied_moments import option_implied_moments_report

    try:
        options = payload.get("options")
        if not isinstance(options, list):
            raise ValueError("options must be a list")
        result = option_implied_moments_report(options, spot=_finite_number(payload.get("spot"), "spot"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/correlation-regime", dependencies=[Depends(require_api_key())])
def correlation_regime_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.correlation_regime import correlation_regime_report

    try:
        result = correlation_regime_report(_panel_field(payload, field="returns_panel"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/factor-crowding", dependencies=[Depends(require_api_key())])
def factor_crowding_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.factor_crowding import factor_crowding_report

    try:
        result = factor_crowding_report(_dict_float_field(payload, "factor"), valuations=_optional_float_dict(payload.get("valuations")), flows=_optional_float_dict(payload.get("flows")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/curve-spread", dependencies=[Depends(require_api_key())])
def curve_spread_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.curve_spread import curve_spread_report

    try:
        history_raw = payload.get("history")
        history = None if history_raw is None else [_finite_number(value, "history entries") for value in history_raw]
        result = curve_spread_report(_curve_float_field(payload, "curve"), short_tenor=_finite_number(payload.get("short_tenor"), "short_tenor"), long_tenor=_finite_number(payload.get("long_tenor"), "long_tenor"), history=history)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/turnover-attribution", dependencies=[Depends(require_api_key())])
def turnover_attribution_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.turnover_attribution import turnover_attribution_report

    try:
        result = turnover_attribution_report(_dict_float_field(payload, "prev_weights"), _dict_float_field(payload, "current_weights"), drifted_weights=_optional_float_dict(payload.get("drifted_weights")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/signal-information-ratio", dependencies=[Depends(require_api_key())])
def signal_information_ratio_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.signal_information_ratio import signal_information_ratio_report

    try:
        periods = payload.get("periods_per_year", 252)
        buckets = payload.get("n_buckets", 5)
        if isinstance(periods, bool) or not isinstance(periods, int):
            raise ValueError("periods_per_year must be an int")
        if isinstance(buckets, bool) or not isinstance(buckets, int):
            raise ValueError("n_buckets must be an int")
        result = signal_information_ratio_report(_numeric_series(payload, field="signals"), _numeric_series(payload, field="forward_returns"), periods_per_year=periods, n_buckets=buckets)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


def _dict_float_field(payload: dict[str, Any], field: str) -> dict[str, float]:
    raw = payload.get(field)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a non-empty dict")
    return {str(k): _finite_number(v, f"{field}['{k}']") for k, v in raw.items()}


def _optional_float_dict(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("optional mapping fields must be dicts")
    return {str(k): _finite_number(v, f"mapping['{k}']") for k, v in raw.items()}


def _curve_float_field(payload: dict[str, Any], field: str) -> dict[float, float]:
    raw = payload.get(field)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a non-empty dict")
    out: dict[float, float] = {}
    for key, value in raw.items():
        try:
            tenor = float(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("curve tenor keys must be numeric") from exc
        out[_finite_number(tenor, "curve tenor")] = _finite_number(value, f"{field}['{key}']")
    return out


# ---------------------------------------------------------------------------
# P299–P308 — strategy validation & adaptive intelligence endpoints
# ---------------------------------------------------------------------------


@router.post("/regime-factor-returns", dependencies=[Depends(require_api_key())])
def regime_factor_returns_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.regime_factor_returns import regime_factor_returns_report

    try:
        regimes = payload.get("regimes")
        if not isinstance(regimes, list):
            raise ValueError("regimes must be a list")
        result = regime_factor_returns_report(_dict_float_field(payload, "factor"), _dict_float_field(payload, "returns"), regimes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/transfer-entropy", dependencies=[Depends(require_api_key())])
def transfer_entropy_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.transfer_entropy import transfer_entropy_report

    try:
        lag = payload.get("lag", 1)
        if isinstance(lag, bool) or not isinstance(lag, int):
            raise ValueError("lag must be an int")
        bins = payload.get("bins", 10)
        if isinstance(bins, bool) or not isinstance(bins, int):
            raise ValueError("bins must be an int")
        result = transfer_entropy_report(_numeric_series(payload, field="source"), _numeric_series(payload, field="target"), lag=lag, bins=bins)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/event-study", dependencies=[Depends(require_api_key())])
def event_study_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.event_study import event_study_report

    try:
        event_indices = payload.get("event_indices")
        if not isinstance(event_indices, list):
            raise ValueError("event_indices must be a list")
        window_before = payload.get("window_before", 5)
        window_after = payload.get("window_after", 5)
        if isinstance(window_before, bool) or not isinstance(window_before, int):
            raise ValueError("window_before must be an int")
        if isinstance(window_after, bool) or not isinstance(window_after, int):
            raise ValueError("window_after must be an int")
        result = event_study_report(_numeric_series(payload, field="market_returns"), _numeric_series(payload, field="stock_returns"), event_indices, window_before=window_before, window_after=window_after)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/bootstrap-significance", dependencies=[Depends(require_api_key())])
def bootstrap_significance_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.bootstrap_strategy_significance import bootstrap_strategy_significance_report

    try:
        n_bootstrap = payload.get("n_bootstrap", 1000)
        seed = payload.get("seed", 42)
        if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, int):
            raise ValueError("n_bootstrap must be an int")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an int")
        result = bootstrap_strategy_significance_report(_numeric_series(payload, field="returns"), n_bootstrap=n_bootstrap, seed=seed)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/dynamic-factor-exposure", dependencies=[Depends(require_api_key())])
def dynamic_factor_exposure_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.dynamic_factor_exposure import dynamic_factor_exposure_report

    try:
        window = payload.get("window", 20)
        if isinstance(window, bool) or not isinstance(window, int):
            raise ValueError("window must be an int")
        result = dynamic_factor_exposure_report(_numeric_series(payload, field="strategy_returns"), _panel_field(payload, field="factor_panel"), window=window)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/market-impact", dependencies=[Depends(require_api_key())])
def market_impact_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.market_impact_model import market_impact_model_report

    try:
        model = str(payload.get("model", "square_root"))
        result = market_impact_model_report(order_qty=_finite_number(payload.get("order_qty"), "order_qty"), adv=_finite_number(payload.get("adv"), "adv"), volatility=_finite_number(payload.get("volatility"), "volatility"), participation=_finite_number(payload.get("participation", 0.1), "participation"), model=model, permanent_fraction=_finite_number(payload.get("permanent_fraction", 0.5), "permanent_fraction"), price=_finite_number(payload.get("price", 1.0), "price"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/vol-forecast-comparison", dependencies=[Depends(require_api_key())])
def vol_forecast_comparison_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.vol_forecast_comparison import vol_forecast_comparison_report

    try:
        result = vol_forecast_comparison_report(_numeric_series(payload, field="realized_vol"), _panel_field(payload, field="forecasts_panel"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/strategy-capacity", dependencies=[Depends(require_api_key())])
def strategy_capacity_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.strategy_capacity import strategy_capacity_report

    try:
        result = strategy_capacity_report(signal_autocorr=_finite_number(payload.get("signal_autocorr"), "signal_autocorr"), adv=_finite_number(payload.get("adv"), "adv"), turnover=_finite_number(payload.get("turnover"), "turnover"), impact_threshold_bps=_finite_number(payload.get("impact_threshold_bps", 10.0), "impact_threshold_bps"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/momentum-spillover", dependencies=[Depends(require_api_key())])
def momentum_spillover_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.momentum_spillover import momentum_spillover_report

    try:
        max_lag = payload.get("max_lag", 5)
        if isinstance(max_lag, bool) or not isinstance(max_lag, int):
            raise ValueError("max_lag must be an int")
        result = momentum_spillover_report(_numeric_series(payload, field="leader_returns"), _numeric_series(payload, field="lagger_returns"), max_lag=max_lag)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/tail-dependence", dependencies=[Depends(require_api_key())])
def tail_dependence_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.tail_dependence import tail_dependence_report

    try:
        threshold = _finite_number(payload.get("threshold", 0.1), "threshold")
        result = tail_dependence_report(_numeric_series(payload, field="x"), _numeric_series(payload, field="y"), threshold=threshold)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P313 – drawdown surface (depth × duration joint distribution)
# ---------------------------------------------------------------------------


@router.post("/drawdown-surface", dependencies=[Depends(require_api_key())])
def drawdown_surface_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.drawdown_surface import drawdown_surface_report

    try:
        depth_bins = payload.get("depth_bins", 5)
        duration_bins = payload.get("duration_bins", 5)
        if isinstance(depth_bins, bool) or not isinstance(depth_bins, int):
            raise ValueError("depth_bins must be an int")
        if isinstance(duration_bins, bool) or not isinstance(duration_bins, int):
            raise ValueError("duration_bins must be an int")
        result = drawdown_surface_report(
            _numeric_series(payload, field="equity_curve"),
            depth_bins=depth_bins,
            duration_bins=duration_bins,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P314 – tail hedge cost (Hill estimator + CVaR-based put protection cost)
# ---------------------------------------------------------------------------


@router.post("/tail-hedge-cost", dependencies=[Depends(require_api_key())])
def tail_hedge_cost_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.tail_hedge_cost import tail_hedge_cost_report

    try:
        confidence = _finite_number(payload.get("confidence", 0.95), "confidence")
        result = tail_hedge_cost_report(
            _numeric_series(payload, field="returns"),
            confidence=confidence,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P315 – correlation risk premium (implied vs realized correlation spread)
# ---------------------------------------------------------------------------


@router.post("/correlation-risk-premium", dependencies=[Depends(require_api_key())])
def correlation_risk_premium_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.correlation_risk_premium import correlation_risk_premium_report

    try:
        result = correlation_risk_premium_report(
            _numeric_series(payload, field="realized_corr"),
            _numeric_series(payload, field="implied_corr"),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P316 – volatility term structure (ATM IV slope + contango/backwardation)
# ---------------------------------------------------------------------------


@router.post("/vol-term-structure", dependencies=[Depends(require_api_key())])
def vol_term_structure_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.vol_term_structure import vol_term_structure_report

    try:
        spot = _finite_number(payload.get("spot"), "spot")
        options_raw = payload.get("options")
        if not isinstance(options_raw, list):
            raise ValueError("options must be a non-empty list")
        result = vol_term_structure_report(
            options_raw,
            spot=spot,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P321 — causal impact endpoint
# ---------------------------------------------------------------------------


@router.post("/causal-impact", dependencies=[Depends(require_api_key())])
def causal_impact_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P321: estimate causal effect of an intervention via OLS counterfactual.

    Body: ``{"target": [...], "control": [...], "intervention_index": int}``.
    422 on missing/unequal/invalid inputs.
    """
    from app.platform.causal_impact import causal_impact_report

    try:
        target = _numeric_series(payload, field="target")
        control = _numeric_series(payload, field="control")
        intervention_index_raw = payload.get("intervention_index")
        if isinstance(intervention_index_raw, bool) or not isinstance(intervention_index_raw, int):
            raise ValueError("intervention_index must be an int")
        result = causal_impact_report(target, control, intervention_index=intervention_index_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P322 — spread stability endpoint
# ---------------------------------------------------------------------------


@router.post("/spread-stability", dependencies=[Depends(require_api_key())])
def spread_stability_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P322: rolling-window hedge ratio stability + half-life diagnostics.

    Body: ``{"y": [...], "x": [...], "window"?: int}``. 422 on invalid.
    """
    from app.platform.spread_stability import spread_stability_report

    try:
        y = _numeric_series(payload, field="y")
        x = _numeric_series(payload, field="x")
        window_raw = payload.get("window", 20)
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int")
        result = spread_stability_report(y, x, window=window_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P323 — regime transitions endpoint
# ---------------------------------------------------------------------------


@router.post("/regime-transitions", dependencies=[Depends(require_api_key())])
def regime_transitions_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P323: transition matrix + expected duration + steady state from regime labels.

    Body: ``{"regimes": [...]}``. 422 on invalid.
    """
    from app.platform.regime_transitions import regime_transitions_report

    try:
        regimes_raw = payload.get("regimes")
        if not isinstance(regimes_raw, list) or not regimes_raw:
            raise ValueError("regimes must be a non-empty list")
        regimes = [str(r) for r in regimes_raw]
        result = regime_transitions_report(regimes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P324 — regime backtest diagnostics endpoint
# ---------------------------------------------------------------------------


@router.post("/regime-backtest-diagnostics", dependencies=[Depends(require_api_key())])
def regime_backtest_diagnostics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P324: per-regime Sharpe/win_rate/mean/std + optional trade PnL attribution.

    Body: ``{"returns": [...], "regimes": [...], "trade_outcomes"?: [...]}``.
    422 on invalid.
    """
    from app.platform.regime_backtest_diagnostics import regime_backtest_diagnostics_report

    try:
        returns = _numeric_series(payload, field="returns")
        regimes_raw = payload.get("regimes")
        if not isinstance(regimes_raw, list) or not regimes_raw:
            raise ValueError("regimes must be a non-empty list")
        regimes = [str(r) for r in regimes_raw]
        trade_outcomes_raw = payload.get("trade_outcomes")
        trade_outcomes: list[float] | None = None
        if trade_outcomes_raw is not None:
            if not isinstance(trade_outcomes_raw, list):
                raise ValueError("trade_outcomes must be a list")
            trade_outcomes = [_finite_number(v, "trade_outcomes entries") for v in trade_outcomes_raw]
        result = regime_backtest_diagnostics_report(returns, regimes, trade_outcomes=trade_outcomes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P325 — capacity frontier endpoint
# ---------------------------------------------------------------------------


@router.post("/capacity-frontier", dependencies=[Depends(require_api_key())])
def capacity_frontier_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P325: AUM impact-degraded Sharpe curve and optimal capacity.

    Body: ``{"base_sharpe": float, "signal_autocorr": float, "adv": float,
    "turnover": float, "aum_levels"?: [float]}``. Returns per-level degraded
    Sharpe, impact penalty, and optimal_aum. HTTP 422 on invalid input.
    """
    from app.platform.capacity_frontier import capacity_frontier_report

    try:
        aum_levels_raw = payload.get("aum_levels")
        aum_levels: list[float] | None = None
        if aum_levels_raw is not None:
            if not isinstance(aum_levels_raw, list):
                raise ValueError("aum_levels must be a list")
            aum_levels = [_finite_number(v, f"aum_levels[{i}]") for i, v in enumerate(aum_levels_raw)]
        result = capacity_frontier_report(
            base_sharpe=_finite_number(payload.get("base_sharpe"), "base_sharpe"),
            signal_autocorr=_finite_number(payload.get("signal_autocorr"), "signal_autocorr"),
            adv=_finite_number(payload.get("adv"), "adv"),
            turnover=_finite_number(payload.get("turnover"), "turnover"),
            aum_levels=aum_levels,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P326 — regime attribution endpoint
# ---------------------------------------------------------------------------


@router.post("/regime-attribution", dependencies=[Depends(require_api_key())])
def regime_attribution_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P326: decompose returns by market regime (alpha, beta, contribution).

    Body: ``{"returns": [...], "regimes": [...], "benchmark"?: [...]}``.
    Returns per-regime alpha, beta, contribution, volatility. HTTP 422
    on invalid/length-mismatched input.
    """
    from app.platform.regime_attribution import regime_attribution_report

    try:
        returns_raw = payload.get("returns")
        regimes_raw = payload.get("regimes")
        if not isinstance(returns_raw, list) or not returns_raw:
            raise ValueError("returns must be a non-empty list")
        if not isinstance(regimes_raw, list) or not regimes_raw:
            raise ValueError("regimes must be a non-empty list")
        returns = [_finite_number(v, "returns entries") for v in returns_raw]
        regimes = [str(r) for r in regimes_raw]
        benchmark_raw = payload.get("benchmark")
        benchmark: list[float] | None = None
        if benchmark_raw is not None:
            if not isinstance(benchmark_raw, list):
                raise ValueError("benchmark must be a list")
            benchmark = [_finite_number(v, "benchmark entries") for v in benchmark_raw]
        result = regime_attribution_report(returns, regimes, benchmark=benchmark)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P327 — distribution shape endpoint
# ---------------------------------------------------------------------------


@router.post("/distribution-shape", dependencies=[Depends(require_api_key())])
def distribution_shape_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P327: rolling skew, kurtosis, tail-index, fat-tail clusters.

    Body: ``{"returns": [...], "window"?: int}`` (default window=20).
    Returns per-bar shape stats and fat_tail_clusters. HTTP 422 on
    invalid input.
    """
    from app.platform.distribution_shape import distribution_shape_report

    try:
        series = _numeric_series(payload, field="returns")
        window_raw = payload.get("window", 20)
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int")
        window = int(window_raw)
        result = distribution_shape_report(series, window=window)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P328 — walk-forward surface endpoint
# ---------------------------------------------------------------------------


@router.post("/walk-forward-surface", dependencies=[Depends(require_api_key())])
def walk_forward_surface_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P328: rolling IS/OOS Sharpe degradation surface.

    Body: ``{"returns": [...], "train_window"?: int, "test_window"?: int}``
    (defaults 20/10). Returns per-segment is_sharpe, oos_sharpe, degradation
    and summary. HTTP 422 on invalid input.
    """
    from app.platform.walk_forward_surface import walk_forward_surface_report

    try:
        series = _numeric_series(payload, field="returns")
        train_raw = payload.get("train_window", 20)
        test_raw = payload.get("test_window", 10)
        if isinstance(train_raw, bool) or not isinstance(train_raw, int):
            raise ValueError("train_window must be an int")
        if isinstance(test_raw, bool) or not isinstance(test_raw, int):
            raise ValueError("test_window must be an int")
        train_window = int(train_raw)
        test_window = int(test_raw)
        result = walk_forward_surface_report(
            series, train_window=train_window, test_window=test_window
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P309 – pareto optimization endpoint
# ---------------------------------------------------------------------------


@router.post("/pareto-optimize", dependencies=[Depends(require_api_key())])
def pareto_optimize_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.pareto_optimization import pareto_optimize_report

    try:
        configs_raw = payload.get("configs")
        if not isinstance(configs_raw, list) or not configs_raw:
            raise ValueError("configs must be a non-empty list")
        objectives_raw = payload.get("objectives")
        if not isinstance(objectives_raw, list) or not objectives_raw:
            raise ValueError("objectives must be a non-empty list of strings")
        objectives = [str(o) for o in objectives_raw]
        result = pareto_optimize_report(configs_raw, objectives=objectives)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P310 – volume profile endpoint
# ---------------------------------------------------------------------------


@router.post("/volume-profile", dependencies=[Depends(require_api_key())])
def volume_profile_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.volume_profile import volume_profile_report

    try:
        bins_raw = payload.get("bins", 10)
        if isinstance(bins_raw, bool) or not isinstance(bins_raw, int):
            raise ValueError("bins must be a positive integer")
        result = volume_profile_report(
            _numeric_series(payload, field="prices"),
            _numeric_series(payload, field="volumes"),
            bins=bins_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P311 – cost surface endpoint
# ---------------------------------------------------------------------------


@router.post("/cost-surface", dependencies=[Depends(require_api_key())])
def cost_surface_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.cost_surface import cost_surface_report

    try:
        adv = _finite_number(payload.get("adv"), "adv")
        volatility = _finite_number(payload.get("volatility"), "volatility")
        participation_raw = payload.get("participation_levels")
        participation_levels: list[float] | None = None
        if participation_raw is not None:
            if not isinstance(participation_raw, list):
                raise ValueError("participation_levels must be a list")
            participation_levels = [
                _finite_number(v, "participation_levels entries") for v in participation_raw
            ]
        qty_raw = payload.get("qty_levels")
        qty_levels: list[float] | None = None
        if qty_raw is not None:
            if not isinstance(qty_raw, list):
                raise ValueError("qty_levels must be a list")
            qty_levels = [_finite_number(v, "qty_levels entries") for v in qty_raw]
        result = cost_surface_report(
            adv=adv,
            volatility=volatility,
            participation_levels=participation_levels,
            qty_levels=qty_levels,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P312 – liquidity-adjusted returns endpoint
# ---------------------------------------------------------------------------


@router.post("/liquidity-adjusted-returns", dependencies=[Depends(require_api_key())])
def liquidity_adjusted_returns_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    from app.platform.liquidity_adjusted_returns import liquidity_adjusted_returns_report

    try:
        method = str(payload.get("method", "amihud"))
        result = liquidity_adjusted_returns_report(
            _numeric_series(payload, field="returns"),
            _numeric_series(payload, field="volumes"),
            method=method,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P317 — Concept drift detection (rolling-window mean-shift)
# ---------------------------------------------------------------------------


@router.post("/concept-drift", dependencies=[Depends(require_api_key())])
def concept_drift_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P317: detect concept drift via non-overlapping window mean-shift scoring.

    Body: ``{"series": [...], "window"?: int, "threshold"?: float}`` where
    ``window`` (default 20) is the non-overlapping window size and
    ``threshold`` (default 2.0) is the z-score threshold for flagging drift.
    Returns ``drift_points`` (list of indices in drifting windows) and
    per-bar ``drift_scores``. HTTP 422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        window_raw = payload.get("window", 20)
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int >= 2")
        threshold_raw = payload.get("threshold", 2.0)
        threshold = _finite_number(threshold_raw, "threshold")
        result = concept_drift_report(series, window=window_raw, threshold=threshold)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P318 — Multi-timeframe coherence
# ---------------------------------------------------------------------------


@router.post("/multitimeframe-coherence", dependencies=[Depends(require_api_key())])
def multitimeframe_coherence_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P318: compute coherence across multi-timeframe signal vectors.

    Body: ``{"signals": {tf: [floats]}, "weights"?: {tf: float}}`` where
    ``signals`` is a non-empty dict of equal-length signal series keyed by
    timeframe name and ``weights`` (optional) assigns a weight per timeframe
    (uniform when omitted). Returns per-bar ``coherence_scores`` and a scalar
    ``agreement_ratio`` in ``[0, 1]``. HTTP 422 on invalid input.
    """
    try:
        signals = _panel_field(payload, field="signals")
        weights_raw = payload.get("weights")
        weights: dict[str, float] | None = None
        if weights_raw is not None:
            if not isinstance(weights_raw, dict) or not weights_raw:
                raise ValueError("weights must be a non-empty dict")
            weights = {str(k): _finite_number(v, f"weights['{k}']") for k, v in weights_raw.items()}
        result = multitimeframe_coherence_report(signals, weights=weights)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P319 — Feature extraction
# ---------------------------------------------------------------------------


@router.post("/feature-extraction", dependencies=[Depends(require_api_key())])
def feature_extraction_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P319: extract statistical features from a single numeric series.

    Body: ``{"series": [...]}`` where ``series`` is a non-empty list of
    finite numbers (length >= 2). Returns ``features`` dict with mean, std,
    skew, kurt, min, max, range, autocorr_lag1, trend_slope,
    volatility_clustering, and max_drawdown. HTTP 422 on invalid input.
    """
    try:
        series = _numeric_series(payload)
        if len(series) < 2:
            raise ValueError("series must contain at least 2 values")
        result = feature_extraction_report(series)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P320 — Factor momentum
# ---------------------------------------------------------------------------


@router.post("/factor-momentum", dependencies=[Depends(require_api_key())])
def factor_momentum_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P320: compute factor momentum ranking from a panel of factor returns.

    Body: ``{"factor_returns": {name: [floats]}, "lookback"?: int}`` where
    ``factor_returns`` is a non-empty dict of equal-length factor return
    series and ``lookback`` (default 12) is the trailing window size.
    Returns per-factor ``momentum``, descending ``ranking``, and
    ``long_short_signal``. HTTP 422 on invalid input.
    """
    try:
        factor_returns = _panel_field(payload, field="factor_returns")
        lookback_raw = payload.get("lookback", 12)
        if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, int):
            raise ValueError("lookback must be an int >= 2")
        result = factor_momentum_report(factor_returns, lookback=lookback_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P332 — reverse stress test endpoint
# ---------------------------------------------------------------------------


@router.post("/reverse-stress", dependencies=[Depends(require_api_key())])
def reverse_stress_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P332: reverse stress test — find the smallest shock that breaches a loss threshold.

    Body: ``{"positions": {symbol: notional}, "betas": {symbol: beta},
    "loss_threshold": float, "scenarios"?: [{name, market_return}]}``.
    Returns critical scenario name, multiplier (>1 safe, <1 breached),
    and per-scenario details. HTTP 422 on invalid input.
    """
    from app.platform.reverse_stress import reverse_stress_report

    positions_raw = payload.get("positions")
    if not isinstance(positions_raw, dict) or not positions_raw:
        raise HTTPException(status_code=422, detail="positions must be a non-empty dict")
    betas_raw = payload.get("betas") or {}
    if not isinstance(betas_raw, dict):
        raise HTTPException(status_code=422, detail="betas must be a dict")
    if "loss_threshold" not in payload:
        raise HTTPException(status_code=422, detail="loss_threshold is required")
    try:
        positions = {str(k): _finite_number(v, f"positions['{k}']") for k, v in positions_raw.items()}
        betas = {str(k): _finite_number(v, f"betas['{k}']") for k, v in betas_raw.items()}
        loss_threshold = _finite_number(payload["loss_threshold"], "loss_threshold")
        scenarios = payload.get("scenarios")
        if scenarios is not None and not isinstance(scenarios, list):
            raise ValueError("scenarios must be a list")
        result = reverse_stress_report(positions, betas, loss_threshold, scenarios=scenarios)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P333 — dynamic style analysis endpoint
# ---------------------------------------------------------------------------


@router.post("/dynamic-style-analysis", dependencies=[Depends(require_api_key())])
def dynamic_style_analysis_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P333: rolling-window dynamic style analysis.

    Body: ``{"returns": [...], "factor_returns": {name: [...]},
    "window"?: int, "constraint"?: "sum_eq_one"|"sum_le_one"|"none"}``.
    Returns per-window weights, R² series, style drift score, and drift flag.
    HTTP 422 on invalid input.
    """
    from app.platform.dynamic_style import dynamic_style_analysis_report

    returns_raw = payload.get("returns")
    if not isinstance(returns_raw, list) or not returns_raw:
        raise HTTPException(status_code=422, detail="returns must be a non-empty list")
    fr_raw = payload.get("factor_returns")
    if not isinstance(fr_raw, dict) or not fr_raw:
        raise HTTPException(status_code=422, detail="factor_returns must be a non-empty dict")
    try:
        returns = [_finite_number(v, "returns entries") for v in returns_raw]
        factor_returns = {}
        for name, series in fr_raw.items():
            if not isinstance(series, list) or not series:
                raise ValueError(f"factor_returns['{name}'] must be a non-empty list")
            factor_returns[str(name)] = [_finite_number(v, f"factor_returns['{name}'] entries") for v in series]
        window_raw = payload.get("window", 20)
        if isinstance(window_raw, bool) or not isinstance(window_raw, int):
            raise ValueError("window must be an int")
        constraint = str(payload.get("constraint", "sum_eq_one"))
        result = dynamic_style_analysis_report(returns, factor_returns, window=window_raw, constraint=constraint)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P334 — online covariance endpoint
# ---------------------------------------------------------------------------


@router.post("/online-covariance", dependencies=[Depends(require_api_key())])
def online_covariance_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P334: online EWMA covariance estimation.

    Body: ``{"returns_panel": {asset: [...]}, "lam"?: 0.97,
    "min_window"?: int}`` where ``returns_panel`` is a non-empty dict of
    equal-length return series (≤50 assets, ≥ ``min_window`` obs),
    ``lam`` is the EWMA decay in ``(0, 1]``, and ``min_window`` (default 30)
    is the initial sample-covariance window. Returns the latest covariance
    matrix (nested dict), condition number, eigenvalues, and asset list.
    HTTP 422 on invalid input.
    """
    from app.platform.online_covariance import online_covariance_report

    rp_raw = payload.get("returns_panel")
    if not isinstance(rp_raw, dict) or not rp_raw:
        raise HTTPException(status_code=422, detail="returns_panel must be a non-empty dict")
    if len(rp_raw) > 50:
        raise HTTPException(status_code=422, detail="returns_panel must contain at most 50 assets")
    try:
        returns_panel: dict[str, list[float]] = {}
        for name, series in rp_raw.items():
            if not isinstance(series, list) or not series:
                raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
            returns_panel[str(name)] = [_finite_number(v, f"returns_panel['{name}'] entries") for v in series]
        lam = _finite_number(payload.get("lam", 0.97), "lam")
        min_window_raw = payload.get("min_window", 30)
        if isinstance(min_window_raw, bool) or not isinstance(min_window_raw, int):
            raise ValueError("min_window must be an int")
        result = online_covariance_report(returns_panel, lam=lam, min_window=min_window_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P329 — Correlation network (MST from returns panel)
# ---------------------------------------------------------------------------


@router.post("/correlation-network", dependencies=[Depends(require_api_key())])
def correlation_network_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P329: build MST correlation network from a returns panel.

    Body: ``{"returns_panel": {asset: [floats]}, "method"?: "pearson"|"spearman"}``
    where ``returns_panel`` is a non-empty dict of equal-length return series
    (2–50 assets) and ``method`` defaults to ``"pearson"``. Returns MST edges,
    per-node degrees, and average distance. HTTP 422 on invalid input.
    """
    from app.platform.correlation_network import correlation_network_report

    try:
        returns_panel = _panel_field(payload, field="returns_panel")
        method = str(payload.get("method", "pearson"))
        result = correlation_network_report(returns_panel, method=method)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P330 — HAC (Newey-West) statistics
# ---------------------------------------------------------------------------


@router.post("/hac-statistics", dependencies=[Depends(require_api_key())])
def hac_statistics_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P330: OLS regression with Newey-West HAC standard errors.

    Body: ``{"y": [floats], "x": [[floats], ...], "lags"?: int}`` where
    ``y`` is the dependent variable series, ``x`` is a list of regressor
    series (each same length as ``y``, at least one regressor), and ``lags``
    (default 5) is the Newey-West lag truncation parameter. Returns
    coefficients, HAC/OLS standard errors, t-stats, and p-values. HTTP 422
    on invalid input.
    """
    from app.platform.hac_statistics import hac_statistics_report

    try:
        y = _numeric_series(payload, field="y")
        x_raw = payload.get("x")
        if not isinstance(x_raw, list) or not x_raw:
            raise ValueError("x must be a non-empty list of regressor series")
        x: list[list[float]] = []
        for idx, xi in enumerate(x_raw):
            if not isinstance(xi, list):
                raise ValueError(f"x[{idx}] must be a list of finite numbers")
            x.append([_finite_number(v, f"x[{idx}] entries") for v in xi])
        lags_raw = payload.get("lags", 5)
        if isinstance(lags_raw, bool) or not isinstance(lags_raw, int):
            raise ValueError("lags must be an int >= 0")
        result = hac_statistics_report(y, x, lags=lags_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P331 — Adjusted Sharpe ratio
# ---------------------------------------------------------------------------


@router.post("/adjusted-sharpe", dependencies=[Depends(require_api_key())])
def adjusted_sharpe_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P331: raw and adjusted Sharpe ratios (autocorrelation + moments).

    Body: ``{"returns": [floats], "periods_per_year"?: int,
    "max_lag"?: int}`` where ``returns`` is a non-empty list of finite
    period returns, ``periods_per_year`` (default 252) is the annualization
    factor, and ``max_lag`` (default 10) bounds the autocorrelation
    adjustment window. Returns raw Sharpe, autocorrelation-adjusted,
    moments-adjusted (Harvey-Siddique), autocorrelations, skewness, and
    kurtosis. HTTP 422 on invalid input.
    """
    from app.platform.adjusted_sharpe import adjusted_sharpe_report

    try:
        returns = _numeric_series(payload, field="returns")
        periods_per_year_raw = payload.get("periods_per_year", 252)
        max_lag_raw = payload.get("max_lag", 10)
        if isinstance(periods_per_year_raw, bool) or not isinstance(periods_per_year_raw, int):
            raise ValueError("periods_per_year must be an int >= 1")
        if isinstance(max_lag_raw, bool) or not isinstance(max_lag_raw, int):
            raise ValueError("max_lag must be an int >= 1")
        result = adjusted_sharpe_report(returns, periods_per_year=periods_per_year_raw, max_lag=max_lag_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P335 — Multi-Strategy Risk Report endpoint
# ---------------------------------------------------------------------------


@router.post("/multi-strategy-risk", dependencies=[Depends(require_api_key())])
def multi_strategy_risk_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P335: portfolio volatility and risk decomposition across strategies.

    Body: ``{"strategy_returns": {name: [float]}, "weights": {name: float},
    "periods_per_year"?: 252}``. Returns portfolio_vol (annualized),
    risk_contributions (summing to portfolio_vol), diversification_ratio,
    concentration_hhi, and covariance_matrix. HTTP 422 on invalid input.
    """
    from app.platform.multi_strategy_risk import multi_strategy_risk_report

    sr_raw = payload.get("strategy_returns")
    if not isinstance(sr_raw, dict) or not sr_raw:
        raise HTTPException(status_code=422, detail="strategy_returns must be a non-empty dict")
    w_raw = payload.get("weights")
    if not isinstance(w_raw, dict) or not w_raw:
        raise HTTPException(status_code=422, detail="weights must be a non-empty dict")
    try:
        strategy_returns: dict[str, list[float]] = {}
        for name, series in sr_raw.items():
            if not isinstance(series, list) or not series:
                raise ValueError(f"strategy_returns['{name}'] must be a non-empty list")
            strategy_returns[str(name)] = [_finite_number(v, f"strategy_returns['{name}'] entries") for v in series]
        weights = {str(k): _finite_number(v, f"weights['{k}']") for k, v in w_raw.items()}
        periods_per_year_raw = payload.get("periods_per_year", 252)
        if isinstance(periods_per_year_raw, bool) or not isinstance(periods_per_year_raw, int):
            raise ValueError("periods_per_year must be an int >= 1")
        result = multi_strategy_risk_report(strategy_returns, weights, periods_per_year=periods_per_year_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P336 — Volatility-of-Volatility Report endpoint
# ---------------------------------------------------------------------------


@router.post("/vol-of-vol", dependencies=[Depends(require_api_key())])
def vol_of_vol_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P336: volatility-of-volatility diagnostics across multiple rolling windows.

    Body: ``{"returns": [...], "windows"?: [int], "periods_per_year"?: 252}``.
    Returns per-window VoV, mean realized vol, annualized VoV,
    vov_term_structure_slope, and autocorr_lag1. HTTP 422 on invalid input.
    """
    from app.platform.vol_of_vol import vol_of_vol_report

    try:
        returns = _numeric_series(payload, field="returns")
        windows_raw = payload.get("windows")
        windows: list[int] | None = None
        if windows_raw is not None:
            if not isinstance(windows_raw, list):
                raise ValueError("windows must be a list of ints")
            windows = []
            for idx, w in enumerate(windows_raw):
                if isinstance(w, bool) or not isinstance(w, int):
                    raise ValueError(f"windows[{idx}] must be an int >= 2")
                windows.append(w)
        periods_per_year_raw = payload.get("periods_per_year", 252)
        if isinstance(periods_per_year_raw, bool) or not isinstance(periods_per_year_raw, int):
            raise ValueError("periods_per_year must be an int >= 1")
        result = vol_of_vol_report(returns, windows=windows, periods_per_year=periods_per_year_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P337 — Regime-Conditional Cointegration endpoint
# ---------------------------------------------------------------------------


@router.post("/regime-cointegration", dependencies=[Depends(require_api_key())])
def regime_cointegration_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P337: per-regime OLS hedge ratios and residual mean-reversion diagnostics.

    Body: ``{"y": [...], "x": [...], "regime_labels": [...],
    "min_regime_samples"?: 10}``. Returns per_regime hedge_ratio, half_life,
    residual_autocorr, n_samples, sufficient, plus stability_score and
    breakdown_regimes. HTTP 422 on invalid input.
    """
    from app.platform.regime_cointegration import regime_cointegration_report

    y_raw = payload.get("y")
    x_raw = payload.get("x")
    regimes_raw = payload.get("regime_labels")
    if not isinstance(y_raw, list) or not isinstance(x_raw, list):
        raise HTTPException(status_code=422, detail="y and x must be lists")
    if not isinstance(regimes_raw, list):
        raise HTTPException(status_code=422, detail="regime_labels must be a list")
    try:
        y = [_finite_number(v, "y entries") for v in y_raw]
        x = [_finite_number(v, "x entries") for v in x_raw]
        regimes = [str(v) for v in regimes_raw]
        min_regime_raw = payload.get("min_regime_samples", 10)
        if isinstance(min_regime_raw, bool) or not isinstance(min_regime_raw, int):
            raise ValueError("min_regime_samples must be an int >= 1")
        result = regime_cointegration_report(y, x, regimes, min_regime_samples=min_regime_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()


# ---------------------------------------------------------------------------
# P338 — Turnover Frontier endpoint
# ---------------------------------------------------------------------------


@router.post("/turnover-frontier", dependencies=[Depends(require_api_key())])
def turnover_frontier_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """P338: net Sharpe ratio frontier across different portfolio turnover rates.

    Body: ``{"returns_panel": {asset: [...]}, "turnover_rates"?: [float],
    "cost_per_turnover"?: 0.001, "periods_per_year"?: 252}``.
    Returns frontier (list of {turnover, gross_sharpe, net_sharpe, cost_drag}),
    breakeven_turnover, and optimal_turnover. HTTP 422 on invalid input.
    """
    from app.platform.turnover_frontier import turnover_frontier_report

    rp_raw = payload.get("returns_panel")
    if not isinstance(rp_raw, dict) or not rp_raw:
        raise HTTPException(status_code=422, detail="returns_panel must be a non-empty dict")
    if len(rp_raw) > 50:
        raise HTTPException(status_code=422, detail="returns_panel must contain at most 50 assets")
    try:
        returns_panel: dict[str, list[float]] = {}
        for name, series in rp_raw.items():
            if not isinstance(series, list) or not series:
                raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
            returns_panel[str(name)] = [_finite_number(v, f"returns_panel['{name}'] entries") for v in series]
        turnover_rates_raw = payload.get("turnover_rates")
        turnover_rates: list[float] | None = None
        if turnover_rates_raw is not None:
            if not isinstance(turnover_rates_raw, list):
                raise ValueError("turnover_rates must be a list of non-negative floats")
            turnover_rates = [_finite_number(v, "turnover_rates entries") for v in turnover_rates_raw]
        cost = _finite_number(payload.get("cost_per_turnover", 0.001), "cost_per_turnover")
        periods_per_year_raw = payload.get("periods_per_year", 252)
        if isinstance(periods_per_year_raw, bool) or not isinstance(periods_per_year_raw, int):
            raise ValueError("periods_per_year must be an int >= 1")
        result = turnover_frontier_report(
            returns_panel,
            turnover_rates=turnover_rates,
            cost_per_turnover=cost,
            periods_per_year=periods_per_year_raw,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return result.to_dict()
