from __future__ import annotations

from datetime import date, datetime, time, timezone

from pytest import approx
from pytest import LogCaptureFixture

from app import database
from app.models import OrderRecord, RuntimeStateSnapshot
from app.services.daily_pnl_service import DailyPnlService


database.init_db()


class TestDailyPnlService:
    def _get_db(self):
        return database.SessionLocal()

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(OrderRecord).delete()
        db.query(RuntimeStateSnapshot).delete()
        db.commit()
        db.close()

    def test_prefers_persisted_actual_fees_and_computes_excursions(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 11)
        entry_at = self._dt(trade_day, 10)
        exit_at = self._dt(trade_day, 11)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="actual-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=1.0,
                estimated_fee=0.5,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ),
            OrderRecord(
                broker_order_id="actual-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=2.0,
                estimated_fee=0.55,
                fee_source="ACTUAL",
                slippage_bps=1.5,
                exit_cause="TIME_STOP",
                status="FILLED",
                created_at=exit_at,
                filled_at=exit_at,
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=115,
                created_at=self._dt(trade_day, 10, 30),
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=97,
                created_at=self._dt(trade_day, 10, 45),
            ),
        ])
        db.commit()

        trip = DailyPnlService(db).pair_round_trips()[0]

        assert trip.fee_source == "ACTUAL"
        assert trip.est_fees == approx(3.0)
        assert trip.net_pnl == approx(97.0)
        assert trip.mfe_pct == approx(15.0)
        assert trip.mae_pct == approx(-3.0)
        assert trip.exit_cause == "TIME_STOP"
        db.close()

    def test_persisted_estimate_does_not_change_with_active_fee_rate(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 11)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="frozen-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                estimated_fee=1.0,
                fee_source="ESTIMATED",
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="frozen-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                estimated_fee=2.0,
                fee_source="ESTIMATED",
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()

        low_rate = DailyPnlService(db).pair_round_trips(fee_rate_us=0.0001)[0]
        high_rate = DailyPnlService(db).pair_round_trips(fee_rate_us=0.1)[0]

        assert low_rate.net_pnl == high_rate.net_pnl == approx(97.0)
        assert low_rate.fee_source == "ESTIMATED"
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def test_calculates_today_pnl_using_carryover_cost_basis(self) -> None:
        self._cleanup()
        prior_day = date(2026, 5, 21)
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-prior",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(prior_day, 14),
                filled_at=self._dt(prior_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="sell-today",
                symbol="NVDA.US",
                side="SELL",
                quantity=4,
                price=110,
                executed_quantity=4,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(trade_day, 14),
                filled_at=self._dt(trade_day, 14, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(40.0 - (4 * 100 + 4 * 110) * 0.0005)
        assert result.consecutive_losses == 0
        assert [(trade.broker_order_id, trade.pnl) for trade in result.trades] == [
            ("sell-today", approx(40.0 - (4 * 100 + 4 * 110) * 0.0005))
        ]

    def test_calculates_long_held_position_realized_pnl(self) -> None:
        self._cleanup()
        db = self._get_db()
        buy_day = date(2026, 1, 1)
        sell_day = date(2026, 1, 5)
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
                created_at=self._dt(buy_day, 14),
                filled_at=self._dt(buy_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="aapl-sell-held",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=105,
                executed_quantity=10,
                executed_price=105,
                status="FILLED",
                created_at=self._dt(sell_day, 15),
                filled_at=self._dt(sell_day, 15, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=sell_day)
        db.close()

        assert result.realized_pnl == approx(50.0 - (10 * 100 + 10 * 105) * 0.0005)
        assert [trade.broker_order_id for trade in result.trades] == ["aapl-sell-held"]
        assert result.trades[0].pnl == approx(50.0 - (10 * 100 + 10 * 105) * 0.0005)

    def test_calculates_average_cost_for_same_day_round_trip(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-1",
                symbol="NVDA.US",
                side="BUY",
                quantity=105,
                price=220.15,
                executed_quantity=105,
                executed_price=220.15,
                status="FILLED",
                created_at=self._dt(trade_day, 12, 32),
                filled_at=self._dt(trade_day, 12, 33),
            ),
            OrderRecord(
                broker_order_id="buy-2",
                symbol="NVDA.US",
                side="BUY",
                quantity=16,
                price=219.51,
                executed_quantity=16,
                executed_price=219.51,
                status="FILLED",
                created_at=self._dt(trade_day, 13, 30),
                filled_at=self._dt(trade_day, 13, 31),
            ),
            OrderRecord(
                broker_order_id="sell-1",
                symbol="NVDA.US",
                side="SELL",
                quantity=121,
                price=217.53,
                executed_quantity=121,
                executed_price=217.530909,
                status="FILLED",
                created_at=self._dt(trade_day, 15),
                filled_at=self._dt(trade_day, 15, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        avg_cost = ((105 * 220.15) + (16 * 219.51)) / 121
        assert result.realized_pnl == approx(
            (217.530909 - avg_cost) * 121
            - ((105 * 220.15) + (16 * 219.51) + (121 * 217.530909)) * 0.0005
        )
        assert result.consecutive_losses == 1

    def test_counts_executed_quantity_on_partially_filled_terminal_order(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=5,
                price=100,
                executed_quantity=5,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell-cancelled-partial",
                symbol="AAPL.US",
                side="SELL",
                quantity=5,
                price=101,
                executed_quantity=3,
                executed_price=101,
                status="CANCELLED",
                created_at=self._dt(trade_day, 11),
                filled_at=self._dt(trade_day, 11, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(3.0 - (3 * 100 + 3 * 101) * 0.0005)
        assert result.consecutive_losses == 0

    def test_market_aware_trade_day_keeps_after_hours_fill_on_session_day(self) -> None:
        """A US fill at 22:30 UTC = 18:30 ET still belongs to that session day."""
        self._cleanup()
        from app.core.market_calendar import trade_day_for

        session_day = date(2026, 5, 22)
        # UTC date for filled_at is 2026-05-22, but happens AFTER local RTH close
        after_close = datetime(2026, 5, 22, 22, 30, tzinfo=timezone.utc)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-rth",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(session_day, 14),
                filled_at=self._dt(session_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="sell-after-hours",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                status="FILLED",
                created_at=after_close,
                filled_at=after_close,
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=session_day,
            to_trade_day=lambda dt: trade_day_for("US", dt),
        )
        db.close()

        assert result.realized_pnl == approx(100.0 - (10 * 100 + 10 * 110) * 0.0005)
        assert any(t.broker_order_id == "sell-after-hours" for t in result.trades)

    def test_market_aware_trade_day_keeps_late_utc_us_fill_on_previous_session(self) -> None:
        """01:00 UTC 2026-05-23 = 21:00 ET 2026-05-22 must count toward 2026-05-22 session."""
        self._cleanup()
        from app.core.market_calendar import trade_day_for

        session_day = date(2026, 5, 22)
        late_utc = datetime(2026, 5, 23, 1, 0, tzinfo=timezone.utc)  # 21:00 ET 5-22
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=5,
                price=100,
                executed_quantity=5,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(session_day, 14),
                filled_at=self._dt(session_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="sell-late",
                symbol="AAPL.US",
                side="SELL",
                quantity=5,
                price=104,
                executed_quantity=5,
                executed_price=104,
                status="FILLED",
                created_at=late_utc,
                filled_at=late_utc,
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=session_day,
            to_trade_day=lambda dt: trade_day_for("US", dt),
        )
        db.close()

        assert result.realized_pnl == approx(20.0 - (5 * 100 + 5 * 104) * 0.0005)

    def test_calculates_short_cover_pnl(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="short",
                symbol="TSLA.US",
                side="SELL_SHORT",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="cover",
                symbol="TSLA.US",
                side="BUY_TO_COVER",
                quantity=10,
                price=95,
                executed_quantity=10,
                executed_price=95,
                status="FILLED",
                created_at=self._dt(trade_day, 11),
                filled_at=self._dt(trade_day, 11, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(50.0 - (10 * 100 + 10 * 95) * 0.0005)
        assert result.consecutive_losses == 0

    def test_executed_price_fallback_logs_warning(self, caplog: LogCaptureFixture) -> None:
        """G1-2: _executed_price logs warning when falling back to limit price."""
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-no-exec-price",
                symbol="AAPL.US",
                side="BUY",
                quantity=5,
                price=100,
                executed_quantity=5,
                executed_price=None,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
        ])
        db.commit()

        import logging
        caplog.set_level(logging.WARNING)
        _ = DailyPnlService(db).calculate(trade_day=trade_day)
        _ = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        records = [
            rec
            for rec in caplog.records
            if "has no executed_price, falling back to limit price" in rec.message
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert not any(
            rec.levelno >= logging.ERROR
            for rec in caplog.records
        )

    def test_unclosed_remainder_logs_warning(self, caplog: LogCaptureFixture) -> None:
        """G1-3: _apply_fill logs warning when close exceeds tracked position."""
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="sell-without-holding",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
        ])
        db.commit()

        import logging
        caplog.set_level(logging.WARNING)
        _ = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert any(
            "close quantity exceeds tracked position by" in rec.message
            for rec in caplog.records
        )

    def test_unclosed_remainder_warning_is_logged_once(self, caplog: LogCaptureFixture) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="sell-without-holding-once",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
        ])
        db.commit()

        import logging
        caplog.set_level(logging.WARNING)
        _ = DailyPnlService(db).calculate(trade_day=trade_day)
        _ = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        records = [
            rec
            for rec in caplog.records
            if "close quantity exceeds tracked position by" in rec.message
        ]
        assert len(records) == 1

    def test_round_trip_overclose_warning_is_logged_once(self, caplog: LogCaptureFixture) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-before-overclose",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell-overclose-once",
                symbol="AAPL.US",
                side="SELL",
                quantity=12,
                price=110,
                executed_quantity=12,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(trade_day, 11),
                filled_at=self._dt(trade_day, 11, 1),
            ),
        ])
        db.commit()

        import logging
        caplog.set_level(logging.WARNING)
        _ = DailyPnlService(db).pair_round_trips()
        _ = DailyPnlService(db).pair_round_trips()
        db.close()

        records = [
            rec
            for rec in caplog.records
            if "round-trip close of" in rec.message
        ]
        assert len(records) == 1
