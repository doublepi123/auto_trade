from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import PlatformBacktestRun
from app.platform.backtest_service import PlatformBacktestService

__all__ = ["BacktestRunService"]


class BacktestRunService:
    """Persist platform backtest runs and provide list/get/compare queries.

    Wraps :class:`PlatformBacktestService` to run a backtest then store the
    full result (equity curve, fills, positions, stats, analytics) in
    ``platform_backtest_runs``. ``final_nav`` and ``sharpe`` are denormalized
    so list/compare endpoints avoid deserializing the full ``result_json``.
    """

    def __init__(self, db: Session, backtest: PlatformBacktestService | None = None) -> None:
        self.db = db
        self.backtest = backtest or PlatformBacktestService()

    def create(
        self,
        name: str,
        strategy_name: str,
        params: dict[str, Any],
        symbols: list[str],
        bars: list[dict[str, Any]],
        initial_cash: Decimal = Decimal("100000"),
    ) -> PlatformBacktestRun:
        result = self.backtest.run(
            strategy_name=strategy_name,
            params=params,
            symbols=symbols,
            bars=bars,
            initial_cash=initial_cash,
        )
        row = PlatformBacktestRun(
            name=name,
            strategy=strategy_name,
            params_json=json.dumps(params, default=str),
            symbols_json=json.dumps(symbols),
            result_json=json.dumps(result, default=str),
            final_nav=float(result.get("stats", {}).get("final_nav", 0.0)),
            sharpe=float(result.get("analytics", {}).get("sharpe", 0.0)),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PlatformBacktestRun)
            .order_by(PlatformBacktestRun.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "name": r.name,
                "strategy": r.strategy,
                "final_nav": r.final_nav,
                "sharpe": r.sharpe,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.query(PlatformBacktestRun).filter_by(id=run_id).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "strategy": row.strategy,
            "params": json.loads(row.params_json),
            "symbols": json.loads(row.symbols_json),
            "result": json.loads(row.result_json),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def compare(self, run_ids: list[int]) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PlatformBacktestRun)
            .filter(PlatformBacktestRun.id.in_(run_ids))
            .all()
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            result = json.loads(r.result_json)
            stats = result.get("stats", {})
            analytics = result.get("analytics", {})
            out.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "strategy": r.strategy,
                    "final_nav": r.final_nav,
                    "pnl": stats.get("pnl", 0.0),
                    "sharpe": analytics.get("sharpe", 0.0),
                    "sortino": analytics.get("sortino", 0.0),
                    "max_drawdown": analytics.get("max_drawdown", 0.0),
                    "win_rate": analytics.get("win_rate", 0.0),
                }
            )
        return out
