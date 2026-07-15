from datetime import datetime, timezone

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
