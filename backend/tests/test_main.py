import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import main as main_module


@pytest.mark.asyncio
async def test_lifespan_logs_runner_start_failure_without_crashing(monkeypatch) -> None:
    class FailingRunner:
        def start(self, *, loop=None) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: FailingRunner())

    async with main_module.lifespan(main_module.app):
        pass


@pytest.mark.asyncio
async def test_lifespan_passes_application_loop_to_runner(monkeypatch) -> None:
    started_with = []

    class RecordingRunner:
        def start(self, *, loop=None) -> bool:
            started_with.append(loop)
            return True

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: RecordingRunner())

    current_loop = asyncio.get_running_loop()
    async with main_module.lifespan(main_module.app):
        pass

    assert started_with == [current_loop]


class TestPriceDriftPct:
    def test_zero_baseline_returns_zero(self) -> None:
        assert main_module._price_drift_pct(110.0, 0.0) == 0.0

    def test_zero_current_returns_zero(self) -> None:
        assert main_module._price_drift_pct(0.0, 100.0) == 0.0

    def test_positive_drift(self) -> None:
        assert main_module._price_drift_pct(105.0, 100.0) == 5.0

    def test_negative_drift(self) -> None:
        assert main_module._price_drift_pct(95.0, 100.0) == 5.0

    def test_no_drift(self) -> None:
        assert main_module._price_drift_pct(100.0, 100.0) == 0.0


class TestShouldRunLLMAnalysis:
    def test_time_gate_passed_no_baseline(self) -> None:
        time_passed, vol_triggered = main_module._should_run_llm_analysis(
            current_price=100.0,
            last_trigger_price=0.0,
            threshold_pct=1.0,
            last_analysis_at=None,
            interval_minutes=2,
            now=datetime.now(timezone.utc),
        )
        assert time_passed is True
        assert vol_triggered is False

    def test_time_gate_blocked_volatility_triggered(self) -> None:
        now = datetime.now(timezone.utc)
        time_passed, vol_triggered = main_module._should_run_llm_analysis(
            current_price=105.0,
            last_trigger_price=100.0,
            threshold_pct=1.0,
            last_analysis_at=now - timedelta(minutes=1),
            interval_minutes=2,
            now=now,
        )
        assert time_passed is False
        assert vol_triggered is True

    def test_time_gate_passed_volatility_not_triggered(self) -> None:
        now = datetime.now(timezone.utc)
        time_passed, vol_triggered = main_module._should_run_llm_analysis(
            current_price=100.5,
            last_trigger_price=100.0,
            threshold_pct=1.0,
            last_analysis_at=now - timedelta(minutes=3),
            interval_minutes=2,
            now=now,
        )
        assert time_passed is True
        assert vol_triggered is False

    def test_both_blocked(self) -> None:
        now = datetime.now(timezone.utc)
        time_passed, vol_triggered = main_module._should_run_llm_analysis(
            current_price=100.5,
            last_trigger_price=100.0,
            threshold_pct=1.0,
            last_analysis_at=now - timedelta(minutes=1),
            interval_minutes=2,
            now=now,
        )
        assert time_passed is False
        assert vol_triggered is False

    def test_naive_last_analysis_at_gets_tz(self) -> None:
        now = datetime.now(timezone.utc)
        time_passed, _ = main_module._should_run_llm_analysis(
            current_price=100.0,
            last_trigger_price=0.0,
            threshold_pct=1.0,
            last_analysis_at=now.replace(tzinfo=None) - timedelta(minutes=3),
            interval_minutes=2,
            now=now,
        )
        assert time_passed is True


class TestLLMAnalysisTick:
    @pytest.fixture(autouse=True)
    def reset_trigger_price(self) -> None:
        main_module._last_llm_trigger_price = 0.0
        yield
        main_module._last_llm_trigger_price = 0.0

    def _make_fake_config(self, **overrides: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "auto_interval_enabled": True,
            "symbol": "AAPL.US",
            "market": "US",
            "llm_interval_minutes": 2,
            "llm_last_analysis_at": None,
            "buy_low": 100.0,
            "sell_high": 110.0,
            "short_selling": False,
            "min_profit_amount": 0.0,
            "trading_session_mode": "ANY",
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _make_fake_runner(self, last_price: float = 100.0) -> MagicMock:
        runner = MagicMock()
        runner.engine = SimpleNamespace(last_price=last_price)
        runner.broker = MagicMock()
        runner.recent_price_context.return_value = []
        runner.execute_llm_order_decision.return_value = {"status": "NO_ACTION", "order_id": None}
        return runner

    def _patch_tick_deps(self, monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace, runner: MagicMock) -> MagicMock:
        class FakeDB:
            def commit(self) -> None:
                pass

            def close(self) -> None:
                pass

            def __enter__(self) -> "FakeDB":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        fake_db = FakeDB()

        class FakeStrategyService:
            def __init__(self, db: object) -> None:
                pass

            def get_config(self) -> SimpleNamespace:
                return config

        monkeypatch.setattr("app.database.SessionLocal", lambda: fake_db)
        monkeypatch.setattr("app.services.strategy_service.StrategyService", FakeStrategyService)
        monkeypatch.setattr("app.runner.get_runner", lambda: runner)
        monkeypatch.setattr("app.services.llm_advisor_service.build_recent_analysis_context", lambda cfg: [])
        monkeypatch.setattr("app.main.record_trade_event", lambda *a, **kw: None)
        monkeypatch.setattr(
            "app.api.llm_advisor._position_context",
            lambda symbol, price: {"side": "FLAT", "quantity": 0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0},
        )
        monkeypatch.setattr("app.api.llm_advisor._account_context", lambda *a: {})
        monkeypatch.setattr("app.api.llm_advisor._interval_reference_quantity", lambda *a: 1.0)
        monkeypatch.setattr(
            "app.services.llm_interaction_service.LLMInteractionService",
            lambda db: MagicMock(update_outcome=lambda *a, **kw: None),
        )

        analyze_calls: list[dict[str, object]] = []

        class FakeAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                analyze_calls.append(kwargs)
                return {
                    "success": True,
                    "suggested_buy_low": 98.0,
                    "suggested_sell_high": 112.0,
                    "confidence_score": 0.85,
                    "order_action": "NONE",
                    "interaction_id": 42,
                }

        monkeypatch.setattr("app.services.llm_advisor_service.LLMAdvisorService", FakeAdvisor)
        monkeypatch.setattr(
            "app.main.IntervalApplicationService",
            lambda: MagicMock(
                apply_direct_suggestion=lambda **kw: {"applied": True, "reason": "ok"}
            ),
        )

        return analyze_calls

    @pytest.mark.asyncio
    async def test_tick_runs_when_time_gate_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=100.0)
        calls = self._patch_tick_deps(monkeypatch, config, runner)

        await main_module._llm_analysis_tick()

        assert len(calls) == 1
        assert calls[0]["current_price"] == 100.0

    @pytest.mark.asyncio
    async def test_tick_skipped_when_time_gate_blocked_and_no_volatility(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=1))
        runner = self._make_fake_runner(last_price=100.5)
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        main_module._last_llm_trigger_price = 100.0

        await main_module._llm_analysis_tick()

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_tick_runs_when_volatility_triggered_despite_time_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=1))
        runner = self._make_fake_runner(last_price=105.0)
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        main_module._last_llm_trigger_price = 100.0

        await main_module._llm_analysis_tick()

        assert len(calls) == 1
        assert calls[0]["current_price"] == 105.0
        assert main_module._last_llm_trigger_price == 105.0

    @pytest.mark.asyncio
    async def test_tick_skipped_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = self._make_fake_config(auto_interval_enabled=False)
        runner = self._make_fake_runner(last_price=105.0)
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        main_module._last_llm_trigger_price = 100.0

        await main_module._llm_analysis_tick()

        assert len(calls) == 0
        assert main_module._last_llm_trigger_price == 100.0

    @pytest.mark.asyncio
    async def test_tick_skipped_when_rth_only_and_non_rth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(
            trading_session_mode="RTH_ONLY",
            llm_last_analysis_at=now - timedelta(minutes=3),
        )
        runner = self._make_fake_runner(last_price=100.0)
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr("app.core.market_calendar.is_trading_hours", lambda market: False)

        await main_module._llm_analysis_tick()

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_tick_runs_when_rth_only_and_rth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(
            trading_session_mode="RTH_ONLY",
            llm_last_analysis_at=now - timedelta(minutes=3),
        )
        runner = self._make_fake_runner(last_price=100.0)
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr("app.core.market_calendar.is_trading_hours", lambda market: True)

        await main_module._llm_analysis_tick()

        assert len(calls) == 1
