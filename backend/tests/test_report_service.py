from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone

os.makedirs("data", exist_ok=True)
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///data/test_report_service_{os.getpid()}.db"

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, LLMInteraction, OrderRecord
from app.services.report_service import ReportService


class TestReportService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(LLMInteraction).delete()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def _seed_rows(self) -> None:
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="aapl-buy-1",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 1), 14),
                filled_at=self._dt(date(2026, 1, 1), 14, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-sell-1",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 1), 15),
                filled_at=self._dt(date(2026, 1, 1), 15, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-buy-2",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=120,
                executed_quantity=10,
                executed_price=120,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 2), 14),
                filled_at=self._dt(date(2026, 1, 2), 14, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-sell-2",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=115,
                executed_quantity=10,
                executed_price=115,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 2), 15),
                filled_at=self._dt(date(2026, 1, 2), 15, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-buy-3",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 3), 14),
                filled_at=self._dt(date(2026, 1, 3), 14, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-sell-3",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=206,
                executed_quantity=1,
                executed_price=206,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 3), 15),
                filled_at=self._dt(date(2026, 1, 3), 15, 1),
            ),
            OrderRecord(
                broker_order_id="msft-buy",
                symbol="MSFT.US",
                side="BUY",
                quantity=10,
                price=50,
                executed_quantity=10,
                executed_price=50,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 2), 10),
                filled_at=self._dt(date(2026, 1, 2), 10, 1),
            ),
            OrderRecord(
                broker_order_id="msft-sell",
                symbol="MSFT.US",
                side="SELL",
                quantity=10,
                price=60,
                executed_quantity=10,
                executed_price=60,
                status="FILLED",
                created_at=self._dt(date(2026, 1, 2), 11),
                filled_at=self._dt(date(2026, 1, 2), 11, 1),
            ),
            LLMInteraction(
                symbol="AAPL.US",
                applied=True,
                order_action="BUY",
                created_at=self._dt(date(2026, 1, 2), 9),
            ),
            LLMInteraction(
                symbol="AAPL.US",
                applied=False,
                order_action="SELL",
                created_at=self._dt(date(2026, 1, 3), 9),
            ),
            LLMInteraction(
                symbol="MSFT.US",
                applied=True,
                order_action="BUY",
                created_at=self._dt(date(2026, 1, 2), 9),
            ),
        ])
        db.commit()
        db.close()

    def test_empty_range_returns_zero_metrics_and_empty_arrays(self) -> None:
        self._cleanup()
        db = self._get_db()
        service = ReportService(db=db)
        try:
            report = service.get_range_report("AAPL.US", "2026-02-01", "2026-02-03")

            assert report.daily_points == []
            assert report.attribution == []
            assert report.details == []
            assert report.metrics.total_pnl == 0.0
            assert report.metrics.total_trades == 0
            assert report.metrics.win_count == 0
            assert report.metrics.loss_count == 0
            assert report.metrics.win_rate == 0.0
            assert report.metrics.avg_pnl_per_trade == 0.0
            assert report.metrics.max_profit == 0.0
            assert report.metrics.max_loss == 0.0
            assert report.metrics.max_drawdown == 0.0
            assert report.metrics.llm_suggestions_count == 0
            assert report.metrics.llm_applied_count == 0
            assert report.metrics.llm_apply_rate == 0.0
            assert report.metrics.llm_profitable_count == 0
            assert report.metrics.llm_accuracy_rate == 0.0
        finally:
            db.close()

    def test_range_report_rejects_unsupported_market_suffix(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        service = ReportService(db=db)
        try:
            try:
                service.get_range_report("7203.JP", "2026-01-01", "2026-01-03")
                raise AssertionError("expected ValueError")
            except ValueError as exc:
                assert str(exc) == "symbol market must be US or HK"
        finally:
            db.close()

    def test_core_metrics_over_three_days(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        service = ReportService(db=db)
        try:
            report = service.get_range_report("AAPL.US", "2026-01-01", "2026-01-03")

            assert report.details == []
            assert report.metrics.total_pnl == 56.0
            assert report.metrics.total_trades == 3
            assert report.metrics.win_count == 2
            assert report.metrics.loss_count == 1
            assert report.metrics.win_rate == 0.6667
            assert report.metrics.avg_pnl_per_trade == 18.67
            assert report.metrics.max_profit == 100.0
            assert report.metrics.max_loss == -50.0
            assert report.metrics.profit_loss_ratio == 1.06
            assert report.metrics.max_drawdown == 50.0
            assert report.metrics.llm_suggestions_count == 2
            assert report.metrics.llm_applied_count == 1
            assert report.metrics.llm_apply_rate == 0.5
            assert report.metrics.llm_profitable_count == 0
            assert report.metrics.llm_accuracy_rate == 0.0
            assert [p.pnl for p in report.daily_points] == [100.0, -50.0, 6.0]
            assert [p.trade_count for p in report.daily_points] == [1, 1, 1]
            assert [
                {
                    "date": p.date,
                    "pnl": p.pnl,
                    "cumulative_pnl": p.cumulative_pnl,
                    "drawdown": p.drawdown,
                }
                for p in report.daily_points
            ] == [
                {"date": "2026-01-01", "pnl": 100.0, "cumulative_pnl": 100.0, "drawdown": 0.0},
                {"date": "2026-01-02", "pnl": -50.0, "cumulative_pnl": 50.0, "drawdown": 50.0},
                {"date": "2026-01-03", "pnl": 6.0, "cumulative_pnl": 56.0, "drawdown": 44.0},
            ]
        finally:
            db.close()

    def test_us_late_utc_fill_is_assigned_to_prior_trade_day(self) -> None:
        self._cleanup()
        db = self._get_db()
        try:
            db.add_all([
                OrderRecord(
                    broker_order_id="aapl-buy-late-utc",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10,
                    price=100,
                    executed_quantity=10,
                    executed_price=100,
                    status="FILLED",
                    created_at=self._dt(date(2026, 5, 22), 14),
                    filled_at=self._dt(date(2026, 5, 22), 14),
                ),
                OrderRecord(
                    broker_order_id="aapl-sell-late-utc",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10,
                    price=110,
                    executed_quantity=10,
                    executed_price=110,
                    status="FILLED",
                    created_at=self._dt(date(2026, 5, 23), 1),
                    filled_at=self._dt(date(2026, 5, 23), 1),
                ),
            ])
            db.commit()

            service = ReportService(db=db)
            report = service.get_daily_report("AAPL.US", "2026-05-22")

            assert report.metrics.total_pnl == 100.0
            assert [p.date for p in report.daily_points] == ["2026-05-22"]
            assert [p.pnl for p in report.daily_points] == [100.0]
        finally:
            db.close()

    def test_multi_day_held_position_continuity_across_range(self) -> None:
        self._cleanup()
        db = self._get_db()
        try:
            db.add_all([
                OrderRecord(
                    broker_order_id="aapl-buy-held",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10,
                    price=100,
                    executed_quantity=10,
                    executed_price=100,
                    status="FILLED",
                    created_at=self._dt(date(2026, 1, 1), 14),
                    filled_at=self._dt(date(2026, 1, 1), 14, 1),
                ),
                OrderRecord(
                    broker_order_id="aapl-sell-held",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10,
                    price=103,
                    executed_quantity=10,
                    executed_price=103,
                    status="FILLED",
                    created_at=self._dt(date(2026, 1, 3), 15),
                    filled_at=self._dt(date(2026, 1, 3), 15, 1),
                ),
            ])
            db.commit()

            service = ReportService(db=db)
            report = service.get_range_report("AAPL.US", "2026-01-01", "2026-01-03")

            assert report.metrics.total_trades == 1
            assert report.metrics.total_pnl == 30.0
            assert [p.date for p in report.daily_points] == ["2026-01-03"]
            assert [p.cumulative_pnl for p in report.daily_points] == [30.0]
        finally:
            db.close()

    def test_range_report_excludes_trade_at_to_date_midnight_next_day(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        try:
            db.add_all([
                OrderRecord(
                    broker_order_id="aapl-buy-boundary",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10,
                    price=100,
                    executed_quantity=10,
                    executed_price=100,
                    status="FILLED",
                    created_at=self._dt(date(2026, 1, 4), 0),
                    filled_at=self._dt(date(2026, 1, 4), 0, 1),
                ),
                OrderRecord(
                    broker_order_id="aapl-sell-boundary",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10,
                    price=130,
                    executed_quantity=10,
                    executed_price=130,
                    status="FILLED",
                    created_at=self._dt(date(2026, 1, 4), 0),
                    filled_at=self._dt(date(2026, 1, 4), 0, 2),
                ),
            ])
            db.commit()

            service = ReportService(db=db)
            report = service.get_range_report("AAPL.US", "2026-01-01", "2026-01-03")

            assert report.metrics.total_trades == 4
            assert report.metrics.total_pnl == 356.0
            assert [p.date for p in report.daily_points] == ["2026-01-01", "2026-01-02", "2026-01-03"]
            assert all(p.date != "2026-01-04" for p in report.daily_points)
        finally:
            db.close()

    def test_export_report_json_and_csv(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        try:
            service = ReportService(db=db)

            json_bytes = service.export_report("AAPL.US", "2026-01-01", "2026-01-03", "json")
            json_report = json.loads(json_bytes.getvalue().decode("utf-8"))
            assert json_report["metrics"]["max_drawdown"] == 50.0
            assert json_report["daily_points"] == [
                {"date": "2026-01-01", "pnl": 100.0, "cumulative_pnl": 100.0, "drawdown": 0.0, "trade_count": 1, "win_count": 1},
                {"date": "2026-01-02", "pnl": -50.0, "cumulative_pnl": 50.0, "drawdown": 50.0, "trade_count": 1, "win_count": 0},
                {"date": "2026-01-03", "pnl": 6.0, "cumulative_pnl": 56.0, "drawdown": 44.0, "trade_count": 1, "win_count": 1},
            ]

            csv_bytes = service.export_report("AAPL.US", "2026-01-01", "2026-01-03", "csv")
            csv_header = csv_bytes.getvalue().decode("utf-8").splitlines()[0]
            assert csv_header == "date,symbol,trade_count,win_count,pnl,cumulative_pnl,drawdown"
        finally:
            db.close()

    def test_export_report_rejects_invalid_format(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        try:
            service = ReportService(db=db)

            try:
                service.export_report("AAPL.US", "2026-01-01", "2026-01-03", "xml")
                assert False, "expected ValueError"
            except ValueError as exc:
                assert str(exc) == "format must be json or csv"
        finally:
            db.close()

    def test_symbol_is_normalized_and_invalid_symbol_rejected(self) -> None:
        self._cleanup()
        self._seed_rows()
        db = self._get_db()
        try:
            service = ReportService(db=db)

            report = service.get_range_report(" aapl.us ", "2026-01-01", "2026-01-03")
            assert report.symbol == "AAPL.US"

            try:
                service.get_range_report("AAPL", "2026-01-01", "2026-01-03")
                assert False, "expected ValueError"
            except ValueError as exc:
                assert str(exc) == "symbol market must be US or HK"
        finally:
            db.close()
