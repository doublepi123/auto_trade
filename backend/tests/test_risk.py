from datetime import datetime, timezone
import threading

import pytest

from app.core.risk import RiskConfig, RiskController


class TestRiskConfig:
    def test_negative_max_daily_loss_raises(self) -> None:
        with pytest.raises(ValueError, match="max_daily_loss must be non-negative"):
            RiskConfig(max_daily_loss=-100.0)

    def test_zero_max_daily_loss_ok(self) -> None:
        config = RiskConfig(max_daily_loss=0.0)
        assert config.max_daily_loss == 0.0

    def test_none_max_daily_loss_ok(self) -> None:
        # runtime_state_service.load() may pass None when the DB column is NULL.
        config = RiskConfig(max_daily_loss=None)  # type: ignore[arg-type]
        assert config.max_daily_loss is None

    def test_negative_max_drawdown_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="max_drawdown_amount must be non-negative"):
            RiskConfig(max_drawdown_amount=-1.0)


class TestRiskController:
    def test_default_approved(self) -> None:
        ctrl = RiskController()
        result = ctrl.check()
        assert result.approved is True

    def test_paused_rejected(self) -> None:
        ctrl = RiskController()
        ctrl.pause()
        result = ctrl.check()
        assert result.approved is False
        assert "paused" in result.reason.lower()

    def test_resume_after_pause(self) -> None:
        ctrl = RiskController()
        ctrl.pause()
        ctrl.resume()
        result = ctrl.check()
        assert result.approved is True

    def test_verified_resume_callback_failure_restores_original_pause(self) -> None:
        paused_at = datetime(2026, 7, 16, 1, 2, tzinfo=timezone.utc)
        reason = "ORDER_RECONCILIATION_UNCERTAIN: retain this diagnosis"
        ctrl = RiskController()
        ctrl.pause(
            reason,
            auto_resumable=True,
            paused_at=paused_at,
        )
        pause_reason, generation = ctrl.pause_verification_snapshot()

        def fail_persistence() -> None:
            assert ctrl.paused is False
            raise RuntimeError("commit interrupted")

        with pytest.raises(RuntimeError, match="commit interrupted"):
            ctrl.resume_if_pause_reason(
                pause_reason,
                expected_generation=generation,
                on_resumed=fail_persistence,
            )

        assert ctrl.paused is True
        assert ctrl.pause_reason == reason
        assert ctrl.paused_at == paused_at
        assert ctrl.pause_auto_resumable is True

    def test_verified_resume_serializes_concurrent_pause_with_callback(self) -> None:
        original_reason = "ORDER_RECONCILIATION_UNCERTAIN: original"
        changed_reason = "POSITION_RECONCILIATION_UNCERTAIN: changed"
        ctrl = RiskController()
        ctrl.pause(original_reason)
        pause_reason, generation = ctrl.pause_verification_snapshot()
        callback_entered = threading.Event()
        release_callback = threading.Event()
        pause_attempted = threading.Event()
        pause_finished = threading.Event()
        resume_results: list[bool] = []

        def durable_resume_callback() -> None:
            callback_entered.set()
            assert release_callback.wait(timeout=2)

        def verified_resume() -> None:
            resume_results.append(
                ctrl.resume_if_pause_reason(
                    pause_reason,
                    expected_generation=generation,
                    on_resumed=durable_resume_callback,
                )
            )

        def concurrent_pause() -> None:
            pause_attempted.set()
            ctrl.pause(changed_reason)
            pause_finished.set()

        resume_thread = threading.Thread(target=verified_resume)
        pause_thread = threading.Thread(target=concurrent_pause)
        resume_thread.start()
        assert callback_entered.wait(timeout=1)
        pause_thread.start()
        assert pause_attempted.wait(timeout=1)
        assert pause_finished.wait(timeout=0.05) is False

        release_callback.set()
        resume_thread.join(timeout=2)
        pause_thread.join(timeout=2)

        assert resume_thread.is_alive() is False
        assert pause_thread.is_alive() is False
        assert resume_results == [True]
        assert ctrl.paused is True
        assert ctrl.pause_reason == changed_reason

    def test_verified_resume_callback_pause_is_not_reported_as_running(self) -> None:
        original_reason = "ORDER_RECONCILIATION_UNCERTAIN: original"
        changed_reason = "POSITION_RECONCILIATION_UNCERTAIN: callback changed state"
        ctrl = RiskController()
        ctrl.pause(original_reason)
        pause_reason, generation = ctrl.pause_verification_snapshot()

        resumed = ctrl.resume_if_pause_reason(
            pause_reason,
            expected_generation=generation,
            on_resumed=lambda: ctrl.pause(changed_reason),
        )

        assert resumed is False
        assert ctrl.paused is True
        assert ctrl.pause_reason == changed_reason

    def test_pause_records_auto_resume_metadata(self) -> None:
        from datetime import datetime, timezone

        paused_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
        ctrl = RiskController()

        ctrl.pause("rate limit", auto_resumable=True, paused_at=paused_at)

        assert ctrl.paused is True
        assert ctrl.pause_reason == "rate limit"
        assert ctrl.pause_auto_resumable is True
        assert ctrl.paused_at == paused_at

    def test_restore_pause_metadata(self) -> None:
        from datetime import datetime, timezone

        paused_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
        ctrl = RiskController()

        ctrl.restore_pause(True, "429 too many requests", paused_at, True)

        assert ctrl.paused is True
        assert ctrl.pause_reason == "429 too many requests"
        assert ctrl.pause_auto_resumable is True
        assert ctrl.paused_at == paused_at

    def test_kill_switch_rejected(self) -> None:
        ctrl = RiskController()
        ctrl.enable_kill_switch()
        result = ctrl.check()
        assert result.approved is False
        assert "kill" in result.reason.lower()

    def test_daily_loss_limit_reached(self) -> None:
        config = RiskConfig(max_daily_loss=100.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-100.0)
        result = ctrl.check()
        assert result.approved is False
        assert "daily loss" in result.reason.lower()

    def test_daily_loss_not_reached(self) -> None:
        config = RiskConfig(max_daily_loss=100.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-50.0)
        result = ctrl.check()
        assert result.approved is True

    def test_consecutive_losses_reached(self) -> None:
        config = RiskConfig(max_daily_loss=5000.0, max_consecutive_losses=2)
        ctrl = RiskController(config)
        ctrl.record_trade(-10.0)
        ctrl.record_trade(-10.0)
        result = ctrl.check()
        assert result.approved is False
        assert "consecutive" in result.reason.lower()

    def test_winning_resets_consecutive(self) -> None:
        config = RiskConfig(max_daily_loss=5000.0, max_consecutive_losses=3)
        ctrl = RiskController(config)
        ctrl.record_trade(-10.0)
        ctrl.record_trade(5.0)
        assert ctrl.consecutive_losses == 0
        result = ctrl.check()
        assert result.approved is True

    def test_drawdown_limit_latches_auto_resumable_pause_from_all_time_peak(self) -> None:
        ctrl = RiskController(
            RiskConfig(
                max_daily_loss=5000.0,
                max_consecutive_losses=10,
                max_drawdown_amount=30.0,
            )
        )

        ctrl.record_trade(100.0)
        ctrl.record_trade(-30.0)

        assert ctrl.cumulative_realized_pnl == 70.0
        assert ctrl.peak_realized_pnl == 100.0
        assert ctrl.drawdown_amount == 30.0
        assert ctrl.paused is True
        assert ctrl.pause_auto_resumable is True
        assert ctrl.pause_reason.startswith("DRAWDOWN_LIMIT:")
        assert ctrl.consume_drawdown_limit_reason() == ctrl.pause_reason
        assert ctrl.consume_drawdown_limit_reason() is None

    @pytest.mark.parametrize("limit", [None, 0.0])
    def test_drawdown_limit_is_disabled_when_unset_or_zero(
        self,
        limit: float | None,
    ) -> None:
        ctrl = RiskController(
            RiskConfig(
                max_daily_loss=5000.0,
                max_consecutive_losses=10,
                max_drawdown_amount=limit,
            )
        )

        ctrl.record_trade(100.0)
        ctrl.record_trade(-1000.0)

        assert ctrl.paused is False
        assert ctrl.consume_drawdown_limit_reason() is None

    def test_drawdown_peak_survives_day_rollover(self) -> None:
        from datetime import date

        days = iter([
            date(2026, 7, 18),
            date(2026, 7, 18),
            date(2026, 7, 19),
        ])
        ctrl = RiskController(
            RiskConfig(max_drawdown_amount=500.0),
            trade_day_provider=lambda: next(days),
        )
        ctrl.record_trade(100.0)

        ctrl.check()

        assert ctrl.daily_pnl == 0.0
        assert ctrl.cumulative_realized_pnl == 100.0
        assert ctrl.peak_realized_pnl == 100.0

    def test_restored_drawdown_retriggers_after_resume(self) -> None:
        ctrl = RiskController(RiskConfig(max_drawdown_amount=50.0))
        ctrl.restore_drawdown_state(
            cumulative_realized_pnl=80.0,
            peak_realized_pnl=100.0,
        )

        ctrl.record_trade(-30.0)
        first_reason = ctrl.consume_drawdown_limit_reason()
        ctrl.resume()
        ctrl.record_trade(-1.0)

        assert first_reason is not None
        assert ctrl.paused is True
        assert ctrl.peak_realized_pnl == 100.0
        assert ctrl.cumulative_realized_pnl == 49.0
        assert ctrl.consume_drawdown_limit_reason() == ctrl.pause_reason

    def test_disable_kill_switch(self) -> None:
        ctrl = RiskController()
        ctrl.enable_kill_switch()
        assert ctrl.kill_switch is True
        ctrl.disable_kill_switch()
        assert ctrl.kill_switch is False
        result = ctrl.check()
        assert result.approved is True

    def test_daily_pnl_reset_new_day(self) -> None:
        from datetime import timedelta

        ctrl = RiskController()
        ctrl._today = datetime.now(timezone.utc).date() - timedelta(days=1)
        ctrl.daily_pnl = -999999.0
        ctrl.consecutive_losses = 99
        result = ctrl.check()
        assert result.approved is True
        assert ctrl.daily_pnl == 0.0
        assert ctrl.consecutive_losses == 0

    def test_record_trade_resets_daily_pnl_on_new_day(self) -> None:
        from datetime import timedelta

        ctrl = RiskController()
        ctrl._today = datetime.now(timezone.utc).date() - timedelta(days=1)
        ctrl.daily_pnl = -100.0
        ctrl.record_trade(-50.0)
        assert ctrl.daily_pnl == -50.0

    def test_begin_day_resets_daily_pnl_when_day_changed(self) -> None:
        from datetime import timedelta

        ctrl = RiskController()
        ctrl._today = datetime.now(timezone.utc).date() - timedelta(days=1)
        ctrl.daily_pnl = -100.0
        ctrl.consecutive_losses = 5

        ctrl.begin_day()

        assert ctrl.daily_pnl == 0.0
        assert ctrl.consecutive_losses == 0

    def test_begin_day_does_not_reset_when_same_day(self) -> None:
        ctrl = RiskController()
        ctrl.daily_pnl = -100.0
        ctrl.consecutive_losses = 3

        ctrl.begin_day()

        assert ctrl.daily_pnl == -100.0
        assert ctrl.consecutive_losses == 3

    def test_daily_pnl_date_returns_today(self) -> None:
        ctrl = RiskController()
        assert ctrl.daily_pnl_date == datetime.now(timezone.utc).date()

    def test_check_keeps_utc_trade_day_during_local_midnight_gap(self) -> None:
        from datetime import date

        utc_day = datetime.now(timezone.utc).date()
        if date.today() == utc_day:
            pytest.skip("local date matches UTC date")

        ctrl = RiskController()
        ctrl.replace_daily_pnl(-42.0, 1, utc_day)

        result = ctrl.check()

        assert result.approved is True
        assert ctrl.daily_pnl == -42.0
        assert ctrl.consecutive_losses == 1

    def test_trade_day_provider_drives_day_boundary(self) -> None:
        from datetime import date

        days = iter([date(2026, 5, 22), date(2026, 5, 22), date(2026, 5, 23)])
        ctrl = RiskController(trade_day_provider=lambda: next(days))
        ctrl.daily_pnl = -100.0
        ctrl.consecutive_losses = 2

        # provider still returns 2026-05-22 → no reset
        ctrl.check()
        assert ctrl.daily_pnl == -100.0
        assert ctrl.consecutive_losses == 2

        # provider flips to 2026-05-23 → reset
        ctrl.check()
        assert ctrl.daily_pnl == 0.0
        assert ctrl.consecutive_losses == 0

    def test_set_trade_day_provider_updates_today(self) -> None:
        from datetime import date

        ctrl = RiskController()
        ctrl.set_trade_day_provider(lambda: date(2030, 1, 1))
        assert ctrl.daily_pnl_date == date(2030, 1, 1)

    def test_operational_pause_cannot_be_downgraded_by_manual_reason(self) -> None:
        ctrl = RiskController()
        operational = "ORDER_RECONCILIATION_UNCERTAIN: live order unknown"

        ctrl.pause(operational, auto_resumable=False)
        paused_at = ctrl.paused_at
        ctrl.pause("manual", auto_resumable=True)

        assert ctrl.paused is True
        assert ctrl.pause_reason == operational
        assert ctrl.paused_at == paused_at
        assert ctrl.pause_auto_resumable is False

    def test_protective_exit_permission_is_ephemeral_and_operational_only(self) -> None:
        ctrl = RiskController()

        ctrl.pause("manual")
        assert ctrl.permit_protective_exits() is False

        operational = "ORDER_RECONCILIATION_UNCERTAIN: broker proof required"
        ctrl.pause(operational)
        assert ctrl.permit_protective_exits() is True
        assert ctrl.protective_exit_permitted is True
        assert ctrl.paused is True

        ctrl.pause("manual confirmation")
        assert ctrl.pause_reason == operational
        assert ctrl.protective_exit_permitted is False

        assert ctrl.permit_protective_exits() is True
        ctrl.enable_kill_switch()
        assert ctrl.protective_exit_permitted is False

    def test_restored_operational_pause_does_not_restore_protective_permission(self) -> None:
        ctrl = RiskController()
        reason = "ORDER_SUBMISSION_UNCERTAIN: timeout"
        ctrl.pause(reason)
        assert ctrl.permit_protective_exits() is True

        ctrl.restore_pause(True, reason)

        assert ctrl.paused is True
        assert ctrl.protective_exit_permitted is False

    def test_pnl_reconciliation_pause_allows_only_verified_protective_exit(self) -> None:
        ctrl = RiskController()
        ctrl.pause(
            "PNL_RECONCILIATION_UNCERTAIN: incomplete current-day ledger",
            auto_resumable=False,
        )

        assert ctrl.check().approved is False
        assert ctrl.protective_exit_permitted is False
        assert ctrl.permit_protective_exits() is True
        assert ctrl.protective_exit_permitted is True

        pause_reason, generation = ctrl.pause_verification_snapshot()
        latched, current_reason = ctrl.pause_unless_operational(pause_reason)

        assert latched is False
        assert current_reason == pause_reason
        assert ctrl.pause_verification_snapshot() == (pause_reason, generation)
        assert ctrl.protective_exit_permitted is True

    def test_new_pnl_hazard_preserves_existing_protective_authorization(
        self,
    ) -> None:
        ctrl = RiskController()
        existing = "ORDER_SUBMISSION_UNCERTAIN: acknowledgement missing"
        ctrl.pause(existing)
        assert ctrl.permit_protective_exits() is True
        _, generation = ctrl.pause_verification_snapshot()

        latched, current_reason = ctrl.pause_unless_operational(
            "PNL_RECONCILIATION_UNCERTAIN: incomplete ledger"
        )

        assert latched is False
        assert current_reason == existing
        assert ctrl.pause_reason == existing
        assert ctrl.protective_exit_permitted is True
        assert ctrl.pause_verification_snapshot()[1] == generation

    def test_unknown_submission_wins_concurrent_pnl_latch(self) -> None:
        for _ in range(25):
            ctrl = RiskController()
            barrier = threading.Barrier(3)
            unknown_reason = "ORDER_SUBMISSION_UNCERTAIN: broker outcome unknown"
            pnl_reason = "PNL_RECONCILIATION_UNCERTAIN: incomplete ledger"

            def latch_pnl() -> None:
                barrier.wait()
                ctrl.pause_unless_operational(pnl_reason)

            def latch_unknown_submission() -> None:
                barrier.wait()
                ctrl.pause(unknown_reason)

            pnl_thread = threading.Thread(target=latch_pnl)
            order_thread = threading.Thread(target=latch_unknown_submission)
            pnl_thread.start()
            order_thread.start()
            barrier.wait()
            pnl_thread.join()
            order_thread.join()

            assert ctrl.pause_reason == unknown_reason
            assert ctrl.protective_exit_permitted is False

    def test_existing_pause_wins_concurrent_transient_pause_attempt(self) -> None:
        for _ in range(25):
            ctrl = RiskController()
            barrier = threading.Barrier(3)
            transient_reason = "post-fill PnL reconciliation in progress: AAPL.US"
            daily_loss_reason = "daily loss limit reached"

            def latch_transient() -> None:
                barrier.wait()
                ctrl.begin_entry_reconciliation(transient_reason)

            def latch_daily_loss() -> None:
                barrier.wait()
                ctrl.pause(daily_loss_reason, auto_resumable=False)

            transient_thread = threading.Thread(target=latch_transient)
            loss_thread = threading.Thread(target=latch_daily_loss)
            transient_thread.start()
            loss_thread.start()
            barrier.wait()
            transient_thread.join()
            loss_thread.join()

            assert ctrl.pause_reason == daily_loss_reason
            assert ctrl.check().approved is False
            ctrl.finish_entry_reconciliation()

    def test_new_fill_revokes_stale_protective_exit_authorization(self) -> None:
        ctrl = RiskController()
        reason = "PNL_RECONCILIATION_UNCERTAIN: incomplete ledger"
        ctrl.pause(reason)
        assert ctrl.permit_protective_exits() is True

        count, pause_created = ctrl.begin_entry_reconciliation(
            "post-fill PnL reconciliation in progress: AAPL.US"
        )

        assert count == 1
        assert pause_created is False
        assert ctrl.pause_reason == reason
        assert ctrl.protective_exit_permitted is False
        assert ctrl.check().approved is False
        ctrl.finish_entry_reconciliation()

    def test_reduction_fill_preserves_verified_protective_exit_authorization(
        self,
    ) -> None:
        ctrl = RiskController()
        reason = "PNL_RECONCILIATION_UNCERTAIN: incomplete ledger"
        ctrl.pause(reason)
        assert ctrl.permit_protective_exits() is True

        count, pause_created = ctrl.begin_entry_reconciliation(
            "post-fill PnL reconciliation in progress: AAPL.US",
            preserve_protective_exits=True,
        )

        assert count == 1
        assert pause_created is False
        assert ctrl.pause_reason == reason
        assert ctrl.protective_exit_permitted is True
        assert ctrl.check().approved is False
        ctrl.finish_entry_reconciliation()
        assert ctrl.protective_exit_permitted is True

    def test_verified_resume_is_idempotent_when_already_running(self) -> None:
        ctrl = RiskController()
        reason, generation = ctrl.pause_verification_snapshot()

        assert ctrl.resume_if_pause_reason(
            reason,
            expected_generation=generation,
        ) is True

        ctrl.enable_kill_switch()
        reason, generation = ctrl.pause_verification_snapshot()
        assert ctrl.resume_if_pause_reason(
            reason,
            expected_generation=generation,
        ) is False

    def test_ignored_manual_pause_invalidates_operational_verification(self) -> None:
        ctrl = RiskController()
        operational = "ORDER_EXECUTION_BLOCKED: broker proof required"
        ctrl.pause(operational)
        reason, generation = ctrl.pause_verification_snapshot()

        ctrl.pause("manual pause")

        assert ctrl.pause_reason == operational
        assert ctrl.resume_if_pause_reason(
            reason,
            expected_generation=generation,
        ) is False
        assert ctrl.paused is True
