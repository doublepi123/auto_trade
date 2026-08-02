"""Saved backtest runs for side-by-side comparison."""
from __future__ import annotations

import json
import math
from typing import Any, Literal

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


def _classify_value(value: Any) -> tuple[str, float | None]:
    """Classify a raw metric value as NUMERIC / MISSING / NON_NUMERIC.

    Returns ``(classification, numeric_value)``. ``bool`` is NON_NUMERIC.
    Non-finite floats (inf/nan) are NON_NUMERIC.
    """
    if value is None:
        return "MISSING", None
    if isinstance(value, bool):
        return "NON_NUMERIC", None
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError, OverflowError):
            return "NON_NUMERIC", None
        if not math.isfinite(num):
            return "NON_NUMERIC", None
        return "NUMERIC", num
    return "NON_NUMERIC", None


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

    def compare_with_metrics(
        self,
        run_ids: list[int],
        *,
        baseline_id: int | None = None,
    ) -> dict[str, Any]:
        """Enhanced comparison with explicit baseline and metric table.

        Preserves the existing ``compare()`` behavior for the ``runs`` field
        while adding a deterministic metric comparison table. Never executes a
        backtest, calls the engine/broker, or writes.
        """
        if not run_ids:
            return {
                "runs": [],
                "baseline_id": None,
                "missing_run_ids": [],
                "metric_comparison": [],
            }

        # Deduplicate + preserve order; cap to 8.
        unique_ids: list[int] = []
        seen: set[int] = set()
        for rid in run_ids:
            if rid not in seen:
                seen.add(rid)
                unique_ids.append(rid)
        unique_ids = unique_ids[:8]

        rows = list(
            self._db.scalars(select(BacktestRun).where(BacktestRun.id.in_(unique_ids)))
        )
        by_id = {r.id: r for r in rows}

        missing_run_ids = [rid for rid in unique_ids if rid not in by_id]
        existing_ids = [rid for rid in unique_ids if rid in by_id]

        # Baseline selection: explicit or default to first existing.
        if baseline_id is not None:
            if baseline_id not in unique_ids:
                raise ValueError(
                    f"baseline_id {baseline_id} must be in the requested ids"
                )
            if baseline_id not in by_id:
                raise ValueError(f"baseline_id {baseline_id} not found")
            effective_baseline = baseline_id
        elif existing_ids:
            effective_baseline = existing_ids[0]
        else:
            effective_baseline = None

        runs_out = [self._to_out(by_id[i]) for i in existing_ids]
        metric_comparison = self._build_metric_comparison(
            by_id, existing_ids, effective_baseline
        )

        return {
            "runs": runs_out,
            "baseline_id": effective_baseline,
            "missing_run_ids": missing_run_ids,
            "metric_comparison": metric_comparison,
        }

    def _build_metric_comparison(
        self,
        by_id: dict[int, BacktestRun],
        existing_ids: list[int],
        baseline_id: int | None,
    ) -> list[dict[str, Any]]:
        """Build deterministic metric comparison from raw metrics_json."""
        raw_metrics: dict[int, dict[str, Any]] = {}
        for rid in existing_ids:
            run = by_id[rid]
            try:
                parsed = json.loads(run.metrics_json)
                if not isinstance(parsed, dict):
                    parsed = {"__invalid__": True}
            except (json.JSONDecodeError, TypeError):
                parsed = {"__invalid__": True}
            raw_metrics[rid] = parsed

        baseline_metrics = raw_metrics.get(baseline_id, {}) if baseline_id else {}

        all_names: set[str] = set()
        for metrics in raw_metrics.values():
            all_names.update(k for k in metrics if not k.startswith("__"))
        sorted_names = sorted(all_names)

        rows: list[dict[str, Any]] = []
        for name in sorted_names:
            baseline_raw = baseline_metrics.get(name)
            baseline_cls, baseline_num = _classify_value(baseline_raw)

            run_entries: list[dict[str, Any]] = []
            for rid in existing_ids:
                run_raw = raw_metrics[rid].get(name)
                run_cls, run_num = _classify_value(run_raw)
                delta: float | None = None
                if run_cls == "NUMERIC" and baseline_cls == "NUMERIC":
                    assert run_num is not None and baseline_num is not None
                    delta = run_num - baseline_num
                run_entries.append(
                    {
                        "run_id": rid,
                        "classification": run_cls,
                        "raw_value": run_raw,
                        "delta": delta,
                    }
                )

            rows.append(
                {
                    "metric": name,
                    "baseline_value": baseline_raw,
                    "baseline_classification": baseline_cls,
                    "runs": run_entries,
                }
            )
        return rows

    @staticmethod
    def _to_out(run: BacktestRun) -> BacktestRunOut:
        try:
            params = BacktestParams.model_validate_json(run.params_json)
        except Exception:
            # Graceful degradation for corrupt/legacy rows. Must use values that
            # satisfy BacktestParams' gt=0 constraints (buy_low/sell_high) or the
            # fallback itself raises — defeating the purpose.
            params = BacktestParams(buy_low=1.0, sell_high=2.0)
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
