"""Strategy health monitor (GET /api/strategy-health/*). Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_strategy_health_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig, StrategyV2ShadowTrade
from app.services.strategy_health_service import StrategyHealthService


def _dt(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


def _order(
    oid: str, symbol: str, side: str, qty: float, price: float, day: date, hour: int = 10
) -> OrderRecord:
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


def _shadow_trade(
    *,
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    entry_day: date,
    exit_day: date,
    net_pnl: float,
    holding_seconds: float | None = None,
) -> StrategyV2ShadowTrade:
    return StrategyV2ShadowTrade(
        symbol=symbol,
        status="CLOSED",
        entry_at=_dt(entry_day),
        exit_at=_dt(exit_day),
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=qty,
        net_pnl=net_pnl,
        gross_pnl=net_pnl,
        holding_seconds=holding_seconds if holding_seconds is not None
        else (_dt(exit_day) - _dt(entry_day)).total_seconds(),
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
        db.query(StrategyV2ShadowTrade).delete()
        db.query(OrderRecord).delete()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _ensure_config(self) -> None:
        db = self._db()
        db.add(StrategyConfig(symbol="AAPL.US", market="US", fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _seed_live_round_trip(
        self, *, oid_buy: str, oid_sell: str, qty: float, entry_price: float,
        exit_price: float, entry_day: date, exit_day: date,
    ) -> None:
        db = self._db()
        db.add_all([
            _order(oid_buy, "AAPL.US", "BUY", qty, entry_price, entry_day),
            _order(oid_sell, "AAPL.US", "SELL", qty, exit_price, exit_day),
        ])
        db.commit()
        db.close()

    def _seed_shadow(self, trade: StrategyV2ShadowTrade) -> None:
        db = self._db()
        db.add(trade)
        db.commit()
        db.close()


class TestInsufficientData(_Base):
    def test_empty_db_returns_insufficient_data(self) -> None:
        self._ensure_config()
        svc = StrategyHealthService(self._db())
        report = svc.get_health_report(symbol="AAPL.US")
        assert report["health_status"] == "INSUFFICIENT_DATA"
        assert report["live_metrics"]["trade_count"] == 0
        assert report["shadow_metrics"]["trade_count"] == 0
        assert report["alerts"]  # non-empty explanation

    def test_empty_db_endpoint_returns_insufficient_data(self) -> None:
        self._ensure_config()
        resp = self.client.get("/api/strategy-health/report")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["health_status"] == "INSUFFICIENT_DATA"
        assert body["live_metrics"]["trade_count"] == 0
        assert body["shadow_metrics"]["trade_count"] == 0

    def test_live_only_returns_insufficient_data(self) -> None:
        self._ensure_config()
        now = datetime.now(timezone.utc)
        self._seed_live_round_trip(
            oid_buy="b1", oid_sell="s1", qty=100.0, entry_price=10.0,
            exit_price=11.0,
            entry_day=(now - timedelta(days=4)).date(),
            exit_day=(now - timedelta(days=2)).date(),
        )
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        # 1 live trade is below MIN_TRADES_FOR_VERDICT (3).
        assert report["health_status"] == "INSUFFICIENT_DATA"
        assert report["live_metrics"]["trade_count"] == 1

    def test_shadow_only_returns_insufficient_data(self) -> None:
        self._ensure_config()
        now = datetime.now(timezone.utc)
        self._seed_shadow(_shadow_trade(
            symbol="AAPL.US", entry_price=10.0, exit_price=11.0, qty=100.0,
            entry_day=(now - timedelta(days=4)).date(),
            exit_day=(now - timedelta(days=2)).date(),
            net_pnl=100.0,
        ))
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        assert report["health_status"] == "INSUFFICIENT_DATA"


class TestHealthVerdicts(_Base):
    def _seed_live_trades(self, pnls: list[float], *, per_trade_qty: float = 100.0) -> None:
        """Seed ``len(pnls)`` live round trips each realising ``pnls[i]``.

        Spread across distinct days inside the trailing 30-day window so they
        all fall within the report's from_dt filter.
        """
        self._ensure_config()
        now = datetime.now(timezone.utc)
        for i, pnl in enumerate(pnls):
            exit_day = (now - timedelta(days=20 - i)).date()
            entry_day = (now - timedelta(days=21 - i)).date()
            entry_price = 10.0
            # Realise the requested pnl with the configured per-trade qty and
            # a zero fee rate (set up by _ensure_config).
            exit_price = entry_price + (pnl / per_trade_qty)
            self._seed_live_round_trip(
                oid_buy=f"lb{i}", oid_sell=f"ls{i}", qty=per_trade_qty,
                entry_price=entry_price, exit_price=exit_price,
                entry_day=entry_day, exit_day=exit_day,
            )

    def _seed_shadow_trades(self, pnls: list[float], *, per_trade_qty: float = 100.0) -> None:
        now = datetime.now(timezone.utc)
        for i, pnl in enumerate(pnls):
            exit_day = (now - timedelta(days=20 - i)).date()
            entry_day = (now - timedelta(days=21 - i)).date()
            entry_price = 10.0
            exit_price = entry_price + (pnl / per_trade_qty)
            self._seed_shadow(_shadow_trade(
                symbol="AAPL.US", entry_price=entry_price, exit_price=exit_price,
                qty=per_trade_qty, entry_day=entry_day, exit_day=exit_day,
                net_pnl=pnl,
            ))

    def test_matching_performance_is_healthy(self) -> None:
        # Live and shadow both produce 5 winners of +100 each.
        pnls = [100.0, 100.0, 100.0, 100.0, 100.0]
        self._seed_live_trades(pnls)
        self._seed_shadow_trades(pnls)
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        assert report["health_status"] == "HEALTHY", report["alerts"]
        assert report["drift"]["win_rate_drift"] == 0.0
        # trade_frequency_drift = (5 - 5) / 5 = 0
        assert report["drift"]["trade_frequency_drift"] == 0.0
        assert report["live_metrics"]["win_rate"] == 1.0
        assert report["shadow_metrics"]["win_rate"] == 1.0
        # profit factor with all winners and no losers is the sentinel 1e9.
        assert report["live_metrics"]["profit_factor"] == 1e9

    def test_large_win_rate_drift_is_degraded(self) -> None:
        # Live: all winners (5/5 = 100 %). Shadow: all losers (0/5 = 0 %).
        self._seed_live_trades([100.0] * 5)
        self._seed_shadow_trades([-100.0] * 5)
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        # win_rate_drift = 1.0 - 0.0 = 1.0 -> 100 pp > 30 pp -> DEGRADED.
        assert report["health_status"] == "DEGRADED"
        assert report["drift"]["win_rate_drift"] == 1.0
        assert any("drift" in a.lower() for a in report["alerts"])

    def test_moderate_win_rate_drift_is_warning(self) -> None:
        # Live: 4/5 winners (80 %). Shadow: 3/5 winners (60 %).
        # win_rate_drift = 0.20 -> 20 pp -> WARNING (10 <= 20 < 30).
        self._seed_live_trades([100.0, 100.0, 100.0, 100.0, -50.0])
        self._seed_shadow_trades([100.0, 100.0, 100.0, -50.0, -50.0])
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        assert report["health_status"] == "WARNING"
        assert abs(report["drift"]["win_rate_drift"] - 0.20) < 1e-6

    def test_drift_metrics_are_signed_correctly(self) -> None:
        # Live: 5 winners avg +100. Shadow: 5 winners avg +50.
        self._seed_live_trades([100.0] * 5)
        self._seed_shadow_trades([50.0] * 5)
        report = StrategyHealthService(self._db()).get_health_report(symbol="AAPL.US")
        # win rates equal (both 100 %), avg pnl drift = 100 - 50 = +50.
        assert report["drift"]["win_rate_drift"] == 0.0
        assert report["drift"]["pnl_drift"] == 50.0
        # Healthy because win rate and trade frequency match.
        assert report["health_status"] == "HEALTHY"


class TestPerformanceTrend(_Base):
    def test_trend_buckets_weekly_and_orders_chronologically(self) -> None:
        self._ensure_config()
        # One live winner and one shadow loser inside the current week.
        now = datetime.now(timezone.utc)
        exit_day = now.date()
        self._seed_live_round_trip(
            oid_buy="lb", oid_sell="ls", qty=100.0, entry_price=10.0,
            exit_price=11.0, entry_day=exit_day - timedelta(days=2),
            exit_day=exit_day,
        )
        self._seed_shadow(_shadow_trade(
            symbol="AAPL.US", entry_price=10.0, exit_price=9.0, qty=100.0,
            entry_day=exit_day - timedelta(days=2),
            exit_day=exit_day, net_pnl=-100.0,
        ))
        rows = StrategyHealthService(self._db()).get_performance_trend(
            symbol="AAPL.US", weeks=4
        )
        assert len(rows) == 4
        # Chronological order by week_start.
        week_starts = [r["week_start"] for r in rows]
        assert week_starts == sorted(week_starts)
        # The current (last) week should contain both trades.
        current = rows[-1]
        assert current["live_trades"] == 1
        assert current["shadow_trades"] == 1
        assert current["live_win_rate"] == 1.0
        assert current["shadow_win_rate"] == 0.0
        # Older weeks are empty.
        assert rows[0]["live_trades"] == 0
        assert rows[0]["shadow_trades"] == 0

    def test_trend_endpoint_returns_rows(self) -> None:
        self._ensure_config()
        resp = self.client.get("/api/strategy-health/trend", params={"weeks": 3})
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 3
        for row in rows:
            assert set(row.keys()) >= {
                "week_start", "live_win_rate", "shadow_win_rate",
                "live_avg_pnl", "shadow_avg_pnl", "live_trades", "shadow_trades",
            }
