from __future__ import annotations

import csv
import io
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.auth import require_api_key
from app.database import get_db
from app.core.backtest import (
    BacktestBar,
    BacktestEngine,
    BacktestEngineParams,
    BacktestMetrics as EngineMetrics,
    SweepHeatmap as EngineSweepHeatmap,
    SweepResultRow as EngineSweepRow,
    WalkForwardResult as EngineWalkForwardResult,
    expand_numeric_range,
    parse_backtest_csv,
    stress_test,
    sweep_backtest,
    walk_forward_backtest,
)
from app.schemas import (
    BacktestEquityPoint,
    BacktestExportRequest,
    BacktestFeeSensitivityPoint,
    BacktestMetrics,
    BacktestParams,
    BacktestPricePoint,
    BacktestResult,
    BacktestRunRequest,
    BacktestSkippedSignal,
    BacktestSweepHeatmap,
    BacktestSweepHeatmapCell,
    BacktestSweepRequest,
    BacktestSweepResult,
    BacktestSweepRow,
    BacktestTradeLog,
    StrategyExperimentGridItem,
    WalkForwardRequest,
    WalkForwardResultOut,
    WalkForwardWindowOut,
    WalkForwardSummaryOut,
    StressTestRequest,
    StressTestResult,
    BacktestRunSaveRequest,
    BacktestRunOut,
    BacktestRunPage,
    BacktestRunCompare,
)
from app.services.backtest_run_service import BacktestRunService

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.post("/run", response_model=BacktestResult, dependencies=[Depends(require_api_key())])
def run_backtest(payload: BacktestRunRequest) -> BacktestResult:
    try:
        bars = _load_bars(payload.csv_text, payload.price_points)
        if not bars:
            raise HTTPException(status_code=422, detail="at least one price bar is required")
        result = BacktestEngine(_params_to_engine(payload.params)).run(bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return BacktestResult(
        params=payload.params,
        metrics=_metrics_to_schema(result.metrics),
        equity_curve=[
            BacktestEquityPoint(
                timestamp=point.timestamp,
                close=point.close,
                equity=point.equity,
                realized_pnl=point.realized_pnl,
                unrealized_pnl=point.unrealized_pnl,
                drawdown_pct=point.drawdown_pct,
                position=point.position,
            )
            for point in result.equity_curve
        ],
        trades=[
            BacktestTradeLog(
                timestamp=trade.timestamp,
                action=trade.action,
                price=trade.price,
                quantity=trade.quantity,
                fee=trade.fee,
                pnl=trade.pnl,
                state_after=trade.state_after,
                reason=trade.reason,
                holding_minutes=trade.holding_minutes,
            )
            for trade in result.trades
        ],
        skipped_signals=[
            BacktestSkippedSignal(
                timestamp=signal.timestamp,
                action=signal.action,
                price=signal.price,
                reason=signal.reason,
                state=signal.state,
                category=signal.category,
            )
            for signal in result.skipped_signals
        ],
        fee_sensitivity=[
            BacktestFeeSensitivityPoint(
                fee_rate=point.fee_rate,
                total_pnl=point.total_pnl,
                total_return_pct=point.total_return_pct,
                max_drawdown_pct=point.max_drawdown_pct,
            )
            for point in result.fee_sensitivity
        ],
    )


@router.post(
    "/sweep",
    response_model=BacktestSweepResult,
    dependencies=[Depends(require_api_key())],
)
def sweep_backtest_endpoint(payload: BacktestSweepRequest) -> BacktestSweepResult:
    """Synchronous parameter sweep: run every grid combination through
    BacktestEngine and return them ranked by ``sort_by`` plus a best-per-cell
    heatmap. Read-only analysis (no audit), like ``/run``."""
    try:
        bars = _load_bars(payload.csv_text, payload.price_points)
        if not bars:
            raise HTTPException(status_code=422, detail="at least one price bar is required")
        base = _params_to_engine(payload.base)
        grid = {key: _expand_grid_item(item) for key, item in payload.grid.items()}
        result = sweep_backtest(
            base,
            grid,
            bars,
            sort_by=payload.sort_by,
            max_combinations=payload.max_combinations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return BacktestSweepResult(
        rows=[_sweep_row_to_schema(row) for row in result.rows],
        best=_sweep_row_to_schema(result.best) if result.best is not None else None,
        heatmap=_sweep_heatmap_to_schema(result.heatmap),
        evaluated_count=result.evaluated_count,
        skipped_count=result.skipped_count,
        sort_by=result.sort_by,
    )


@router.post(
    "/walk-forward",
    response_model=WalkForwardResultOut,
    dependencies=[Depends(require_api_key())],
)
def walk_forward_backtest_endpoint(payload: WalkForwardRequest) -> WalkForwardResultOut:
    """Walk-forward rolling-window evaluation: optimize params on each train
    window, evaluate out-of-sample on the next test window. Empty grid =
    rolling evaluation of ``base`` (consistency only). Read-only, no audit."""
    try:
        bars = _load_bars(payload.csv_text, payload.price_points)
        if not bars:
            raise HTTPException(status_code=422, detail="at least one price bar is required")
        base = _params_to_engine(payload.base)
        grid = {key: _expand_grid_item(item) for key, item in payload.grid.items()}
        result = walk_forward_backtest(
            base,
            grid,
            bars,
            train_size=payload.train_size,
            test_size=payload.test_size,
            step=payload.step,
            sort_by=payload.sort_by,
            max_combinations=payload.max_combinations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _walk_forward_result_to_schema(result)


@router.post(
    "/stress",
    response_model=StressTestResult,
    dependencies=[Depends(require_api_key())],
)
def stress_test_endpoint(payload: StressTestRequest) -> StressTestResult:
    """What-If stress ensemble: re-run the engine over N deterministically
    jittered price paths and report the return distribution. Read-only, no audit."""
    try:
        bars = _load_bars(payload.csv_text, payload.price_points)
        if not bars:
            raise HTTPException(status_code=422, detail="at least one price bar is required")
        base = _params_to_engine(payload.base)
        result = stress_test(
            base,
            bars,
            scenarios=payload.scenarios,
            jitter_pct=payload.jitter_pct,
            seed=payload.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return StressTestResult(**asdict(result))


# ---------------------------------------------------------------------------
# Export result as CSV
# ---------------------------------------------------------------------------


@router.post("/export", dependencies=[Depends(require_api_key())])
def export_backtest_result(payload: BacktestExportRequest) -> Response:
    """Export a backtest result as a multi-section CSV file.

    The CSV contains comment/header rows for params and metrics, followed by
    dedicated sections for trades, equity curve, skipped signals and fee
    sensitivity. This keeps everything in one file while remaining readable by
    spreadsheets and analysis tools.
    """
    result = payload.result
    sections = set(payload.sections)
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["# Backtest Result Export"])

    if "params" in sections:
        writer.writerow(["# params"])
        for key, value in result.params.model_dump().items():
            writer.writerow([key, value])
        writer.writerow(["# metrics"])
        for key, value in result.metrics.model_dump().items():
            writer.writerow([key, value])

    if "trades" in sections and result.trades:
        writer.writerow([])
        writer.writerow(["trades"])
        writer.writerow([
            "timestamp", "action", "price", "quantity", "fee", "pnl",
            "state_after", "reason", "holding_minutes",
        ])
        for trade in result.trades:
            writer.writerow([
                trade.timestamp.isoformat(),
                trade.action,
                trade.price,
                trade.quantity,
                trade.fee,
                trade.pnl,
                trade.state_after,
                trade.reason,
                trade.holding_minutes,
            ])

    if "equity_curve" in sections and result.equity_curve:
        writer.writerow([])
        writer.writerow(["equity_curve"])
        writer.writerow([
            "timestamp", "close", "equity", "realized_pnl", "unrealized_pnl",
            "drawdown_pct", "position",
        ])
        for point in result.equity_curve:
            writer.writerow([
                point.timestamp.isoformat(),
                point.close,
                point.equity,
                point.realized_pnl,
                point.unrealized_pnl,
                point.drawdown_pct,
                point.position,
            ])

    if "skipped_signals" in sections and result.skipped_signals:
        writer.writerow([])
        writer.writerow(["skipped_signals"])
        writer.writerow([
            "timestamp", "action", "price", "reason", "state", "category",
        ])
        for signal in result.skipped_signals:
            writer.writerow([
                signal.timestamp.isoformat(),
                signal.action,
                signal.price,
                signal.reason,
                signal.state,
                signal.category,
            ])

    if "fee_sensitivity" in sections and result.fee_sensitivity:
        writer.writerow([])
        writer.writerow(["fee_sensitivity"])
        writer.writerow(["fee_rate", "total_pnl", "total_return_pct", "max_drawdown_pct"])
        for point in result.fee_sensitivity:
            writer.writerow([
                point.fee_rate,
                point.total_pnl,
                point.total_return_pct,
                point.max_drawdown_pct,
            ])

    symbol = result.params.symbol.strip().upper() or "UNKNOWN"
    safe_symbol = symbol.replace(".", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{safe_symbol}_{timestamp}.csv"
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Saved runs (comparison)
# ---------------------------------------------------------------------------


@router.post("/runs", response_model=BacktestRunOut, dependencies=[Depends(require_api_key())])
def save_backtest_run(
    payload: BacktestRunSaveRequest,
    db=Depends(get_db),
) -> BacktestRunOut:
    return BacktestRunService(db).save(payload)


@router.get("/runs", response_model=BacktestRunPage, dependencies=[Depends(require_api_key())])
def list_backtest_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
) -> BacktestRunPage:
    return BacktestRunService(db).list_runs(page=page, page_size=page_size)


@router.get(
    "/runs/compare",
    response_model=BacktestRunCompare,
    dependencies=[Depends(require_api_key())],
)
def compare_backtest_runs(
    ids: list[int] = Query(..., min_length=1, max_length=8),
    db=Depends(get_db),
) -> BacktestRunCompare:
    return BacktestRunCompare(runs=BacktestRunService(db).compare(ids))


@router.get("/runs/{run_id}", response_model=BacktestRunOut, dependencies=[Depends(require_api_key())])
def get_backtest_run(run_id: int, db=Depends(get_db)) -> BacktestRunOut:
    out = BacktestRunService(db).get(run_id)
    if out is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return out


@router.delete("/runs/{run_id}", status_code=204, dependencies=[Depends(require_api_key())])
def delete_backtest_run(run_id: int, db=Depends(get_db)) -> Response:
    BacktestRunService(db).delete(run_id)
    return Response(status_code=204)


def _load_bars(csv_text: str | None, price_points: list[BacktestPricePoint]) -> list[BacktestBar]:
    if csv_text and csv_text.strip():
        return parse_backtest_csv(csv_text)
    return [
        BacktestBar(
            timestamp=point.timestamp,
            open=point.open,
            high=point.high,
            low=point.low,
            close=point.close,
            volume=point.volume,
        )
        for point in price_points
    ]


def _params_to_engine(p: BacktestParams) -> BacktestEngineParams:
    return BacktestEngineParams(
        symbol=p.symbol,
        buy_low=p.buy_low,
        sell_high=p.sell_high,
        short_selling=p.short_selling,
        min_profit_amount=p.min_profit_amount,
        max_daily_loss=p.max_daily_loss,
        max_consecutive_losses=p.max_consecutive_losses,
        quantity=p.quantity,
        initial_cash=p.initial_cash,
        fee_rate=p.fee_rate,
        fixed_fee=p.fixed_fee,
        slippage_pct=p.slippage_pct,
        stop_loss_pct=p.stop_loss_pct,
    )


def _metrics_to_schema(m: EngineMetrics) -> BacktestMetrics:
    """Map the engine metrics dataclass to the API schema, surfacing the
    risk-adjusted ratios the engine already computes (sharpe/sortino/calmar/
    profit_factor/profit_loss_ratio)."""
    return BacktestMetrics(
        initial_cash=m.initial_cash,
        final_equity=m.final_equity,
        total_pnl=m.total_pnl,
        total_return_pct=m.total_return_pct,
        max_drawdown_pct=m.max_drawdown_pct,
        trade_count=m.trade_count,
        closed_trade_count=m.closed_trade_count,
        winning_trades=m.winning_trades,
        losing_trades=m.losing_trades,
        win_rate=m.win_rate,
        avg_holding_minutes=m.avg_holding_minutes,
        fees_paid=m.fees_paid,
        skipped_signals=m.skipped_signals,
        final_state=m.final_state,
        sharpe_ratio=m.sharpe_ratio,
        sortino_ratio=m.sortino_ratio,
        calmar_ratio=m.calmar_ratio,
        profit_factor=m.profit_factor,
        profit_loss_ratio=m.profit_loss_ratio,
    )


def _expand_grid_item(item: StrategyExperimentGridItem) -> list[float]:
    """Expand one sweep grid axis to its candidate values. Reuses the core
    ``expand_numeric_range`` (which mirrors ExperimentGridService._expand_item)
    so this module does not depend on the pydantic-bound grid service."""
    if item.value is not None:
        return [float(item.value)]
    if item.values is not None:
        return [float(v) for v in item.values]
    r = item.range
    if r is None:
        return []
    return expand_numeric_range(r.start, r.step, r.end)


def _sweep_row_to_schema(row: EngineSweepRow) -> BacktestSweepRow:
    # asdict carries the raw engine params (may exceed BacktestParams display
    # bounds for an exploratory axis); see BacktestSweepRow.params docstring.
    return BacktestSweepRow(
        params=asdict(row.params),
        metrics=_metrics_to_schema(row.metrics),
        rank=row.rank,
    )


def _sweep_heatmap_to_schema(h: EngineSweepHeatmap) -> BacktestSweepHeatmap:
    return BacktestSweepHeatmap(
        x_axis=h.x_axis,
        y_axis=h.y_axis,
        z_metric=h.z_metric,
        cells=[
            BacktestSweepHeatmapCell(buy_low=c.buy_low, sell_high=c.sell_high, value=c.value)
            for c in h.cells
        ],
    )


def _walk_forward_result_to_schema(result: EngineWalkForwardResult) -> WalkForwardResultOut:
    return WalkForwardResultOut(
        windows=[
            WalkForwardWindowOut(
                index=w.index,
                start=w.start,
                end=w.end,
                train_size=w.train_size,
                test_size=w.test_size,
                best_params=(asdict(w.best_params) if w.best_params is not None else None),
                test_metrics=(_metrics_to_schema(w.test_metrics) if w.test_metrics is not None else None),
            )
            for w in result.windows
        ],
        summary=WalkForwardSummaryOut(
            window_count=result.summary.window_count,
            evaluated_window_count=result.summary.evaluated_window_count,
            mean_test_return_pct=result.summary.mean_test_return_pct,
            median_test_return_pct=result.summary.median_test_return_pct,
            mean_test_metric=result.summary.mean_test_metric,
            profitable_window_pct=result.summary.profitable_window_pct,
            test_return_std_pct=result.summary.test_return_std_pct,
        ),
        sort_by=result.sort_by,
        train_size=result.train_size,
        test_size=result.test_size,
        step=result.step,
    )
