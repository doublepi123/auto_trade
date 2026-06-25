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
