from __future__ import annotations

from datetime import date, datetime, time, timezone

from pytest import approx

from app import database
from app.models import OrderRecord
from app.services.daily_pnl_service import DailyPnlService


database.init_db()


class TestPairRoundTrips:
    """Closed round-trip pairing (entry <-> exit) via DailyPnlService.pair_round_trips.

    Pure read-only FIFO lot ledger; does not touch calculate() / _apply_fill.
    """

    def _get_db(self):
        return database.SessionLocal()

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def test_empty_returns_no_trades(self) -> None:
        self._cleanup()
        db = self._get_db()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()
        assert trades == []

    def test_unclosed_position_not_emitted(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="buy-open", symbol="AAPL.US", side="BUY",
            quantity=10, price=100, executed_quantity=10, executed_price=100,
            status="FILLED", created_at=self._dt(day, 14), filled_at=self._dt(day, 14, 1),
        ))
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()
        assert trades == []

    def test_long_round_trip(self) -> None:
        self._cleanup()
        buy_day = date(2026, 1, 1)
        sell_day = date(2026, 1, 5)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(buy_day, 14), filled_at=self._dt(buy_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=100, price=12, executed_quantity=100, executed_price=12,
                status="FILLED", created_at=self._dt(sell_day, 15), filled_at=self._dt(sell_day, 15, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == "AAPL.US"
        assert t.side == "long"
        assert t.entry_price == approx(10.0)
        assert t.exit_price == approx(12.0)
        assert t.quantity == approx(100.0)
        assert t.gross_pnl == approx(200.0)
        assert t.entry_order_id != t.exit_order_id
        assert t.exit_at > t.entry_at
        # 4 days minus ~1 minute (fills at :01)
        assert t.holding_seconds == approx((self._dt(sell_day, 15, 1) - self._dt(buy_day, 14, 1)).total_seconds())

    def test_short_round_trip(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="short", symbol="TSLA.US", side="SELL_SHORT",
                quantity=100, price=20, executed_quantity=100, executed_price=20,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="cover", symbol="TSLA.US", side="BUY_TO_COVER",
                quantity=100, price=18, executed_quantity=100, executed_price=18,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert len(trades) == 1
        t = trades[0]
        assert t.side == "short"
        assert t.entry_price == approx(20.0)
        assert t.exit_price == approx(18.0)
        assert t.gross_pnl == approx(200.0)

    def test_partial_close_leaves_open_position(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=60, price=12, executed_quantity=60, executed_price=12,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert len(trades) == 1
        assert trades[0].quantity == approx(60.0)
        assert trades[0].gross_pnl == approx(120.0)  # (12-10)*60

    def test_multiple_entry_lots_weighted_average(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-1", symbol="AAPL.US", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="buy-2", symbol="AAPL.US", side="BUY",
                quantity=100, price=11, executed_quantity=100, executed_price=11,
                status="FILLED", created_at=self._dt(day, 10, 30), filled_at=self._dt(day, 10, 31),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=150, price=13, executed_quantity=150, executed_price=13,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert len(trades) == 1
        t = trades[0]
        avg_entry = (100 * 10 + 50 * 11) / 150  # 10.3333...
        assert t.entry_price == approx(avg_entry)
        assert t.quantity == approx(150.0)
        assert t.gross_pnl == approx((13 - avg_entry) * 150)

    def test_net_pnl_subtracts_estimated_fees(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=100, price=12, executed_quantity=100, executed_price=12,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips(fee_rate_us=0.001, fee_rate_hk=0.003)
        db.close()

        t = trades[0]
        expected_fee = (10 + 12) * 100 * 0.001  # 2.2
        assert t.est_fees == approx(expected_fee)
        assert t.net_pnl == approx(200.0 - expected_fee)

    def test_hk_symbol_uses_hk_fee_rate(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy", symbol="0700.HK", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="0700.HK", side="SELL",
                quantity=100, price=12, executed_quantity=100, executed_price=12,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips(fee_rate_us=0.001, fee_rate_hk=0.005)
        db.close()

        # HK symbol -> one_side_rate = fee_rate_hk = 0.005
        expected_fee = (10 + 12) * 100 * 0.005  # 11.0
        assert trades[0].est_fees == approx(expected_fee)

    def test_symbol_filter_isolates_one_symbol(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="a-buy", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=10, executed_price=100,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="a-sell", symbol="AAPL.US", side="SELL",
                quantity=10, price=110, executed_quantity=10, executed_price=110,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
            OrderRecord(
                broker_order_id="t-buy", symbol="TSLA.US", side="BUY",
                quantity=10, price=200, executed_quantity=10, executed_price=200,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="t-sell", symbol="TSLA.US", side="SELL",
                quantity=10, price=190, executed_quantity=10, executed_price=190,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips(symbol="aapl.us")
        db.close()

        assert len(trades) == 1
        assert trades[0].symbol == "AAPL.US"
        assert trades[0].gross_pnl == approx(100.0)

    def test_date_filter_on_exit_time(self) -> None:
        self._cleanup()
        early = date(2026, 1, 1)
        late = date(2026, 2, 1)
        db = self._get_db()
        for i, sell_day in enumerate((early, late)):
            db.add(OrderRecord(
                broker_order_id=f"buy-{i}", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=10, executed_price=100,
                status="FILLED", created_at=self._dt(sell_day, 9), filled_at=self._dt(sell_day, 9, 1),
            ))
            db.add(OrderRecord(
                broker_order_id=f"sell-{i}", symbol="AAPL.US", side="SELL",
                quantity=10, price=110, executed_quantity=10, executed_price=110,
                status="FILLED", created_at=self._dt(sell_day, 11), filled_at=self._dt(sell_day, 11, 1),
            ))
        db.commit()

        svc = DailyPnlService(db)
        # Window covering only the late exit.
        only_late = svc.pair_round_trips(
            from_dt=self._dt(date(2026, 1, 15), 0),
            to_dt=self._dt(date(2026, 2, 28), 0),
        )
        assert len(only_late) == 1
        assert only_late[0].exit_at.date() == late

        all_trades = svc.pair_round_trips()
        db.close()
        assert len(all_trades) == 2

    def test_ordering_by_exit_time_ascending(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-1", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=10, executed_price=100,
                status="FILLED", created_at=self._dt(day, 9), filled_at=self._dt(day, 9, 1),
            ),
            OrderRecord(
                broker_order_id="buy-2", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=10, executed_price=100,
                status="FILLED", created_at=self._dt(day, 9, 5), filled_at=self._dt(day, 9, 6),
            ),
            OrderRecord(
                broker_order_id="sell-2", symbol="AAPL.US", side="SELL",
                quantity=10, price=110, executed_quantity=10, executed_price=110,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell-1", symbol="AAPL.US", side="SELL",
                quantity=10, price=111, executed_quantity=10, executed_price=111,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert [t.exit_order_id for t in trades] == [trades[0].exit_order_id, trades[1].exit_order_id]
        assert trades[0].exit_at < trades[1].exit_at

    def test_dedupes_duplicate_broker_order_rows(self) -> None:
        """Legacy duplicate rows stay defensively deduplicated on read."""
        day = date(2026, 1, 1)
        rows = [
            OrderRecord(
                id=1, broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=0, executed_price=None,
                status="SUBMITTED", created_at=self._dt(day, 9), filled_at=None,
            ),
            OrderRecord(
                id=2, broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=10, price=100, executed_quantity=10, executed_price=100,
                status="FILLED", created_at=self._dt(day, 9, 1), filled_at=self._dt(day, 9, 2),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=10, price=110, executed_quantity=10, executed_price=110,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ]

        class _RowsQuery:
            def all(self):
                return rows

        class _RowsDb:
            def query(self, _model):
                return _RowsQuery()

        trades = DailyPnlService(_RowsDb()).pair_round_trips()

        assert len(trades) == 1
        assert trades[0].gross_pnl == approx(100.0)

    def test_over_close_logs_warning(self, caplog) -> None:
        """A close exceeding available entry lots is truncated to the matched
        quantity and warns (parity with calculate()/_close_long)."""
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy", symbol="AAPL.US", side="BUY",
                quantity=100, price=10, executed_quantity=100, executed_price=10,
                status="FILLED", created_at=self._dt(day, 10), filled_at=self._dt(day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell", symbol="AAPL.US", side="SELL",
                quantity=200, price=12, executed_quantity=200, executed_price=12,
                status="FILLED", created_at=self._dt(day, 11), filled_at=self._dt(day, 11, 1),
            ),
        ])
        db.commit()
        import logging
        caplog.set_level(logging.WARNING)
        trades = DailyPnlService(db).pair_round_trips()
        db.close()

        assert len(trades) == 1
        assert trades[0].quantity == approx(100.0)  # only the matched entry quantity
        assert any("exceeds matched entry lots" in rec.message for rec in caplog.records)
