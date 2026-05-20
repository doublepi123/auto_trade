from app.core.risk import RiskConfig, RiskController


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
        from datetime import date, timedelta

        ctrl = RiskController()
        ctrl._today = date.today() - timedelta(days=1)
        ctrl.daily_pnl = -999999.0
        ctrl.consecutive_losses = 99
        result = ctrl.check()
        assert result.approved is True
        assert ctrl.daily_pnl == 0.0
        assert ctrl.consecutive_losses == 0

    def test_record_trade_resets_daily_pnl_on_new_day(self) -> None:
        from datetime import date, timedelta

        ctrl = RiskController()
        ctrl._today = date.today() - timedelta(days=1)
        ctrl.daily_pnl = -100.0
        ctrl.record_trade(-50.0)
        assert ctrl.daily_pnl == -50.0

    def test_begin_day_resets_daily_pnl_when_day_changed(self) -> None:
        from datetime import date, timedelta

        ctrl = RiskController()
        ctrl._today = date.today() - timedelta(days=1)
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
        from datetime import date

        ctrl = RiskController()
        assert ctrl.daily_pnl_date == date.today()
