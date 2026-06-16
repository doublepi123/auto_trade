"""Saved backtest runs for side-by-side comparison."""
from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import BacktestRun
from app.schemas import (
    BacktestMetrics,
    BacktestParams,
    BacktestRunOut,
    BacktestRunPage,
    BacktestRunSaveRequest,
)


class BacktestRunService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def save(self, payload: BacktestRunSaveRequest) -> BacktestRunOut:
        run = BacktestRun(
            name=payload.name.strip(),
            symbol=(payload.params.symbol or ""),
            params_json=payload.params.model_dump_json(),
            metrics_json=payload.metrics.model_dump_json(),
        )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)
        return self._to_out(run)

    def list_runs(self, *, page: int = 1, page_size: int = 50) -> BacktestRunPage:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        total = self._db.scalar(select(func.count()).select_from(BacktestRun)) or 0
        stmt = (
            select(BacktestRun)
            .order_by(desc(BacktestRun.created_at), desc(BacktestRun.id))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = list(self._db.scalars(stmt))
        return BacktestRunPage(
            items=[self._to_out(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def get(self, run_id: int) -> BacktestRunOut | None:
        run = self._db.get(BacktestRun, run_id)
        return self._to_out(run) if run is not None else None

    def delete(self, run_id: int) -> bool:
        run = self._db.get(BacktestRun, run_id)
        if run is None:
            return False
        self._db.delete(run)
        self._db.commit()
        return True

    def compare(self, run_ids: list[int]) -> list[BacktestRunOut]:
        if not run_ids:
            return []
        # Deduplicate + preserve order; cap to a sane number.
        unique_ids: list[int] = []
        seen: set[int] = set()
        for rid in run_ids:
            if rid not in seen:
                seen.add(rid)
                unique_ids.append(rid)
        unique_ids = unique_ids[:8]
        rows = list(self._db.scalars(select(BacktestRun).where(BacktestRun.id.in_(unique_ids))))
        by_id = {r.id: r for r in rows}
        return [self._to_out(by_id[i]) for i in unique_ids if i in by_id]

    @staticmethod
    def _to_out(run: BacktestRun) -> BacktestRunOut:
        try:
            params = BacktestParams.model_validate_json(run.params_json)
        except Exception:
            params = BacktestParams(buy_low=0, sell_high=0)  # noqa: TRY002 — defensive
        try:
            metrics = BacktestMetrics.model_validate_json(run.metrics_json)
        except Exception:
            metrics = BacktestMetrics(
                initial_cash=0, final_equity=0, total_pnl=0, total_return_pct=0,
                max_drawdown_pct=0, trade_count=0, closed_trade_count=0, winning_trades=0,
                losing_trades=0, win_rate=0, avg_holding_minutes=0, fees_paid=0,
                skipped_signals=0, final_state="flat",
            )
        return BacktestRunOut(
            id=run.id,
            name=run.name,
            symbol=run.symbol,
            params=params,
            metrics=metrics,
            created_at=run.created_at,
        )
