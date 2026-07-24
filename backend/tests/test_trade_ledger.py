from __future__ import annotations

from datetime import date, datetime, time, timezone

from pytest import approx

from app import database
from app.models import OrderRecord
from app.services.daily_pnl_service import DailyPnlService, PnlReplayIssueCode


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
        """A partially matched exit is an issue, never a closed trade subset."""
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
        service = DailyPnlService(db)
        replay = service.pair_round_trips_with_issues()
        backwards_compatible_trades = service.pair_round_trips()
        db.close()

        assert replay.trades == []
        assert backwards_compatible_trades == []
        assert len(replay.issues) == 1
        issue = replay.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.PARTIAL_OVERCLOSE
        assert issue.symbol == "AAPL.US"
        assert issue.trade_day == day
        assert issue.exit_broker_order_id == "sell"
        assert issue.filled_quantity == approx(200)
        assert issue.matched_quantity == approx(100)
        assert issue.unmatched_quantity == approx(100)
        assert any("exceeds matched entry lots" in rec.message for rec in caplog.records)

    def test_full_unmatched_exit_is_a_structured_issue(self) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        exit_order = OrderRecord(
            broker_order_id="unmatched-cover",
            symbol="TSLA.US",
            side="BUY_TO_COVER",
            quantity=25,
            price=18,
            executed_quantity=25,
            executed_price=18,
            status="FILLED",
            filled_at=self._dt(day, 11, 1),
        )
        db.add(exit_order)
        db.commit()

        replay = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False
        )

        assert replay.trades == []
        assert len(replay.issues) == 1
        issue = replay.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.FULL_UNMATCHED_EXIT
        assert issue.exit_order_id == exit_order.id
        assert issue.side == "BUY_TO_COVER"
        assert issue.filled_quantity == approx(25)
        assert issue.matched_quantity == 0
        assert issue.unmatched_quantity == approx(25)
        db.close()

    def test_tracked_cost_basis_conflict_is_an_issue_and_advances_lots(
        self,
    ) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="known-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 10),
            ),
            OrderRecord(
                broker_order_id="conflicting-tracked-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 11),
                cost_basis_price=90,
                cost_basis_quantity=10,
                position_quantity_before=10,
                gross_pnl=200,
                pnl_fee=0,
                net_pnl=200,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="fresh-buy-after-conflict",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 12),
            ),
            OrderRecord(
                broker_order_id="fresh-sell-after-conflict",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=210,
                executed_quantity=1,
                executed_price=210,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 13),
            ),
        ])
        db.commit()

        replay = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False
        )

        assert [trade.exit_broker_order_id for trade in replay.trades] == [
            "fresh-sell-after-conflict"
        ]
        assert replay.trades[0].gross_pnl == approx(10)
        assert len(replay.issues) == 1
        issue = replay.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.COST_BASIS_CONFLICT
        assert issue.exit_broker_order_id == "conflicting-tracked-sell"
        assert issue.filled_quantity == approx(10)
        assert issue.matched_quantity == approx(10)
        assert issue.unmatched_quantity == 0
        db.close()

    def test_authoritative_inventory_reset_is_excluded_from_statistics(
        self,
    ) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="stale-local-inventory",
                symbol="AAPL.US",
                side="BUY",
                quantity=12,
                price=95,
                executed_quantity=12,
                executed_price=95,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 10),
            ),
            OrderRecord(
                broker_order_id="authoritative-reset-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 11),
                cost_basis_price=90,
                cost_basis_quantity=10,
                position_quantity_before=10,
                gross_pnl=100,
                pnl_fee=0,
                net_pnl=100,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="fresh-buy-after-reset",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 12),
            ),
            OrderRecord(
                broker_order_id="fresh-sell-after-reset",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=210,
                executed_quantity=1,
                executed_price=210,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 13),
            ),
        ])
        db.commit()

        replay = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False
        )

        assert [trade.exit_broker_order_id for trade in replay.trades] == [
            "fresh-sell-after-reset"
        ]
        assert replay.trades[0].gross_pnl == approx(10)
        assert len(replay.issues) == 1
        issue = replay.issues[0]
        assert (
            issue.issue_code
            is PnlReplayIssueCode.UNVERIFIED_COST_BASIS
        )
        assert issue.exit_broker_order_id == "authoritative-reset-sell"
        assert issue.filled_quantity == approx(10)
        assert issue.matched_quantity == approx(10)
        assert issue.unmatched_quantity == 0
        db.close()

    def test_malformed_authoritative_reset_is_excluded_from_statistics(
        self,
    ) -> None:
        self._cleanup()
        day = date(2026, 1, 1)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="stale-inventory-before-malformed-reset",
                symbol="AAPL.US",
                side="BUY",
                quantity=12,
                price=95,
                executed_quantity=12,
                executed_price=95,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 10),
            ),
            OrderRecord(
                broker_order_id="malformed-authoritative-reset",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 11),
                cost_basis_price=90,
                cost_basis_quantity=10,
                position_quantity_before=10,
                gross_pnl=100,
                pnl_fee=0,
                net_pnl=-100,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="fresh-buy-after-malformed-reset",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 12),
            ),
            OrderRecord(
                broker_order_id="fresh-sell-after-malformed-reset",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=210,
                executed_quantity=1,
                executed_price=210,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(day, 13),
            ),
        ])
        db.commit()

        replay = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False
        )

        assert [trade.exit_broker_order_id for trade in replay.trades] == [
            "fresh-sell-after-malformed-reset"
        ]
        assert replay.trades[0].gross_pnl == approx(10)
        assert len(replay.issues) == 1
        assert (
            replay.issues[0].issue_code
            is PnlReplayIssueCode.UNVERIFIED_COST_BASIS
        )
        assert (
            replay.issues[0].exit_broker_order_id
            == "malformed-authoritative-reset"
        )
        db.close()

    def test_partial_malformed_short_reset_does_not_validate_residual_lot(
        self,
    ) -> None:
        self._cleanup()
        reset_day = date(2026, 1, 1)
        residual_exit_day = date(2026, 1, 2)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="stale-short-before-partial-reset",
                symbol="AAPL.US",
                side="SELL_SHORT",
                quantity=12,
                price=105,
                executed_quantity=12,
                executed_price=105,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(reset_day, 10),
            ),
            OrderRecord(
                broker_order_id="malformed-partial-short-reset",
                symbol="AAPL.US",
                side="BUY_TO_COVER",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(reset_day, 11),
                cost_basis_price=110,
                cost_basis_quantity=10,
                position_quantity_before=20,
                gross_pnl=100,
                pnl_fee=0,
                net_pnl=-100,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="unverified-residual-cover",
                symbol="AAPL.US",
                side="BUY_TO_COVER",
                quantity=10,
                price=50,
                executed_quantity=10,
                executed_price=50,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(residual_exit_day, 11),
            ),
        ])
        db.commit()

        service = DailyPnlService(db)
        reset_result = service.calculate(
            trade_day=reset_day,
            symbol="AAPL.US",
        )
        residual_result = service.calculate(
            trade_day=residual_exit_day,
            symbol="AAPL.US",
        )
        replay = service.pair_round_trips_with_issues(
            include_excursions=False,
        )

        assert reset_result.is_complete is False
        assert reset_result.realized_pnl == 0
        assert DailyPnlService.reconcile_risk_state(
            -25.0,
            2,
            reset_day,
            reset_result,
        ) == (-25.0, 2)
        assert residual_result.is_complete is False
        assert residual_result.realized_pnl == 0
        assert residual_result.trades == []
        assert len(residual_result.issues) == 1
        assert (
            residual_result.issues[0].issue_code
            is PnlReplayIssueCode.FULL_UNMATCHED_EXIT
        )
        assert replay.trades == []
        assert [
            issue.issue_code
            for issue in replay.issues
        ] == [
            PnlReplayIssueCode.UNVERIFIED_COST_BASIS,
            PnlReplayIssueCode.FULL_UNMATCHED_EXIT,
        ]
        assert [
            issue.exit_broker_order_id
            for issue in replay.issues
        ] == [
            "malformed-partial-short-reset",
            "unverified-residual-cover",
        ]
        db.close()

    def test_issue_trade_day_is_resolved_per_symbol_market(self) -> None:
        self._cleanup()
        filled_at = datetime(2026, 5, 23, 1, tzinfo=timezone.utc)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="unmatched-us",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=100,
                executed_quantity=1,
                executed_price=100,
                status="FILLED",
                filled_at=filled_at,
            ),
            OrderRecord(
                broker_order_id="unmatched-hk",
                symbol="0700.HK",
                side="SELL",
                quantity=1,
                price=500,
                executed_quantity=1,
                executed_price=500,
                status="FILLED",
                filled_at=filled_at,
            ),
        ])
        db.commit()

        replay = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False
        )
        issue_days = {
            issue.symbol: issue.trade_day
            for issue in replay.issues
        }

        assert issue_days == {
            "AAPL.US": date(2026, 5, 22),
            "0700.HK": date(2026, 5, 23),
        }
        db.close()
