"""Saved backtest runs (comparison) — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_backtest_runs_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base
from app.schemas import BacktestMetrics, BacktestParams, BacktestRunSaveRequest
from app.services.backtest_run_service import BacktestRunService


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False}
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        from app.models import BacktestRun
        db = Session(bind=self.engine)
        db.query(BacktestRun).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _params(self, **kw) -> BacktestParams:
        return BacktestParams(buy_low=kw.get("buy_low", 100), sell_high=kw.get("sell_high", 200), **{k: v for k, v in kw.items() if k not in ("buy_low", "sell_high")})

    def _metrics(self, **kw) -> BacktestMetrics:
        base = dict(
            initial_cash=100000, final_equity=10100, total_pnl=100, total_return_pct=0.1,
            max_drawdown_pct=1.0, trade_count=2, closed_trade_count=1, winning_trades=1,
            losing_trades=0, win_rate=100, avg_holding_minutes=5, fees_paid=0,
            skipped_signals=0, final_state="flat",
        )
        base.update(kw)
        return BacktestMetrics(**base)


class TestBacktestRunService(_Base):
    def test_save_and_get(self) -> None:
        svc = BacktestRunService(self._db())
        out = svc.save(BacktestRunSaveRequest(name="run A", params=self._params(), metrics=self._metrics(total_pnl=100)))
        assert out.id > 0
        assert out.name == "run A"
        assert out.metrics.total_pnl == 100
        got = svc.get(out.id)
        assert got is not None and got.name == "run A"

    def test_list_paginates(self) -> None:
        svc = BacktestRunService(self._db())
        for i in range(3):
            svc.save(BacktestRunSaveRequest(name=f"r{i}", params=self._params(), metrics=self._metrics()))
        page = svc.list_runs(page=1, page_size=2)
        assert page.total == 3
        assert len(page.items) == 2

    def test_compare_preserves_order_and_dedupes(self) -> None:
        svc = BacktestRunService(self._db())
        a = svc.save(BacktestRunSaveRequest(name="A", params=self._params(), metrics=self._metrics(total_pnl=10)))
        b = svc.save(BacktestRunSaveRequest(name="B", params=self._params(), metrics=self._metrics(total_pnl=20)))
        compared = svc.compare([b.id, a.id, b.id])
        assert [r.id for r in compared] == [b.id, a.id]

    def test_delete(self) -> None:
        svc = BacktestRunService(self._db())
        out = svc.save(BacktestRunSaveRequest(name="x", params=self._params(), metrics=self._metrics()))
        assert svc.delete(out.id) is True
        assert svc.delete(out.id) is False
        assert svc.get(out.id) is None


class TestBacktestRunAPI(_Base):
    def test_save_list_compare_delete(self) -> None:
        save = self.client.post("/api/backtest/runs", json={
            "name": "API run",
            "params": {"buy_low": 100, "sell_high": 200},
            "metrics": {
                "initial_cash": 100000, "final_equity": 100200, "total_pnl": 200,
                "total_return_pct": 0.2, "max_drawdown_pct": 0.5, "trade_count": 2,
                "closed_trade_count": 1, "winning_trades": 1, "losing_trades": 0,
                "win_rate": 100, "avg_holding_minutes": 1, "fees_paid": 0,
                "skipped_signals": 0, "final_state": "flat",
            },
        })
        assert save.status_code == 200, save.text
        run_id = save.json()["id"]

        lst = self.client.get("/api/backtest/runs")
        assert lst.status_code == 200
        assert lst.json()["total"] == 1

        cmp = self.client.get("/api/backtest/runs/compare", params={"ids": [run_id]})
        assert cmp.status_code == 200
        assert len(cmp.json()["runs"]) == 1

        one = self.client.get(f"/api/backtest/runs/{run_id}")
        assert one.status_code == 200

        missing = self.client.get("/api/backtest/runs/999999")
        assert missing.status_code == 404

        dele = self.client.delete(f"/api/backtest/runs/{run_id}")
        assert dele.status_code == 204

    def test_save_validates_name(self) -> None:
        resp = self.client.post("/api/backtest/runs", json={
            "name": "", "params": {"buy_low": 100, "sell_high": 200}, "metrics": {
                "initial_cash": 1, "final_equity": 1, "total_pnl": 0, "total_return_pct": 0,
                "max_drawdown_pct": 0, "trade_count": 0, "closed_trade_count": 0,
                "winning_trades": 0, "losing_trades": 0, "win_rate": 0,
                "avg_holding_minutes": 0, "fees_paid": 0, "skipped_signals": 0, "final_state": "flat",
            },
        })
        assert resp.status_code == 422
