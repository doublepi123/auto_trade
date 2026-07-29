"""Drawdown analysis panel (GET /api/drawdown-analysis/*). Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_drawdown_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig
from app.services.drawdown_analysis_service import DrawdownAnalysisService


def _dt(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


def _order(
    oid: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    day: date,
    hour: int = 10,
) -> OrderRecord:
    """A fully-filled BUY/SELL order round-trip leg."""
    return OrderRecord(
        broker_order_id=oid,
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        executed_quantity=qty,
        executed_price=price,
        status="FILLED",
        created_at=_dt(day, hour),
        filled_at=_dt(day, hour, 1),
    )


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
        db = Session(bind=self.engine)
        db.query(OrderRecord).delete()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _ensure_config(self) -> None:
        """DailyPnlService fee lookup falls back to defaults without a config row."""
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US", fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _seed_round_trip(
        self,
        *,
        symbol: str,
        entry_oid: str,
        exit_oid: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        entry_day: date,
        exit_day: date,
    ) -> None:
        db = self._db()
        db.add_all([
            _order(entry_oid, symbol, "BUY", qty, entry_price, entry_day),
            _order(exit_oid, symbol, "SELL", qty, exit_price, exit_day),
        ])
        db.commit()
        db.close()


class TestDrawdownEmpty(_Base):
    def test_empty_db_returns_zero_drawdown_summary(self) -> None:
        self._ensure_config()
        svc = DrawdownAnalysisService(self._db())
        summary = svc.get_drawdown_summary(symbol=None, days=90)
        assert summary["current_drawdown"] == 0.0
        assert summary["max_drawdown"] == 0.0
        assert summary["is_in_drawdown"] is False
        assert summary["recovery_count"] == 0
        assert summary["period_days"] == 90

    def test_empty_db_returns_empty_timeline(self) -> None:
        self._ensure_config()
        svc = DrawdownAnalysisService(self._db())
        timeline = svc.get_drawdown_timeline(symbol=None, days=90)
        assert timeline == []

    def test_empty_db_endpoints_return_ok(self) -> None:
        self._ensure_config()
        summary = self.client.get("/api/drawdown-analysis/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["max_drawdown"] == 0.0
        assert body["is_in_drawdown"] is False
        assert body["max_drawdown_date"] is None

        timeline = self.client.get("/api/drawdown-analysis/timeline")
        assert timeline.status_code == 200, timeline.text
        assert timeline.json() == []


class TestDrawdownComputation(_Base):
    def test_winning_round_trip_has_no_drawdown(self) -> None:
        self._ensure_config()
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b1",
            exit_oid="s1",
            entry_price=10.0,
            exit_price=12.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 5),
        )
        svc = DrawdownAnalysisService(self._db())
        summary = svc.get_drawdown_summary(symbol="AAPL.US", days=400)
        # Net PnL = (12 - 10) * 100 - fees(0) = +200
        assert summary["peak_pnl"] == 200.0
        assert summary["current_pnl"] == 200.0
        assert summary["current_drawdown"] == 0.0
        assert summary["max_drawdown"] == 0.0
        assert summary["is_in_drawdown"] is False
        assert summary["recovery_count"] == 0

    def test_loss_after_gain_produces_drawdown_and_recovery(self) -> None:
        self._ensure_config()
        # Trip 1: +200 (peak 200)
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b1",
            exit_oid="s1",
            entry_price=10.0,
            exit_price=12.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 5),
        )
        # Trip 2: -50 (cumulative 150, drawdown -50 from peak 200)
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b2",
            exit_oid="s2",
            entry_price=10.0,
            exit_price=9.5,
            qty=100.0,
            entry_day=date(2026, 1, 8),
            exit_day=date(2026, 1, 10),
        )
        # Trip 3: +80 (cumulative 230 -> new peak, completes recovery)
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b3",
            exit_oid="s3",
            entry_price=10.0,
            exit_price=10.8,
            qty=100.0,
            entry_day=date(2026, 1, 12),
            exit_day=date(2026, 1, 14),
        )
        svc = DrawdownAnalysisService(self._db())
        summary = svc.get_drawdown_summary(symbol="AAPL.US", days=400)

        assert summary["peak_pnl"] == 230.0
        assert summary["current_pnl"] == 230.0
        assert summary["max_drawdown"] == -50.0
        assert summary["is_in_drawdown"] is False  # recovered by trip 3
        assert summary["recovery_count"] == 1
        # Drawdown started at trip 2 exit (2026-01-10), recovered trip 3 exit
        # (2026-01-14) -> 4 calendar days.
        assert summary["avg_recovery_days"] == 4.0
        # Max drawdown was reached on trip 2 exit.
        assert summary["max_drawdown_date"] is not None
        assert summary["max_drawdown_date"].startswith("2026-01-10")

    def test_current_drawdown_when_unrecovered(self) -> None:
        self._ensure_config()
        # Peak then a loss with no recovery.
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b1",
            exit_oid="s1",
            entry_price=10.0,
            exit_price=12.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 5),
        )
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b2",
            exit_oid="s2",
            entry_price=10.0,
            exit_price=9.0,
            qty=100.0,
            entry_day=date(2026, 1, 8),
            exit_day=date(2026, 1, 10),
        )
        svc = DrawdownAnalysisService(self._db())
        summary = svc.get_drawdown_summary(symbol="AAPL.US", days=400)

        # Cumulative = 200 - 100 = 100; peak 200; drawdown -100.
        assert summary["peak_pnl"] == 200.0
        assert summary["current_pnl"] == 100.0
        assert summary["current_drawdown"] == -100.0
        assert summary["is_in_drawdown"] is True
        assert summary["recovery_count"] == 0

    def test_symbol_filter_isolates_per_symbol_curves(self) -> None:
        self._ensure_config()
        # AAPL winner, TSLA loser.
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="ab",
            exit_oid="as",
            entry_price=10.0,
            exit_price=11.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 3),
        )
        self._seed_round_trip(
            symbol="TSLA.US",
            entry_oid="tb",
            exit_oid="ts",
            entry_price=20.0,
            exit_price=18.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 3),
        )
        svc = DrawdownAnalysisService(self._db())

        aapl = svc.get_drawdown_summary(symbol="AAPL.US", days=400)
        tsla = svc.get_drawdown_summary(symbol="TSLA.US", days=400)
        assert aapl["current_pnl"] == 100.0
        assert aapl["is_in_drawdown"] is False
        # TSLA: only one losing trip -> peak 0, no positive peak so no drawdown
        # segment (drawdown_pct is 0 by convention when peak is not positive).
        assert tsla["current_pnl"] == -200.0


class TestDrawdownTimeline(_Base):
    def test_timeline_chronological_and_tracks_peak(self) -> None:
        self._ensure_config()
        # Two trips in deliberate non-chronological insert order to confirm sort.
        # Trip B (later) wins, Trip A (earlier) loses a bit.
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="ba",
            exit_oid="sa",
            entry_price=10.0,
            exit_price=10.5,
            qty=100.0,
            entry_day=date(2026, 1, 10),  # later exit
            exit_day=date(2026, 1, 14),
        )
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="bb",
            exit_oid="sb",
            entry_price=10.0,
            exit_price=11.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),  # earlier exit
            exit_day=date(2026, 1, 5),
        )
        svc = DrawdownAnalysisService(self._db())
        timeline = svc.get_drawdown_timeline(symbol="AAPL.US", days=400)
        assert len(timeline) == 2

        # Chronological by exit time.
        dates = [point["timestamp"] for point in timeline]
        assert dates == sorted(dates)
        # First point: +100, peak 100, not in drawdown.
        assert timeline[0]["cumulative_pnl"] == 100.0
        assert timeline[0]["peak_pnl"] == 100.0
        assert timeline[0]["is_in_drawdown"] is False
        # Second point: +150 cumulative, peak 150.
        assert timeline[1]["cumulative_pnl"] == 150.0
        assert timeline[1]["peak_pnl"] == 150.0

    def test_timeline_endpoint_returns_points(self) -> None:
        self._ensure_config()
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b1",
            exit_oid="s1",
            entry_price=10.0,
            exit_price=12.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 5),
        )
        resp = self.client.get("/api/drawdown-analysis/timeline", params={"days": 400})
        assert resp.status_code == 200, resp.text
        points = resp.json()
        assert len(points) == 1
        point = points[0]
        assert point["cumulative_pnl"] == 200.0
        assert point["peak_pnl"] == 200.0
        assert point["drawdown"] == 0.0
        assert point["is_in_drawdown"] is False
        assert point["date"].startswith("2026-01-05")


class TestDrawdownWindow(_Base):
    def test_days_window_excludes_old_trips(self) -> None:
        """Round trips whose exit falls before the window are excluded."""
        self._ensure_config()
        self._seed_round_trip(
            symbol="AAPL.US",
            entry_oid="b1",
            exit_oid="s1",
            entry_price=10.0,
            exit_price=12.0,
            qty=100.0,
            entry_day=date(2026, 1, 1),
            exit_day=date(2026, 1, 5),
        )
        svc = DrawdownAnalysisService(self._db())
        # A 1-day window ending "now" (well after Jan 2026) excludes Jan 5 trip.
        summary = svc.get_drawdown_summary(symbol="AAPL.US", days=1)
        assert summary["current_pnl"] == 0.0
        assert summary["max_drawdown"] == 0.0
