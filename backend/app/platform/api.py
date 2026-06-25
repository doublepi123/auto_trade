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
