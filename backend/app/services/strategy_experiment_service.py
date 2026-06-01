from __future__ import annotations

import json
import logging
from typing import Any
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.backtest import (
    BacktestBar,
    BacktestEngine,
    BacktestEngineParams,
    BacktestResultData,
    parse_backtest_csv,
)
from app.models import StrategyExperiment, StrategyExperimentRun
from app.schemas import (
    BacktestParams,
    StrategyExperimentCreate,
    StrategyExperimentResponse,
    StrategyExperimentRunPage,
    StrategyExperimentRunRequest,
    StrategyExperimentRunResponse,
)
from app.services.experiment_grid_service import ExperimentGridService

_ALLOWED_SORT_FIELDS = {
    "total_return_pct",
    "total_pnl",
    "max_drawdown_pct",
    "win_rate",
    "trade_count",
    "created_at",
}
_ALLOWED_ORDERS = {"asc", "desc"}
logger = logging.getLogger(__name__)


class StrategyExperimentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.grid = ExperimentGridService()

    # ── create ──────────────────────────────────────────────────────────

    def create_experiment(
        self, request: StrategyExperimentCreate
    ) -> StrategyExperimentResponse:
        estimated_runs = self.grid.estimate_count(request)
        base_params_json = json.dumps(
            request.base_params.model_dump(), ensure_ascii=False, separators=(",", ":")
        )
        parameter_grid_json = json.dumps(
            _serializable_grid(request.parameter_grid),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        exp = StrategyExperiment(
            name=request.name,
            symbol=request.symbol,
            base_params_json=base_params_json,
            parameter_grid_json=parameter_grid_json,
            status="PENDING",
            estimated_runs=estimated_runs,
            completed_runs=0,
            failed_runs=0,
            error="",
        )
        self.db.add(exp)
        self.db.commit()
        self.db.refresh(exp)
        return StrategyExperimentResponse.model_validate(exp)

    # ── list / get ──────────────────────────────────────────────────────

    def list_experiments(self) -> list[StrategyExperimentResponse]:
        exps = (
            self.db.query(StrategyExperiment)
            .order_by(
                StrategyExperiment.created_at.desc(), StrategyExperiment.id.desc()
            )
            .all()
        )
        return [StrategyExperimentResponse.model_validate(e) for e in exps]

    def get_experiment(self, experiment_id: int) -> StrategyExperimentResponse:
        exp = (
            self.db.query(StrategyExperiment)
            .filter(StrategyExperiment.id == experiment_id)
            .first()
        )
        if exp is None:
            raise ValueError("strategy experiment not found")
        return StrategyExperimentResponse.model_validate(exp)

    # ── run ─────────────────────────────────────────────────────────────

    def run_experiment(
        self,
        experiment_id: int,
        request: StrategyExperimentRunRequest,
    ) -> StrategyExperimentResponse:
        exp = (
            self.db.query(StrategyExperiment)
            .filter(StrategyExperiment.id == experiment_id)
            .first()
        )
        if exp is None:
            raise ValueError("strategy experiment not found")

        # Reconstruct the creation request from stored JSON.
        base_params = BacktestParams.model_validate(
            json.loads(exp.base_params_json)
        )
        grid_raw = json.loads(exp.parameter_grid_json)
        parameter_grid = _deserialize_grid(grid_raw)

        create_req = StrategyExperimentCreate(
            name=exp.name,
            symbol=exp.symbol,
            base_params=base_params,
            parameter_grid=parameter_grid,
        )
        param_combos = self.grid.expand(create_req)

        # Load price bars.
        bars = _load_bars(request)
        if not bars:
            raise ValueError("price data is required")

        # Delete previous runs for this experiment.
        self.db.query(StrategyExperimentRun).filter(
            StrategyExperimentRun.experiment_id == experiment_id
        ).delete()
        self.db.flush()

        completed = 0
        failed = 0

        for params in param_combos:
            engine_params = BacktestEngineParams(
                symbol=params.symbol,
                buy_low=params.buy_low,
                sell_high=params.sell_high,
                short_selling=params.short_selling,
                min_profit_amount=params.min_profit_amount,
                max_daily_loss=params.max_daily_loss,
                max_consecutive_losses=params.max_consecutive_losses,
                quantity=params.quantity,
                initial_cash=params.initial_cash,
                fee_rate=params.fee_rate,
                fixed_fee=params.fixed_fee,
                slippage_pct=params.slippage_pct,
                stop_loss_pct=params.stop_loss_pct,
            )
            parameters_json = json.dumps(
                params.model_dump(), ensure_ascii=False, separators=(",", ":")
            )
            try:
                result: BacktestResultData = BacktestEngine(engine_params).run(bars)
                run = StrategyExperimentRun(
                    experiment_id=experiment_id,
                    parameters_json=parameters_json,
                    status="COMPLETED",
                    total_pnl=result.metrics.total_pnl,
                    total_return_pct=result.metrics.total_return_pct,
                    max_drawdown_pct=result.metrics.max_drawdown_pct,
                    win_rate=result.metrics.win_rate,
                    trade_count=result.metrics.trade_count,
                    closed_trade_count=result.metrics.closed_trade_count,
                    result_summary_json=json.dumps(
                        _build_result_summary(result),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    error="",
                )
                self.db.add(run)
                completed += 1
            except Exception as exc:
                logger.exception(
                    "Strategy experiment run failed",
                    extra={
                        "experiment_id": experiment_id,
                        "symbol": exp.symbol,
                        "parameters_json": parameters_json,
                    },
                )
                run = StrategyExperimentRun(
                    experiment_id=experiment_id,
                    parameters_json=parameters_json,
                    status="FAILED",
                    total_pnl=0.0,
                    total_return_pct=0.0,
                    max_drawdown_pct=0.0,
                    win_rate=0.0,
                    trade_count=0,
                    closed_trade_count=0,
                    result_summary_json="{}",
                    error=str(exc),
                )
                self.db.add(run)
                failed += 1

        now = datetime.now(timezone.utc)
        if failed == len(param_combos):
            exp.status = "FAILED"
            exp.error = "all runs failed"
        else:
            exp.status = "COMPLETED"
            exp.error = ""
        exp.completed_runs = completed
        exp.failed_runs = failed
        exp.completed_at = now
        self.db.commit()
        self.db.refresh(exp)
        return StrategyExperimentResponse.model_validate(exp)

    # ── list runs ───────────────────────────────────────────────────────

    def list_runs(
        self,
        experiment_id: int,
        sort: str,
        order: str,
        page: int,
        page_size: int,
    ) -> StrategyExperimentRunPage:
        # Ensure experiment exists.
        self.get_experiment(experiment_id)

        if sort not in _ALLOWED_SORT_FIELDS:
            raise ValueError("unsupported sort field")
        if order not in _ALLOWED_ORDERS:
            raise ValueError("unsupported sort order")
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 1
        if page_size > 100:
            page_size = 100

        sort_col = getattr(StrategyExperimentRun, sort)
        if order == "desc":
            sort_col = sort_col.desc()
        else:
            sort_col = sort_col.asc()

        q = (
            self.db.query(StrategyExperimentRun)
            .filter(StrategyExperimentRun.experiment_id == experiment_id)
        )
        total = q.count()
        runs = (
            q.order_by(sort_col, StrategyExperimentRun.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        items = [StrategyExperimentRunResponse.model_validate(r) for r in runs]
        return StrategyExperimentRunPage(
            items=items, page=page, page_size=page_size, total=total
        )

    def get_run(self, experiment_id: int, run_id: int) -> StrategyExperimentRunResponse:
        """Get a single run by experiment and run id."""
        self.get_experiment(experiment_id)
        run = (
            self.db.query(StrategyExperimentRun)
            .filter(StrategyExperimentRun.experiment_id == experiment_id)
            .filter(StrategyExperimentRun.id == run_id)
            .first()
        )
        if run is None:
            raise ValueError("strategy experiment run not found")
        return StrategyExperimentRunResponse.model_validate(run)
    def export_experiment(
        self, experiment_id: int, format: str
    ) -> dict[str, object] | str:
        """Export experiment runs as JSON or CSV."""
        exp = (
            self.db.query(StrategyExperiment)
            .filter(StrategyExperiment.id == experiment_id)
            .first()
        )
        if exp is None:
            raise ValueError("strategy experiment not found")
        runs = (
            self.db.query(StrategyExperimentRun)
            .filter(StrategyExperimentRun.experiment_id == experiment_id)
            .order_by(StrategyExperimentRun.total_return_pct.desc())
            .all()
        )
        if format.lower() == "json":
            return {
                "experiment": {
                    "id": exp.id,
                    "name": exp.name,
                    "symbol": exp.symbol,
                    "status": exp.status,
                    "created_at": exp.created_at.isoformat(),
                },
                "runs": [
                    {
                        "id": r.id,
                        "parameters": json.loads(r.parameters_json),
                        "status": r.status,
                        "total_pnl": r.total_pnl,
                        "total_return_pct": r.total_return_pct,
                        "max_drawdown_pct": r.max_drawdown_pct,
                        "win_rate": r.win_rate,
                        "trade_count": r.trade_count,
                        "closed_trade_count": r.closed_trade_count,
                        "error": r.error,
                    }
                    for r in runs
                ],
            }
        if format.lower() == "csv":
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    "run_id",
                    "symbol",
                    "buy_low",
                    "sell_high",
                    "quantity",
                    "fee_rate",
                    "slippage_pct",
                    "status",
                    "total_pnl",
                    "total_return_pct",
                    "max_drawdown_pct",
                    "win_rate",
                    "trade_count",
                    "closed_trade_count",
                    "error",
                ]
            )
            for r in runs:
                params = json.loads(r.parameters_json)
                writer.writerow(
                    [
                        r.id,
                        params.get("symbol", ""),
                        params.get("buy_low", ""),
                        params.get("sell_high", ""),
                        params.get("quantity", ""),
                        params.get("fee_rate", ""),
                        params.get("slippage_pct", ""),
                        r.status,
                        r.total_pnl,
                        r.total_return_pct,
                        r.max_drawdown_pct,
                        r.win_rate,
                        r.trade_count,
                        r.closed_trade_count,
                        r.error,
                    ]
                )
            return output.getvalue()
        raise ValueError("format must be csv or json")


# ── helpers ─────────────────────────────────────────────────────────────


def _serializable_grid(
    grid: dict[str, Any],
) -> dict[str, Any]:
    """Convert the pydantic grid items to plain dicts for JSON storage."""
    out: dict[str, object] = {}
    for key, item in grid.items():
        if hasattr(item, "model_dump"):
            out[key] = item.model_dump()
        else:
            out[key] = item
    return out


def _deserialize_grid(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Reconstruct ``StrategyExperimentGridItem`` objects from stored dicts."""
    from app.schemas import StrategyExperimentGridItem

    out: dict[str, object] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[key] = StrategyExperimentGridItem.model_validate(value)
        else:
            out[key] = value
    return out


def _load_bars(request: StrategyExperimentRunRequest) -> list[BacktestBar]:
    if request.csv_text and request.csv_text.strip():
        lines = request.csv_text.strip().splitlines()
        # At least header + one non-empty data row.
        if len(lines) < 2 or all(not line.strip() for line in lines[1:]):
            raise ValueError("price data is required")
        return parse_backtest_csv(request.csv_text)
    return [
        BacktestBar(
            timestamp=point.timestamp,
            open=point.open,
            high=point.high,
            low=point.low,
            close=point.close,
            volume=point.volume,
        )
        for point in request.price_points
    ]


def _build_result_summary(result: BacktestResultData) -> dict[str, Any]:
    return {
        "metrics": {
            "initial_cash": result.metrics.initial_cash,
            "final_equity": result.metrics.final_equity,
            "total_pnl": result.metrics.total_pnl,
            "total_return_pct": result.metrics.total_return_pct,
            "max_drawdown_pct": result.metrics.max_drawdown_pct,
            "trade_count": result.metrics.trade_count,
            "closed_trade_count": result.metrics.closed_trade_count,
            "winning_trades": result.metrics.winning_trades,
            "losing_trades": result.metrics.losing_trades,
            "win_rate": result.metrics.win_rate,
            "avg_holding_minutes": result.metrics.avg_holding_minutes,
            "fees_paid": result.metrics.fees_paid,
            "skipped_signals": result.metrics.skipped_signals,
            "final_state": result.metrics.final_state,
        },
        "trades": [
            _trade_to_dict(t) for t in result.trades[:20]
        ],
        "equity_curve": [
            _equity_point_to_dict(e) for e in result.equity_curve[:200]
        ],
    }


def _trade_to_dict(trade) -> dict[str, Any]:
    return {
        "timestamp": trade.timestamp.isoformat(),
        "action": trade.action,
        "price": trade.price,
        "quantity": trade.quantity,
        "fee": trade.fee,
        "pnl": trade.pnl,
        "state_after": trade.state_after,
        "reason": trade.reason,
        "holding_minutes": trade.holding_minutes,
    }


def _equity_point_to_dict(point) -> dict[str, Any]:
    return {
        "timestamp": point.timestamp.isoformat(),
        "close": point.close,
        "equity": point.equity,
        "realized_pnl": point.realized_pnl,
        "unrealized_pnl": point.unrealized_pnl,
        "drawdown_pct": point.drawdown_pct,
        "position": point.position,
    }
