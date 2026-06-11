from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_strategy_experiment_svc_{os.getpid()}.db"
)

import pytest

from app.database import SessionLocal, engine as db_engine
from app.models import Base, StrategyExperiment, StrategyExperimentRun
from app.schemas import (
    BacktestParams,
    StrategyExperimentCreate,
    StrategyExperimentGridItem,
    StrategyExperimentRunRequest,
)
from app.services.strategy_experiment_service import StrategyExperimentService

Base.metadata.create_all(bind=db_engine)

# ── helpers ─────────────────────────────────────────────────────────────

_CSV = (
    "timestamp,open,high,low,close,volume\n"
    "2026-05-22T10:00:00Z,150,160,99,105,1000\n"
    "2026-05-22T10:01:00Z,150,201,140,200,1000\n"
)


def _make_create_request(
    name: str = "test-exp",
    symbol: str = "AAPL.US",
    buy_low_values: list[float] | None = None,
) -> StrategyExperimentCreate:
    if buy_low_values is None:
        buy_low_values = [100.0]
    return StrategyExperimentCreate(
        name=name,
        symbol=symbol,
        base_params=BacktestParams(
            symbol="AAPL.US",
            buy_low=120.0,
            sell_high=180.0,
            short_selling=False,
            min_profit_amount=0.0,
            max_daily_loss=5000.0,
            max_consecutive_losses=3,
            quantity=1.0,
            initial_cash=10000.0,
            fee_rate=0.0,
            fixed_fee=0.0,
            slippage_pct=0.0,
            stop_loss_pct=0.0,
        ),
        parameter_grid={
            "buy_low": StrategyExperimentGridItem(value=buy_low_values[0])
            if len(buy_low_values) == 1
            else StrategyExperimentGridItem(values=buy_low_values),
        },
    )


def _clean_db() -> None:
    db = SessionLocal()
    try:
        db.query(StrategyExperimentRun).delete()
        db.query(StrategyExperiment).delete()
        db.commit()
    finally:
        db.close()


from typing import Generator

@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    _clean_db()
    yield
    _clean_db()


# ── tests ───────────────────────────────────────────────────────────────


class TestCreateExperiment:
    def test_stores_json_and_estimated_runs(self, clean_db: None) -> None:
        req = _make_create_request(buy_low_values=[100.0, 120.0, 140.0])
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            resp = svc.create_experiment(req)
            db.expire_all()

            assert resp.name == "test-exp"
            assert resp.symbol == "AAPL.US"
            assert resp.status == "PENDING"
            assert resp.estimated_runs == 3  # 3 buy_low values
            assert resp.completed_runs == 0
            assert resp.failed_runs == 0

            # Verify stored JSON is valid.
            import json

            base = json.loads(resp.base_params_json)
            assert base["buy_low"] == 120.0
            assert base["sell_high"] == 180.0

            grid = json.loads(resp.parameter_grid_json)
            assert "buy_low" in grid
        finally:
            db.close()


class TestRunExperiment:
    def test_creates_one_run_per_valid_combination_and_updates_status(
        self, clean_db: None
    ) -> None:
        req = _make_create_request(buy_low_values=[100.0, 120.0])
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            exp_id = created.id

            run_req = StrategyExperimentRunRequest(csv_text=_CSV)
            result = svc.run_experiment(exp_id, run_req)

            assert result.status == "COMPLETED"
            assert result.completed_runs == 2
            assert result.failed_runs == 0
            assert result.completed_at is not None

            # Verify runs stored in DB.
            runs = (
                db.query(StrategyExperimentRun)
                .filter(StrategyExperimentRun.experiment_id == exp_id)
                .all()
            )
            assert len(runs) == 2
            for r in runs:
                assert r.status == "COMPLETED"
                assert r.result_summary_json != "{}"
        finally:
            db.close()

    def test_one_failed_run_does_not_abort_remaining(
        self, clean_db: None
    ) -> None:
        req = _make_create_request(buy_low_values=[100.0, 120.0, 140.0])
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            exp_id = created.id

            # Patch BacktestEngine.run to fail for buy_low == 120.0.
            original_run = __import__(
                "app.core.backtest", fromlist=["BacktestEngine"]
            ).BacktestEngine.run

            def _mock_run(self_eng, bars, **kwargs):
                if self_eng.params.buy_low == 120.0:
                    raise RuntimeError("simulated engine failure")
                return original_run(self_eng, bars, **kwargs)

            with patch(
                "app.core.backtest.BacktestEngine.run", _mock_run
            ):
                run_req = StrategyExperimentRunRequest(csv_text=_CSV)
                result = svc.run_experiment(exp_id, run_req)

            assert result.status == "COMPLETED"  # not all failed
            assert result.completed_runs == 2  # 100, 140 succeed
            assert result.failed_runs == 1      # 120 fails

            runs = (
                db.query(StrategyExperimentRun)
                .filter(StrategyExperimentRun.experiment_id == exp_id)
                .all()
            )
            statuses = {r.status for r in runs}
            assert "FAILED" in statuses
            assert "COMPLETED" in statuses
        finally:
            db.close()

    def test_all_failed_sets_experiment_failed(self, clean_db: None) -> None:
        req = _make_create_request(buy_low_values=[100.0, 120.0])
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            exp_id = created.id

            def _always_fail(self_eng, bars, **kwargs):
                raise RuntimeError("simulated engine failure")

            with patch(
                "app.core.backtest.BacktestEngine.run", _always_fail
            ):
                run_req = StrategyExperimentRunRequest(csv_text=_CSV)
                result = svc.run_experiment(exp_id, run_req)

            assert result.status == "FAILED"
            assert result.completed_runs == 0
            assert result.failed_runs == 2
            assert result.error == "all runs failed"
        finally:
            db.close()

    def test_requires_price_data(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            req = _make_create_request()
            created = svc.create_experiment(req)

            run_req = StrategyExperimentRunRequest(
                csv_text="timestamp,open,high,low,close,volume\n"
            )
            with pytest.raises(ValueError, match="price data is required"):
                svc.run_experiment(created.id, run_req)
        finally:
            db.close()

    def test_deletes_previous_runs_before_rerun(self, clean_db: None) -> None:
        req = _make_create_request(buy_low_values=[100.0])
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            exp_id = created.id

            # First run.
            run_req = StrategyExperimentRunRequest(csv_text=_CSV)
            svc.run_experiment(exp_id, run_req)

            # Second run.
            result = svc.run_experiment(exp_id, run_req)

            runs = (
                db.query(StrategyExperimentRun)
                .filter(StrategyExperimentRun.experiment_id == exp_id)
                .all()
            )
            # Only runs from the second invocation remain.
            assert len(runs) == 1
            assert result.completed_runs == 1
        finally:
            db.close()


class TestListRuns:
    def _setup_experiment_with_runs(self, db) -> int:
        """Create an experiment with 3 runs; return experiment_id."""
        req = _make_create_request(buy_low_values=[100.0, 120.0, 140.0])
        svc = StrategyExperimentService(db)
        created = svc.create_experiment(req)
        run_req = StrategyExperimentRunRequest(csv_text=_CSV)
        svc.run_experiment(created.id, run_req)
        return created.id

    def test_sorts_by_total_return_pct_desc(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            exp_id = self._setup_experiment_with_runs(db)
            svc = StrategyExperimentService(db)
            page = svc.list_runs(
                experiment_id=exp_id,
                sort="total_return_pct",
                order="desc",
                page=1,
                page_size=10,
            )
            assert page.total == 3
            assert len(page.items) == 3
            # Verify descending order.
            returns = [r.total_return_pct for r in page.items]
            assert returns == sorted(returns, reverse=True)
            # Verify parameters field is a dict.
            for item in page.items:
                assert isinstance(item.parameters, dict)
        finally:
            db.close()

    def test_paginates_and_no_duplicates(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            exp_id = self._setup_experiment_with_runs(db)
            svc = StrategyExperimentService(db)

            collected_ids: list[int] = []
            page = 1
            page_size = 2
            while True:
                p = svc.list_runs(
                    experiment_id=exp_id,
                    sort="created_at",
                    order="asc",
                    page=page,
                    page_size=page_size,
                )
                for item in p.items:
                    collected_ids.append(item.id)
                if page * page_size >= p.total:
                    break
                page += 1

            assert len(collected_ids) == p.total
            assert len(set(collected_ids)) == len(collected_ids)
        finally:
            db.close()


class TestErrors:
    def test_missing_experiment(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            svc = StrategyExperimentService(db)
            with pytest.raises(ValueError, match="strategy experiment not found"):
                svc.get_experiment(9999)
            run_req = StrategyExperimentRunRequest(csv_text=_CSV)
            with pytest.raises(ValueError, match="strategy experiment not found"):
                svc.run_experiment(9999, run_req)
            with pytest.raises(ValueError, match="strategy experiment not found"):
                svc.list_runs(
                    experiment_id=9999,
                    sort="total_return_pct",
                    order="desc",
                    page=1,
                    page_size=10,
                )
        finally:
            db.close()

    def test_invalid_sort_field(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            req = _make_create_request()
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            with pytest.raises(ValueError, match="unsupported sort field"):
                svc.list_runs(
                    experiment_id=created.id,
                    sort="nonexistent",
                    order="asc",
                    page=1,
                    page_size=10,
                )
        finally:
            db.close()

    def test_invalid_sort_order(self, clean_db: None) -> None:
        db = SessionLocal()
        try:
            req = _make_create_request()
            svc = StrategyExperimentService(db)
            created = svc.create_experiment(req)
            with pytest.raises(ValueError, match="unsupported sort order"):
                svc.list_runs(
                    experiment_id=created.id,
                    sort="total_return_pct",
                    order="random",
                    page=1,
                    page_size=10,
                )
        finally:
            db.close()
