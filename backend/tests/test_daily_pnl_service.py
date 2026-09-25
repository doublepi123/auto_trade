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

    def test_revalidates_persisted_excursions_against_interior_snapshots(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 11)
        entry_at = self._dt(trade_day, 10)
        exit_at = self._dt(trade_day, 11)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="persisted-excursion-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=1,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ),
            OrderRecord(
                broker_order_id="persisted-excursion-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=2,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=exit_at,
                filled_at=exit_at,
                mfe_amount=42,
                mae_amount=-7,
                mfe_pct=4.2,
                mae_pct=-0.7,
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=150,
                created_at=self._dt(trade_day, 10, 30),
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=50,
                created_at=self._dt(trade_day, 10, 45),
            ),
        ])
        db.commit()
        without_excursions = DailyPnlService(db).pair_round_trips(
            include_excursions=False
        )[0]
        trip = DailyPnlService(db).pair_round_trips()[0]

        assert trip.mfe_amount == approx(500)
        assert trip.mae_amount == approx(-500)
        assert trip.mfe_pct == approx(50)
        assert trip.mae_pct == approx(-50)
        assert trip.excursion_source == "SNAPSHOT_OBSERVED"
        assert trip.excursion_interior_observation_count == 2
        assert without_excursions.mfe_amount is None
        assert without_excursions.mae_amount is None
        assert without_excursions.mfe_pct is None
        assert without_excursions.mae_pct is None
        db.close()

    def test_zero_actual_fee_uses_persisted_estimate_or_fee_schedule_once(
        self,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="paper-zero-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                estimated_fee=0.5,
                fee_source="ACTUAL",
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="paper-zero-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()
        service = DailyPnlService(db)

        trip = service.pair_round_trips(include_excursions=False)[0]
        result = service.calculate(trade_day=trade_day)

        assert trip.gross_pnl == approx(100.0)
        assert trip.est_fees == approx(1.05)
        assert trip.net_pnl == approx(98.95)
        assert trip.fee_source == "ESTIMATED"
        assert trip.actual_fees is None
        assert result.realized_pnl == approx(98.95)
        db.close()

    def test_backfilled_actual_fee_supersedes_the_stale_persisted_total(
        self,
    ) -> None:
        """A persisted total frozen from estimates must yield to a real charge.

        Longbridge reports a zero charge while a fill is still settling, so
        ``pnl_fee`` gets frozen from the fee schedule and stamped MIXED. Once
        the broker settles and ``actual_fee`` is backfilled with the real
        amount, ``_effective_authoritative_fee`` still returns the stale
        ``pnl_fee`` — it only special-cases ``actual_fee == 0``. Live evidence:
        order 19 holds pnl_fee=230.89 against a real round trip of 13.95 +
        19.39 = 33.34, a 6.9x overstatement that survives the backfill.
        """
        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="settled-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0.4,
                estimated_fee=5.0,
                fee_source="ACTUAL",
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="settled-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                # The broker settled: this is the real charge, not a placeholder.
                actual_fee=0.6,
                estimated_fee=5.5,
                fee_source="ACTUAL",
                # Frozen from estimates while the fill was still unsettled.
                pnl_fee=10.5,
                pnl_fee_source="MIXED",
                pnl_fee_rate=0.0005,
                # Authoritative outcome: this is the branch that reuses pnl_fee.
                pnl_source="TRACKED_ENTRY",
                gross_pnl=100.0,
                net_pnl=89.5,
                cost_basis_price=100.0,
                cost_basis_quantity=10,
                position_quantity_before=10,
                exit_cause="TIME_STOP",
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
            ),
        ])
        db.commit()
        try:
            service = DailyPnlService(db)

            trip = service.pair_round_trips(include_excursions=False)[0]

            assert trip.gross_pnl == approx(100.0)
            assert trip.est_fees == approx(1.0), (
                "both sides settled at 0.4 + 0.6, so the round trip cost 1.0; "
                f"the stale persisted total must not be reused, got {trip.est_fees}"
            )
            # net_pnl stays the persisted authoritative outcome by design; this
            # test pins the reported fee, not a recomputation of the P&L.
            assert trip.net_pnl == approx(89.5)
            assert trip.fee_source == "ACTUAL"
        finally:
            db.close()

    def test_zero_actual_charge_persisted_outcome_matches_the_ledger_replay(self) -> None:
        # The stored net_pnl (read directly by ~20 analytics services) must
        # agree with what /api/trades and the daily risk replay report.
        from app.runner import AppRunner

        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="paper-zero-buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                estimated_fee=0.5,
                fee_source="ACTUAL",
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="paper-zero-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                # Fill-time outcome: the charge had not been reported yet.
                actual_fee=None,
                estimated_fee=0.55,
                fee_source="UNKNOWN",
                status="FILLED",
                filled_at=self._dt(trade_day, 11),
                cost_basis_price=100,
                cost_basis_quantity=10,
                position_quantity_before=10,
                pnl_fee_rate=0.0005,
                pnl_source="TRACKED_ENTRY",
            ),
        ])
        db.commit()
        exit_order = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "paper-zero-sell"
        ).one()
        AppRunner._update_execution_outcome_fields(exit_order)
        db.commit()

        # When the broker later reports its 0.00 placeholder charge
        exit_order.actual_fee = 0.0
        exit_order.fee_source = "ACTUAL"
        AppRunner._update_execution_outcome_fields(exit_order)
        db.commit()
        db.expire_all()
        stored = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "paper-zero-sell"
        ).one()
        trip = DailyPnlService(db).pair_round_trips(include_excursions=False)[0]
        result = DailyPnlService(db).calculate(trade_day=trade_day)

        assert stored.net_pnl == approx(trip.net_pnl), (
            f"orders.net_pnl={stored.net_pnl} disagrees with /api/trades {trip.net_pnl}"
        )
        assert stored.net_pnl == approx(98.95)
        assert result.realized_pnl == approx(98.95)
        db.close()

    def test_refresh_then_order_sync_then_refresh_never_flip_flops(self) -> None:
        # Live order of events: fill-time outcome frozen as MIXED with the
        # entry fee only, the post-fill refresh repairs it, then today-order
        # sync rewrites the row through the runner helper, then refresh again.
        from app.runner import AppRunner

        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="paper-zero-sequence-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=110,
            executed_quantity=10,
            executed_price=110,
            actual_fee=0,
            estimated_fee=0.55,
            fee_source="ACTUAL",
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=10,
            position_quantity_before=10,
            gross_pnl=100,
            pnl_fee=0.5,
            pnl_fee_source="MIXED",
            pnl_fee_rate=0.0005,
            net_pnl=99.5,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        def stored() -> OrderRecord:
            db.expire_all()
            return db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == "paper-zero-sequence-sell"
            ).one()

        DailyPnlService(db).refresh_execution_outcomes(symbol="AAPL.US")
        after_repair = (stored().pnl_fee, stored().net_pnl, stored().pnl_fee_source)
        row = stored()
        AppRunner._update_execution_outcome_fields(row)
        db.commit()
        after_sync = (stored().pnl_fee, stored().net_pnl, stored().pnl_fee_source)
        second = DailyPnlService(db).refresh_execution_outcomes(symbol="AAPL.US")
        after_second = (stored().pnl_fee, stored().net_pnl, stored().pnl_fee_source)
        trip = DailyPnlService(db).pair_round_trips(include_excursions=False)[0]

        assert after_repair == (approx(1.05), approx(98.95), "ESTIMATED")
        assert after_sync == after_repair, "order sync undid the zero-charge repair"
        assert second == 0
        assert after_second == after_repair
        assert trip.net_pnl == approx(98.95)
        db.close()

    def test_repairs_authoritative_zero_exit_fee_idempotently(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="paper-zero-authoritative-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=110,
            executed_quantity=10,
            executed_price=110,
            actual_fee=0,
            estimated_fee=0.55,
            fee_source="ACTUAL",
            status="FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=10,
            position_quantity_before=10,
            gross_pnl=100,
            pnl_fee=0.5,
            pnl_fee_source="MIXED",
            pnl_fee_rate=0.0005,
            net_pnl=99.5,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()
        service = DailyPnlService(db)

        before_refresh = service.pair_round_trips(include_excursions=False)[0]
        result = service.calculate(trade_day=trade_day)
        first_updated = service.refresh_execution_outcomes(symbol="AAPL.US")
        second_updated = service.refresh_execution_outcomes(symbol="AAPL.US")
        db.expire_all()
        repaired = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "paper-zero-authoritative-sell"
        ).one()
        after_refresh = service.pair_round_trips(include_excursions=False)[0]

        assert before_refresh.est_fees == approx(1.05)
        assert before_refresh.net_pnl == approx(98.95)
        assert before_refresh.fee_source == "ESTIMATED"
        assert result.realized_pnl == approx(98.95)
        assert first_updated == 1
        assert second_updated == 0
        assert repaired.actual_fee == 0
        assert repaired.estimated_fee == approx(0.55)
        assert repaired.fee_source == "ACTUAL"
        assert repaired.pnl_fee == approx(1.05)
        assert repaired.pnl_fee_source == "ESTIMATED"
        assert repaired.net_pnl == approx(98.95)
        assert after_refresh.est_fees == approx(1.05)
        assert after_refresh.net_pnl == approx(98.95)
        db.close()

    def test_partial_fill_scales_frozen_full_order_fee_estimate(self) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 24)
        db = self._get_db()
        db.add(OrderRecord(
            broker_order_id="paper-partial-authoritative-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price=110,
            executed_quantity=10,
            executed_price=110,
            actual_fee=0,
            estimated_fee=5.5,
            fee_source="ACTUAL",
            status="PARTIAL_FILLED",
            filled_at=self._dt(trade_day, 11),
            cost_basis_price=100,
            cost_basis_quantity=10,
            position_quantity_before=10,
            gross_pnl=100,
            pnl_fee=0.5,
            pnl_fee_source="MIXED",
            pnl_fee_rate=0.0005,
            net_pnl=99.5,
            pnl_source="TRACKED_ENTRY",
        ))
        db.commit()

        trip = DailyPnlService(db).pair_round_trips(
            include_excursions=False,
        )[0]

        assert trip.quantity == approx(10)
        assert trip.gross_pnl == approx(100)
        assert trip.est_fees == approx(1.05)
        assert trip.net_pnl == approx(98.95)
        assert trip.fee_source == "ESTIMATED"
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

    @pytest.mark.parametrize(
        "pnl_source",
        ["TRACKED_ENTRY", "BROKER_POSITION"],
    )
    def test_refresh_enriches_authoritative_outcome_with_excursions(
        self,
        pnl_source: str,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 24)
        entry_at = self._dt(trade_day, 10)
        exit_at = self._dt(trade_day, 11)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id=f"{pnl_source}-buy",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=1,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ),
            OrderRecord(
                broker_order_id=f"{pnl_source}-sell",
                symbol="NVDA.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                actual_fee=2,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=exit_at,
                filled_at=exit_at,
                cost_basis_price=100,
                cost_basis_quantity=10,
                cost_basis_opened_at=entry_at,
                position_quantity_before=10,
                gross_pnl=100,
                pnl_fee=3,
                pnl_fee_source="ACTUAL",
                pnl_fee_rate=0.0005,
                net_pnl=97,
                pnl_source=pnl_source,
            ),
            RuntimeStateSnapshot(
                symbol="NVDA.US",
                last_price=115,
                created_at=self._dt(trade_day, 10, 30),
            ),
            RuntimeStateSnapshot(
                symbol="NVDA.US",
                last_price=97,
                created_at=self._dt(trade_day, 10, 45),
            ),
        ])
        db.commit()
        service = DailyPnlService(db)

        first_updated = service.refresh_execution_outcomes(symbol="NVDA.US")
        second_updated = service.refresh_execution_outcomes(symbol="NVDA.US")
        db.expire_all()
        refreshed = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == f"{pnl_source}-sell"
        ).one()

        assert first_updated == 1
        assert second_updated == 0
        assert refreshed.pnl_source == pnl_source
        assert refreshed.gross_pnl == approx(100)
        assert refreshed.net_pnl == approx(97)
        assert refreshed.mfe_amount == approx(150)
        assert refreshed.mae_amount == approx(-30)
        assert refreshed.mfe_pct == approx(15)
        assert refreshed.mae_pct == approx(-3)
        db.close()

    def test_authoritative_reset_drives_risk_but_not_performance_stats(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 15)
        db = self._get_db()
        _exit_order, authoritative_net = self._seed_authoritative_inventory_reset(
            db,
            trade_day,
        )
        svc = DailyPnlService(db)

        replay = svc.pair_round_trips_with_issues(
            symbol="NVDA.US",
            include_excursions=False,
        )
        result = svc.calculate(trade_day=trade_day, symbol="NVDA.US")
        fresh_net_pnl = 10.0 - (205.0 + 206.0) * 10 * 0.0005
        expected_daily_pnl = authoritative_net + fresh_net_pnl
        reconciled_pnl, reconciled_losses = DailyPnlService.reconcile_risk_state(
            expected_daily_pnl,
            0,
            trade_day,
            result,
        )

        assert len(replay.trades) == 1
        assert (
            replay.trades[0].exit_broker_order_id
            == "fresh-sell-after-reset"
        )
        assert replay.trades[0].quantity == approx(10.0)
        assert replay.trades[0].entry_price == approx(205.0)
        assert replay.trades[0].gross_pnl == approx(10.0)
        assert replay.trades[0].net_pnl == approx(fresh_net_pnl)
        assert len(replay.issues) == 1
        assert (
            replay.issues[0].issue_code
            is PnlReplayIssueCode.UNVERIFIED_COST_BASIS
        )
        assert (
            replay.issues[0].exit_broker_order_id
            == "tracked-entry-sell"
        )
        assert not any(
            "round-trip replay" in record.message
            for record in caplog.records
        )
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

    def test_malformed_authoritative_outcome_with_inventory_drift_fails_closed(
        self,
    ) -> None:
        self._cleanup()
        trade_day = date(2026, 7, 17)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="stale-buy-before-malformed-exit",
                symbol="AAPL.US",
                side="BUY",
                quantity=12,
                price=95,
                executed_quantity=12,
                executed_price=95,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(trade_day, 10),
            ),
            OrderRecord(
                broker_order_id="malformed-authoritative-drift-sell",
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
                net_pnl=-100,
                pnl_source="TRACKED_ENTRY",
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(
            trade_day=trade_day,
            symbol="AAPL.US",
        )

        assert result.is_complete is False
        assert result.realized_pnl == 0.0
        assert result.trades == []
        assert len(result.issues) == 1
        assert (
            result.issues[0].issue_code
            is PnlReplayIssueCode.UNVERIFIED_COST_BASIS
        )
        assert (
            result.issues[0].exit_broker_order_id
            == "malformed-authoritative-drift-sell"
        )
        assert DailyPnlService.reconcile_risk_state(
            -25.0,
            2,
            trade_day,
            result,
        ) == (-25.0, 2)
        db.close()

    def test_partial_authoritative_reset_does_not_validate_residual_position(
        self,
    ) -> None:
        self._cleanup()
        reset_day = date(2026, 7, 17)
        residual_exit_day = date(2026, 7, 18)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="stale-buy-before-partial-reset",
                symbol="AAPL.US",
                side="BUY",
                quantity=12,
                price=95,
                executed_quantity=12,
                executed_price=95,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(reset_day, 10),
            ),
            OrderRecord(
                broker_order_id="valid-partial-authoritative-reset",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                actual_fee=0,
                status="FILLED",
                filled_at=self._dt(reset_day, 11),
                cost_basis_price=90,
                cost_basis_quantity=10,
                position_quantity_before=20,
                gross_pnl=100,
                pnl_fee=0,
                net_pnl=100,
                pnl_source="TRACKED_ENTRY",
            ),
            OrderRecord(
                broker_order_id="unverified-residual-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=200,
                executed_quantity=10,
                executed_price=200,
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
            symbol="AAPL.US",
            include_excursions=False,
        )

        assert reset_result.is_complete is True
        assert reset_result.realized_pnl == approx(100)
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

        service = DailyPnlService(db)
        replay = service.pair_round_trips_with_issues(
            symbol="AAPL.US",
            include_excursions=False,
        )
        result = service.calculate(
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
        assert [issue.issue_code for issue in replay.issues] == [
            PnlReplayIssueCode.COST_BASIS_CONFLICT
        ]
        assert reconciled == (-7.0, 2)
        conflict_logs = [
            record
            for record in caplog.records
            if "conflicting tracked cost basis" in record.message
        ]
        assert len(conflict_logs) == 1
        assert conflict_logs[0].message.startswith("daily PnL replay")
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

    def test_historical_unclosed_remainder_does_not_warn_for_later_day(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
        self._cleanup()
        historical_day = date(2026, 5, 22)
        target_day = date(2026, 5, 26)
        db = self._get_db()
        db.add(
            OrderRecord(
                broker_order_id="historical-sell-without-holding",
                symbol="AAPL.US",
                side="SELL",
                quantity=10,
                price=110,
                executed_quantity=10,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(historical_day, 10),
                filled_at=self._dt(historical_day, 10, 1),
            )
        )
        db.commit()

        import logging
        caplog.set_level(logging.WARNING)
        _ = DailyPnlService(db).calculate(trade_day=target_day)
        db.close()

        assert not any(
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

    def test_round_trip_overclose_is_structured_without_logging(
        self,
        caplog: LogCaptureFixture,
    ) -> None:
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
        first = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False,
        )
        second = DailyPnlService(db).pair_round_trips_with_issues(
            include_excursions=False,
        )
        db.close()

        assert first == second
        assert first.trades == []
        assert [issue.issue_code for issue in first.issues] == [
            PnlReplayIssueCode.PARTIAL_OVERCLOSE
        ]
        assert not any(
            "round-trip close of" in record.message
            for record in caplog.records
        )

    def test_nan_price_on_claimed_order_record_is_structured_invalid_fill(self) -> None:
        trade_day = date(2026, 7, 31)
        order = OrderRecord(
            id=901,
            broker_order_id="nan-price-fill",
            symbol="AAPL.US",
            side="SELL",
            quantity=1,
            price=100,
            executed_quantity=1,
            executed_price=float("nan"),
            status="FILLED",
            created_at=self._dt(trade_day, 10),
            filled_at=self._dt(trade_day, 10),
        )

        class _OrderQuery:
            def filter(self, *_args: object) -> "_OrderQuery":
                return self

            def all(self) -> list[OrderRecord]:
                return [order]

        class _ReplayDb:
            def query(self, *_args: object) -> _OrderQuery:
                return _OrderQuery()

        replay = DailyPnlService(_ReplayDb()).pair_round_trips_with_issues(
            include_excursions=False,
        )

        assert replay.trades == []
        assert len(replay.issues) == 1
        issue = replay.issues[0]
        assert issue.issue_code is PnlReplayIssueCode.INVALID_FILL_EVIDENCE
        assert issue.filled_quantity == 1
        assert issue.matched_quantity == 0
        assert issue.unmatched_quantity == 1

    def test_sec98_partial_then_external_close_recognises_full_entry_fee(self) -> None:
        """§9.8 marker: local partial exit + external close of the remainder.

        Entry BUY 2 @ 250 with the marker (frozen estimated_fee = order_fee
        = 1.568 + 0.0000641*250*2 = 1.60005); local SELL 1 @ 250 is an
        authoritative TRACKED_ENTRY exit carrying the marker, whose persisted
        outcome is computed with the proportional allocated_entry_fee; the
        external SELL 1 @ 250 has no marker and falls back to fee_rate_us.
        Both replay views must total
        1.60005 + (1.568 + 0.0000641*250) + 250*1*0.0005 = 3.309075.
        """
        import json as _json
        from decimal import Decimal as _Decimal

        from app.runner import AppRunner

        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        trade_day = date(2026, 9, 24)
        entry_at = self._dt(trade_day, 10)
        local_exit_at = self._dt(trade_day, 11)
        external_exit_at = self._dt(trade_day, 12)
        db = self._get_db()
        try:
            # 1) Entry order with the marker; estimated_fee frozen via order_fee.
            db.add(OrderRecord(
                broker_order_id="sec98-e2e-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=1.60005,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            # 2) Local partial SELL 1: authoritative TRACKED_ENTRY with marker.
            #    Persisted outcome computed exactly as the live path does.
            entry_fee_alloc = _Decimal("1.568") / 2 + _Decimal("0.0000641") * _Decimal("250") * _Decimal("1")
            exit_fee_local = _Decimal("1.568") + _Decimal("0.0000641") * _Decimal("250") * _Decimal("1")
            local_gross = (_Decimal("250") - _Decimal("250")) * _Decimal("1")
            local_order = OrderRecord(
                broker_order_id="sec98-e2e-local-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=local_exit_at,
                filled_at=local_exit_at,
                config_snapshot=marker,
                pnl_source="TRACKED_ENTRY",
                cost_basis_price=250,
                cost_basis_quantity=1,
                cost_basis_opened_at=entry_at,
                position_quantity_before=2,
                pnl_fee_rate=0.0005,
            )
            db.add(local_order)
            db.commit()
            db.refresh(local_order)
            AppRunner._update_execution_outcome_fields(local_order)
            db.commit()
            assert local_order.pnl_fee is not None
            assert float(entry_fee_alloc + exit_fee_local) == approx(
                float(local_order.pnl_fee), abs=1e-9
            )
            # 3) External SELL 1: broker-synced, no marker, legacy fee rate.
            db.add(OrderRecord(
                broker_order_id="sec98-e2e-external-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            expected_total = 1.60005 + (1.568 + 0.0000641 * 250) + 250 * 1 * 0.0005

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 2
            assert sum(t.est_fees for t in trips) == approx(
                expected_total, abs=1e-6
            )
            assert sum(t.net_pnl for t in trips) == approx(
                -expected_total, abs=1e-6
            )

            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.is_complete is True
            assert result.realized_pnl == approx(-expected_total, abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_legacy_partial_then_external_close_unchanged(self) -> None:
        """No marker anywhere: today's numbers through both replay views."""
        self._cleanup()
        trade_day = date(2026, 9, 24)
        entry_at = self._dt(trade_day, 10)
        local_exit_at = self._dt(trade_day, 11)
        external_exit_at = self._dt(trade_day, 12)
        db = self._get_db()
        try:
            from app.runner import AppRunner

            db.add(OrderRecord(
                broker_order_id="legacy-e2e-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=0.25,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ))
            local_order = OrderRecord(
                broker_order_id="legacy-e2e-local-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=local_exit_at,
                filled_at=local_exit_at,
                pnl_source="TRACKED_ENTRY",
                cost_basis_price=250,
                cost_basis_quantity=1,
                cost_basis_opened_at=entry_at,
                position_quantity_before=2,
                pnl_fee_rate=0.0005,
            )
            db.add(local_order)
            db.commit()
            db.refresh(local_order)
            AppRunner._update_execution_outcome_fields(local_order)
            db.commit()
            db.add(OrderRecord(
                broker_order_id="legacy-e2e-external-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            # entry fee 0.25 allocated half to each close; both exits fall
            # back to the frozen estimate/rate: 0.125 each (250*1*0.0005).
            expected_total = 0.25 + 0.125 + 0.125

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 2
            assert sum(t.est_fees for t in trips) == approx(
                expected_total, abs=1e-6
            )
            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.realized_pnl == approx(-expected_total, abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def _sec98_exit_order(
        self,
        order_id: str,
        filled_at: datetime,
        *,
        position_before: int,
        fill_qty: int,
        cost_basis_opened_at: datetime,
        price: float = 250.0,
    ) -> OrderRecord:
        """Marker SELL ready for the live-path outcome computation."""
        import json as _json

        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        return OrderRecord(
            broker_order_id=order_id,
            symbol="AAPL.US",
            side="SELL",
            quantity=fill_qty,
            price=price,
            executed_quantity=fill_qty,
            executed_price=price,
            actual_fee=0,
            fee_source="ACTUAL",
            status="FILLED",
            created_at=filled_at,
            filled_at=filled_at,
            config_snapshot=marker,
            pnl_source="TRACKED_ENTRY",
            cost_basis_price=price,
            cost_basis_quantity=fill_qty,
            cost_basis_opened_at=cost_basis_opened_at,
            position_quantity_before=position_before,
            pnl_fee_rate=0.0005,
        )

    def _persist_authoritative_sec98_outcome(
        self,
        db,
        order: OrderRecord,
    ) -> None:
        from app.runner import AppRunner

        db.add(order)
        db.commit()
        db.refresh(order)
        AppRunner._update_execution_outcome_fields(order)
        db.commit()

    def test_sec98_two_local_partials_then_external_close(self) -> None:
        """S1: BUY 100 (marker) → local SELL 40 → local SELL 30 → external
        SELL 30 (no marker).

        Rule R re-derives the entry pool at each authoritative marker exit
        (order_fee(B, Q)) and the remainder carries to the external close:
        1.2682 + 2.209 + 1.26475 + 2.04875 + 1.26475 + 3.75 = 11.80545.
        """
        import json as _json
        from decimal import Decimal as _Decimal

        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        first_exit_at = self._dt(trade_day, 11)
        second_exit_at = self._dt(trade_day, 12)
        external_exit_at = self._dt(trade_day, 13)
        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        db = self._get_db()
        try:
            db.add(OrderRecord(
                broker_order_id="s1-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=100,
                price=250,
                executed_quantity=100,
                executed_price=250,
                estimated_fee=3.1705,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            self._persist_authoritative_sec98_outcome(db, self._sec98_exit_order(
                "s1-local-sell-40",
                first_exit_at,
                position_before=100,
                fill_qty=40,
                cost_basis_opened_at=entry_at,
            ))
            self._persist_authoritative_sec98_outcome(db, self._sec98_exit_order(
                "s1-local-sell-30",
                second_exit_at,
                position_before=60,
                fill_qty=30,
                cost_basis_opened_at=entry_at,
            ))
            db.add(OrderRecord(
                broker_order_id="s1-external-sell-30",
                symbol="AAPL.US",
                side="SELL",
                quantity=30,
                price=250,
                executed_quantity=30,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            expected = _Decimal("11.80545")

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 3
            assert sum(t.net_pnl for t in trips) == approx(-float(expected), abs=1e-6)

            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.is_complete is True
            assert result.realized_pnl == approx(-float(expected), abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_sec98_rule_r_skips_a_disputed_cost_basis(self) -> None:
        # A marker partial exit that declares a conflicting basis (10 vs the
        # replayed 250) is rejected as COST_BASIS_CONFLICT. Rule R must not
        # rebuild the remaining entry fee pool from that rejected basis, so
        # the external close still carries order_fee(250, 2) / 2 = 0.800025.
        import json as _json

        trade_day = date(2026, 9, 25)
        next_day = date(2026, 9, 26)
        entry_at = self._dt(trade_day, 10)
        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        db = self._get_db()
        try:
            db.add(OrderRecord(
                broker_order_id="conflict-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=1.60005,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            disputed = self._sec98_exit_order(
                "conflict-local-sell",
                self._dt(trade_day, 11),
                position_before=2,
                fill_qty=1,
                cost_basis_opened_at=entry_at,
            )
            disputed.cost_basis_price = 10
            self._persist_authoritative_sec98_outcome(db, disputed)
            external_at = self._dt(next_day, 11)
            db.add(OrderRecord(
                broker_order_id="conflict-external-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                status="FILLED",
                created_at=external_at,
                filled_at=external_at,
            ))
            db.commit()

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            external = [
                t for t in trips if t.exit_broker_order_id == "conflict-external-sell"
            ]
            assert len(external) == 1
            assert external[0].net_pnl == approx(-(0.800025 + 0.125), abs=1e-6)

            result = DailyPnlService(db).calculate(
                trade_day=next_day, symbol="AAPL.US",
            )
            assert result.realized_pnl == approx(external[0].net_pnl, abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_sec98_rule_r_never_reports_a_modelled_entry_fee_as_actual(self) -> None:
        # The entry carried a settled broker charge (10). Once Rule R
        # replaces its remaining pool with the §9.8 model, that remainder is
        # an estimate: a later external close with its own actual charge
        # must not be reported as a fully ACTUAL round trip.
        import json as _json

        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        db = self._get_db()
        try:
            db.add(OrderRecord(
                broker_order_id="actual-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=250,
                executed_quantity=2,
                executed_price=250,
                actual_fee=10,
                estimated_fee=1.60005,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            self._persist_authoritative_sec98_outcome(db, self._sec98_exit_order(
                "actual-local-sell",
                self._dt(trade_day, 11),
                position_before=2,
                fill_qty=1,
                cost_basis_opened_at=entry_at,
            ))
            external_at = self._dt(trade_day, 12)
            db.add(OrderRecord(
                broker_order_id="actual-external-sell",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                actual_fee=1,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=external_at,
                filled_at=external_at,
            ))
            db.commit()

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            external = [
                t for t in trips if t.exit_broker_order_id == "actual-external-sell"
            ]
            assert len(external) == 1
            assert external[0].fee_source != "ACTUAL", (
                "a §9.8 modelled entry share was reported as a broker charge"
            )
            assert external[0].actual_fees is None
        finally:
            db.close()
            self._cleanup()

    def test_sec98_partially_filled_entry_keeps_the_fixed_fee(self) -> None:
        """S2: BUY submitted 4, filled 2 (marker, estimate frozen for 4) →
        local SELL 1 → external SELL 1.

        The frozen estimate is NOT scaled by executed/submitted (that would
        halve the fixed 1.568); the fill carries order_fee(executed):
        0.800025 + 1.584025 + 0.800025 + 0.125 = 3.309075.
        """
        import json as _json
        from decimal import Decimal as _Decimal

        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        local_exit_at = self._dt(trade_day, 11)
        external_exit_at = self._dt(trade_day, 12)
        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        db = self._get_db()
        try:
            db.add(OrderRecord(
                broker_order_id="s2-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=4,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=1.6321,  # frozen via order_fee(250, 4)
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            self._persist_authoritative_sec98_outcome(db, self._sec98_exit_order(
                "s2-local-sell-1",
                local_exit_at,
                position_before=2,
                fill_qty=1,
                cost_basis_opened_at=entry_at,
            ))
            db.add(OrderRecord(
                broker_order_id="s2-external-sell-1",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            expected = _Decimal("3.309075")

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 2
            assert sum(t.net_pnl for t in trips) == approx(-float(expected), abs=1e-6)

            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.is_complete is True
            assert result.realized_pnl == approx(-float(expected), abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_sec98_partially_filled_entry_external_close_only(self) -> None:
        """Blocker 1b minimal: BUY submitted 4, filled 2 (marker) closed only
        by an external SELL 2. Entry side = order_fee(250, 2) = 1.60005
        (not the scaled 0.81605), exit side 0.25: total 1.85005."""
        import json as _json
        from decimal import Decimal as _Decimal

        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        external_exit_at = self._dt(trade_day, 12)
        marker = _json.dumps({"accounting_fee_model": "us-sec98-v1"})
        self._cleanup()
        db = self._get_db()
        try:
            db.add(OrderRecord(
                broker_order_id="s3-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=4,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=1.6321,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
                config_snapshot=marker,
            ))
            db.add(OrderRecord(
                broker_order_id="s3-external-sell-2",
                symbol="AAPL.US",
                side="SELL",
                quantity=2,
                price=250,
                executed_quantity=2,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            expected = _Decimal("1.85005")

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 1
            assert sum(t.net_pnl for t in trips) == approx(-float(expected), abs=1e-6)

            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.realized_pnl == approx(-float(expected), abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_legacy_two_local_partials_then_external_close_unchanged(self) -> None:
        """L1: the S1 sequence without any marker keeps today's numbers:
        100*250*0.0005 + 2*(40*250*0.0005) + 2*(30*250*0.0005) + 2*(30*250*0.0005)
        = 12.5 + 10 + 7.5 + 7.5 = 37.5."""
        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        first_exit_at = self._dt(trade_day, 11)
        second_exit_at = self._dt(trade_day, 12)
        external_exit_at = self._dt(trade_day, 13)
        self._cleanup()
        db = self._get_db()
        try:
            from app.runner import AppRunner

            db.add(OrderRecord(
                broker_order_id="l1-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=100,
                price=250,
                executed_quantity=100,
                executed_price=250,
                estimated_fee=12.5,
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ))
            for order_id, at, before, fill in (
                ("l1-local-sell-40", first_exit_at, 100, 40),
                ("l1-local-sell-30", second_exit_at, 60, 30),
            ):
                order = OrderRecord(
                    broker_order_id=order_id,
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=fill,
                    price=250,
                    executed_quantity=fill,
                    executed_price=250,
                    actual_fee=0,
                    fee_source="ACTUAL",
                    status="FILLED",
                    created_at=at,
                    filled_at=at,
                    pnl_source="TRACKED_ENTRY",
                    cost_basis_price=250,
                    cost_basis_quantity=fill,
                    cost_basis_opened_at=entry_at,
                    position_quantity_before=before,
                    pnl_fee_rate=0.0005,
                )
                db.add(order)
                db.commit()
                db.refresh(order)
                AppRunner._update_execution_outcome_fields(order)
                db.commit()
            db.add(OrderRecord(
                broker_order_id="l1-external-sell-30",
                symbol="AAPL.US",
                side="SELL",
                quantity=30,
                price=250,
                executed_quantity=30,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            expected = 25.0

            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 3
            assert sum(t.net_pnl for t in trips) == approx(-expected, abs=1e-6)
            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.realized_pnl == approx(-expected, abs=1e-6)
        finally:
            db.close()
            self._cleanup()

    def test_legacy_partially_filled_entry_unchanged(self) -> None:
        """L2: the S2 sequence without any marker keeps today's numbers:
        the frozen estimate 0.25 stays scaled by executed/submitted (0.125),
        so total = 0.125 + 0.25 + 0.25 = 0.625."""
        trade_day = date(2026, 9, 25)
        entry_at = self._dt(trade_day, 10)
        local_exit_at = self._dt(trade_day, 11)
        external_exit_at = self._dt(trade_day, 12)
        self._cleanup()
        db = self._get_db()
        try:
            from app.runner import AppRunner

            db.add(OrderRecord(
                broker_order_id="l2-entry",
                symbol="AAPL.US",
                side="BUY",
                quantity=4,
                price=250,
                executed_quantity=2,
                executed_price=250,
                estimated_fee=0.25,  # frozen for the submitted 4
                fee_source="ESTIMATED",
                status="FILLED",
                created_at=entry_at,
                filled_at=entry_at,
            ))
            order = OrderRecord(
                broker_order_id="l2-local-sell-1",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                actual_fee=0,
                fee_source="ACTUAL",
                status="FILLED",
                created_at=local_exit_at,
                filled_at=local_exit_at,
                pnl_source="TRACKED_ENTRY",
                cost_basis_price=250,
                cost_basis_quantity=1,
                cost_basis_opened_at=entry_at,
                position_quantity_before=2,
                pnl_fee_rate=0.0005,
            )
            db.add(order)
            db.commit()
            db.refresh(order)
            AppRunner._update_execution_outcome_fields(order)
            db.commit()
            db.add(OrderRecord(
                broker_order_id="l2-external-sell-1",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=250,
                executed_quantity=1,
                executed_price=250,
                status="FILLED",
                created_at=external_exit_at,
                filled_at=external_exit_at,
            ))
            db.commit()

            # Today's numbers, per view: the legacy entry lot's fee keeps the
            # executed/submitted scaling (0.25 * 2/4 = 0.125), so
            # pair_round_trips totals 0.125 + 0.25 + 0.0625 = 0.4375 while
            # calculate()'s authoritative rebases give 0.5. Both are today's
            # behaviour and must not change.
            trips = DailyPnlService(db).pair_round_trips(
                symbol="AAPL.US", include_excursions=False,
            )
            assert len(trips) == 2
            assert sum(t.net_pnl for t in trips) == approx(-0.4375, abs=1e-6)
            result = DailyPnlService(db).calculate(
                trade_day=trade_day, symbol="AAPL.US",
            )
            assert result.realized_pnl == approx(-0.5, abs=1e-6)
        finally:
            db.close()
            self._cleanup()
