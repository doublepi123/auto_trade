from __future__ import annotations

from datetime import date, datetime, time, timezone

from pytest import approx
import pytest
from pytest import LogCaptureFixture

from app import database
from app.models import OrderRecord, RuntimeStateSnapshot
from app.services.daily_pnl_service import (
    DailyPnlResult,
    DailyPnlService,
    PnlReplayIssueCode,
    RealizedTrade,
)


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

    def _seed_authoritative_inventory_reset(
        self,
        db,
        trade_day: date,
    ) -> tuple[OrderRecord, float]:
        authoritative_gross = (208.16 - 206.329) * 1088
        authoritative_fee = 12.128
        authoritative_net = authoritative_gross - authoritative_fee
        db.add(OrderRecord(
            broker_order_id="stale-fifo-buy",
            symbol="NVDA.US",
            side="BUY",
            quantity=1192,
            price=209.3493704,
            executed_quantity=1192,
            executed_price=209.3493704,
            estimated_fee=124.7782241584,
            fee_source="ESTIMATED",
            status="FILLED",
            created_at=self._dt(trade_day, 10),
            filled_at=self._dt(trade_day, 10),
        ))
        exit_order = OrderRecord(
            broker_order_id="tracked-entry-sell",
            symbol="NVDA.US",
            side="SELL",
            quantity=1088,
            price=208.16,
            executed_quantity=1088,
            executed_price=208.16,
            status="FILLED",
            created_at=self._dt(trade_day, 11),
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=206.329,
            cost_basis_quantity=1088,
            cost_basis_opened_at=self._dt(trade_day, 9),
            position_quantity_before=1088,
            gross_pnl=authoritative_gross,
            pnl_fee=authoritative_fee,
            pnl_fee_rate=0.0005,
            pnl_fee_source="MIXED",
            net_pnl=authoritative_net,
            pnl_source="TRACKED_ENTRY",
        )
        db.add(exit_order)
        db.add_all([
            OrderRecord(
                broker_order_id="fresh-buy-after-reset",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=205,
                executed_quantity=10,
                executed_price=205,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=self._dt(trade_day, 12),
                filled_at=self._dt(trade_day, 12),
            ),
            OrderRecord(
                broker_order_id="fresh-sell-after-reset",
                symbol="NVDA.US",
                side="SELL",
                quantity=10,
                price=206,
                executed_quantity=10,
                executed_price=206,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=self._dt(trade_day, 13),
                filled_at=self._dt(trade_day, 13),
            ),
        ])
        db.commit()
        return exit_order, authoritative_net

    def test_refresh_preserves_authoritative_tracked_entry_outcome(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 15)
        db = self._get_db()
        exit_order, authoritative_net = self._seed_authoritative_inventory_reset(
            db,
            trade_day,
        )
        authoritative_gross = float(exit_order.gross_pnl or 0)

        DailyPnlService(db).refresh_execution_outcomes(symbol="NVDA.US")
        db.expire_all()
        refreshed = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "tracked-entry-sell"
        ).one()

        assert refreshed.pnl_source == "TRACKED_ENTRY"
        assert refreshed.cost_basis_quantity == approx(1088.0)
        assert refreshed.gross_pnl == approx(authoritative_gross)
        assert refreshed.net_pnl == approx(authoritative_net)
        assert refreshed.gross_pnl is not None and refreshed.gross_pnl > 0
        assert refreshed.net_pnl is not None and refreshed.net_pnl > 0
        db.close()

    def test_authoritative_outcome_drives_calculate_and_risk_reconcile(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 15)
        db = self._get_db()
        _exit_order, authoritative_net = self._seed_authoritative_inventory_reset(
            db,
            trade_day,
        )
        svc = DailyPnlService(db)

        trips = svc.pair_round_trips(symbol="NVDA.US")
        result = svc.calculate(trade_day=trade_day, symbol="NVDA.US")
        expected_daily_pnl = authoritative_net + 10.0
        reconciled_pnl, reconciled_losses = DailyPnlService.reconcile_risk_state(
            expected_daily_pnl,
            0,
            trade_day,
            result,
        )

        assert len(trips) == 2
        assert trips[0].quantity == approx(1088.0)
        assert trips[0].entry_price == approx(206.329)
        assert trips[0].gross_pnl == approx((208.16 - 206.329) * 1088)
        assert trips[0].net_pnl == approx(authoritative_net)
        assert trips[1].exit_broker_order_id == "fresh-sell-after-reset"
        assert trips[1].quantity == approx(10.0)
        assert trips[1].entry_price == approx(205.0)
        assert trips[1].gross_pnl == approx(10.0)
        assert trips[1].net_pnl == approx(10.0)
        assert result.realized_pnl == approx(expected_daily_pnl)
        assert [(trade.broker_order_id, trade.quantity) for trade in result.trades] == [
            ("tracked-entry-sell", approx(1088.0)),
            ("fresh-sell-after-reset", approx(10.0)),
        ]
        assert reconciled_pnl == approx(expected_daily_pnl)
        assert reconciled_losses == 0
        db.close()

    def test_same_day_replay_never_reduces_live_consecutive_losses(self) -> None:
        trade_day = date(2026, 7, 17)
        result = DailyPnlResult(
            trade_day=trade_day,
            realized_pnl=-20.0,
            consecutive_losses=0,
            trades=[
                RealizedTrade(
                    broker_order_id="ledger-loss",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=1.0,
                    price=90.0,
                    pnl=-20.0,
                    filled_at=self._dt(trade_day, 11),
                )
            ],
        )

        assert DailyPnlService.reconcile_risk_state(
            -10.0,
            3,
            trade_day,
            result,
        ) == (-20.0, 3)

    def test_malformed_authoritative_outcome_is_not_trusted(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 17)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="malformed-authoritative-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=90,
            executed_quantity=10,
            executed_price=90,
            actual_fee=0,
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=10,
            position_quantity_before=10,
            gross_pnl=-100,
            pnl_fee=0,
            net_pnl=100,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )

        assert result.is_complete is False
        assert result.realized_pnl == 0.0
        assert result.trades == []
        db.close()

    def test_near_zero_authoritative_sign_flip_is_not_trusted(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 17)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="sign-flipped-authoritative-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=1,
            price=100.00000001,
            executed_quantity=1,
            executed_price=100.00000001,
            actual_fee=0,
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=1,
            position_quantity_before=1,
            gross_pnl=-0.00000001,
            pnl_fee=0,
            net_pnl=-0.00000001,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )

        assert result.is_complete is False
        assert result.realized_pnl == 0.0
        assert result.trades == []
        db.close()

    def test_authoritative_outcome_uses_canonical_formula_after_validation(
        self,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 17)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="rounded-authoritative-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=1000,
            price=1100,
            executed_quantity=1000,
            executed_price=1100,
            actual_fee=10,
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=1000,
            position_quantity_before=1000,
            gross_pnl=1000000.5,
            pnl_fee=10,
            net_pnl=999990.5,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        service = DailyPnlService(db)
        result = service.calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )
        round_trips = service.pair_round_trips(
            symbol="AAPL.US",
            include_excursions=False,
        )

        assert result.is_complete is True
        assert result.realized_pnl == pytest.approx(999990.0)
        assert round_trips[0].gross_pnl == pytest.approx(1000000.0)
        assert round_trips[0].net_pnl == pytest.approx(999990.0)
        db.close()

    def test_conflicting_tracked_cost_basis_fails_closed_and_consumes_exit(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 16)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="ledger-buy-before-drift",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="tracked-sell-with-drift",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
                cost_basis_price=105,
                cost_basis_quantity=10,
                position_quantity_before=10,
                gross_pnl=50,
                pnl_fee=0,
                net_pnl=50,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="fresh-buy-after-drift",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 12),
            ),
            OrderRecord(
                broker_order_id="fresh-sell-after-drift",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=210,
                executed_quantity=1,
                executed_price=210,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 13),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )
        reconciled = DailyPnlService.reconcile_risk_state(
            -7.0,
            2,
            trade_day,
            result,
        )

        assert result.is_complete is False
        assert result.realized_pnl == approx(10.0)
        assert [trade.broker_order_id for trade in result.trades] == [
            "fresh-sell-after-drift"
        ]
        assert reconciled == (-7.0, 2)
        assert "conflicting tracked cost basis" in caplog.text
        db.close()

    def test_external_tracked_position_without_full_ledger_remains_authoritative(
        self,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 16)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="external-tracked-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=100,
            executed_quantity=10,
            executed_price=100,
            actual_fee=0,
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=90,
            cost_basis_quantity=10,
            position_quantity_before=10,
            gross_pnl=100,
            pnl_fee=0,
            net_pnl=100,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )

        assert result.is_complete is True
        assert result.realized_pnl == approx(100.0)
        assert [trade.broker_order_id for trade in result.trades] == [
            "external-tracked-sell"
        ]
        db.close()

    def test_historical_cost_conflict_does_not_taint_later_trade_day(self) -> None:
        self._cleanup()
        prior_day = date(2026, 7, 15)
        trade_day = date(2026, 7, 16)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="prior-buy-before-drift",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(prior_day, 10),
            ),
            OrderRecord(
                broker_order_id="prior-tracked-sell-with-drift",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(prior_day, 11),
                cost_basis_price=105,
                cost_basis_quantity=10,
                position_quantity_before=10,
                gross_pnl=50,
                pnl_fee=0,
                net_pnl=50,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="next-day-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=200,
                executed_quantity=1,
                executed_price=200,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="next-day-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=210,
                executed_quantity=1,
                executed_price=210,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )

        assert result.is_complete is True
        assert result.realized_pnl == approx(10.0)
        assert [trade.broker_order_id for trade in result.trades] == [
            "next-day-sell"
        ]
        db.close()

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

    def test_partial_fill_without_executed_quantity_is_not_assumed_full(self) -> None:
        self._cleanup()
        db = self._get_db()
        order = OrderRecord(
            broker_order_id="partial-without-quantity",
            symbol="AAPL.US",
            side="BUY",
            quantity=50,
            price=100,
            executed_quantity=0,
            executed_price=100,
            status="PARTIAL_FILLED",
            created_at=self._dt(date(2026, 7, 11), 10),
            filled_at=self._dt(date(2026, 7, 11), 10, 1),
        )

        assert DailyPnlService(db)._fill_from_order(order) is None
        db.close()

    def test_zero_quantity_terminal_order_does_not_warn_about_price(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
        self._cleanup()
        db = self._get_db()
        order = OrderRecord(
            broker_order_id="rejected-no-fill",
            symbol="AAPL.US",
            side="BUY",
            quantity=50,
            price=100,
            executed_quantity=0,
            executed_price=0,
            status="REJECTED",
        )

        assert DailyPnlService(db)._fill_from_order(order) is None
        assert "falling back to limit price" not in caplog.text
        db.close()

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

    def test_calculate_reports_full_unmatched_exit_issue(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        filled_at = self._dt(trade_day, 10, 1)
        db = self._get_db()
        exit_order = OrderRecord(
            broker_order_id="sell-without-entry",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=110,
            executed_quantity=10,
            executed_price=110,
            actual_fee=0,
            status="FILLED",
            created_at=self._dt(trade_day, 10),
            filled_at=filled_at,
        )
        db.add(exit_order)
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)

        assert result.is_complete is False
        assert result.realized_pnl == 0
        assert result.trades == []
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.FULL_UNMATCHED_EXIT
        assert issue.symbol == "AAPL.US"
        assert issue.side == "SELL"
        assert issue.trade_day == trade_day
        assert issue.filled_at == filled_at
        assert issue.exit_order_id == exit_order.id
        assert issue.exit_broker_order_id == "sell-without-entry"
        assert issue.filled_quantity == approx(10)
        assert issue.matched_quantity == 0
        assert issue.unmatched_quantity == approx(10)
        db.close()

    def test_calculate_reports_partial_overclose_without_partial_trade(
        self,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-before-partial-overclose",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="partial-overclose",
                symbol="AAPL.US",
                side="SELL",
                quantity=12,
                price=110,
                executed_quantity=12,
                executed_price=110,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)

        assert result.is_complete is False
        assert result.realized_pnl == 0
        assert result.trades == []
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.PARTIAL_OVERCLOSE
        assert issue.exit_broker_order_id == "partial-overclose"
        assert issue.filled_quantity == approx(12)
        assert issue.matched_quantity == approx(10)
        assert issue.unmatched_quantity == approx(2)
        db.close()

    def test_refresh_does_not_persist_partial_overclose_subset(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-before-refresh-overclose",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="refresh-partial-overclose",
                symbol="AAPL.US",
                side="SELL",
                quantity=12,
                price=110,
                executed_quantity=12,
                executed_price=110,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()
        service = DailyPnlService(db)

        updated = service.refresh_execution_outcomes(symbol="AAPL.US")
        db.expire_all()
        exit_order = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "refresh-partial-overclose"
        ).one()

        assert updated == 0
        assert exit_order.pnl_source == "UNKNOWN"
        assert exit_order.gross_pnl is None
        assert exit_order.net_pnl is None
        assert exit_order.cost_basis_price is None
        assert exit_order.cost_basis_quantity is None
        db.close()

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
