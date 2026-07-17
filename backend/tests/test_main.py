from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import main as main_module
from app.core.engine import StrategyParams


@pytest.mark.asyncio
async def test_lifespan_fails_startup_when_runner_cannot_start(monkeypatch) -> None:
    class FailingRunner:
        def start(self, *, loop=None) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "get_runner", lambda: FailingRunner())

    with pytest.raises(RuntimeError, match="runner failed to start during app lifespan"):
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


async def test_llm_storage_maintenance_waits_for_worker_during_cancel(
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_tick() -> None:
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(
        main_module,
        "_llm_storage_maintenance_tick_sync",
        blocking_tick,
    )
    task = asyncio.create_task(main_module._run_llm_storage_maintenance_tick())
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_strategy_v2_shadow_tick_is_isolated_from_execution(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    collections: list[dict[str, object]] = []

    class FakeQuery:
        def __init__(self, values: list[tuple[str]]) -> None:
            self.values = values

        def filter(self, *_args: object) -> "FakeQuery":
            return self

        def distinct(self) -> "FakeQuery":
            return self

        def all(self) -> list[tuple[str]]:
            return self.values

    class FakeDB:
        closed = False
        rolled_back = 0

        def __init__(self) -> None:
            self.query_count = 0

        def query(self, *_args: object) -> FakeQuery:
            self.query_count += 1
            return FakeQuery(
                [("0700.HK",), ("NVDA.US",)]
                if self.query_count == 1
                else [("MSFT.US",)]
            )

        def rollback(self) -> None:
            self.rolled_back += 1

        def close(self) -> None:
            self.closed = True

    db = FakeDB()
    broker = object()
    runner = SimpleNamespace(broker=broker, _trade_svc=MagicMock())

    class FakeStrategyService:
        def __init__(self, received_db: object) -> None:
            assert received_db is db

        def get_config(self) -> SimpleNamespace:
            return SimpleNamespace(symbol="NVDA.US", market="US")

    class FakeShadowService:
        def __init__(self, received_db: object, candle_provider: object) -> None:
            assert received_db is db
            assert candle_provider is broker

        def tick(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if kwargs["symbol"] == "MSFT.US":
                raise RuntimeError("isolated symbol failure")

        def collect_forward_validation(self, **kwargs: object) -> None:
            collections.append(kwargs)

    monkeypatch.setattr(main_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(main_module, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    monkeypatch.setattr(
        "app.services.strategy_v2_shadow_service.StrategyV2ShadowService",
        FakeShadowService,
    )

    main_module._strategy_v2_shadow_tick_sync()

    assert calls == [
        {"symbol": "0700.HK", "market": "HK"},
        {"symbol": "MSFT.US", "market": "US"},
        {"symbol": "NVDA.US", "market": "US"},
    ]
    assert collections == calls
    assert db.rolled_back == 1
    assert db.closed is True
    runner._trade_svc.execute.assert_not_called()


def test_strategy_v2_shadow_tick_skips_without_primary_symbol(monkeypatch) -> None:
    class FakeQuery:
        def filter(self, *_args: object) -> "FakeQuery":
            return self

        def distinct(self) -> "FakeQuery":
            return self

        def all(self) -> list[tuple[str]]:
            return []

    class FakeDB:
        closed = False

        def query(self, *_args: object) -> FakeQuery:
            return FakeQuery()

        def close(self) -> None:
            self.closed = True

    db = FakeDB()

    class FakeStrategyService:
        def __init__(self, _db: object) -> None:
            pass

        def get_config(self) -> SimpleNamespace:
            return SimpleNamespace(symbol="", market="US")

    shadow_service = MagicMock()
    monkeypatch.setattr(main_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(main_module, "StrategyService", FakeStrategyService)
    monkeypatch.setattr(
        "app.services.strategy_v2_shadow_service.StrategyV2ShadowService",
        shadow_service,
    )

    main_module._strategy_v2_shadow_tick_sync()

    shadow_service.assert_not_called()
    assert db.closed is True


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
    def reset_trigger_price(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        monkeypatch.setattr(main_module.settings, "llm_shadow_mode", False)
        main_module._last_llm_trigger_price = 0.0
        main_module._last_llm_trigger_price_by_symbol = {}
        main_module._llm_last_analysis_at_by_symbol = {}
        main_module._llm_analysis_timestamps = []
        yield
        main_module._last_llm_trigger_price = 0.0
        main_module._last_llm_trigger_price_by_symbol = {}
        main_module._llm_last_analysis_at_by_symbol = {}
        main_module._llm_analysis_timestamps = []

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
        runner.engine = SimpleNamespace(last_price=last_price, state=SimpleNamespace(value="flat"))
        runner.broker = MagicMock()
        runner.fresh_market_price.return_value = last_price
        runner.recent_price_context.return_value = []
        runner.execute_llm_order_decision.return_value = {"status": "NO_ACTION", "order_id": None}
        return runner

    def _patch_tick_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: SimpleNamespace,
        runner: MagicMock,
        schedule_state: SimpleNamespace | None = None,
    ) -> list[dict[str, object]]:
        class FakeDB:
            def commit(self) -> None:
                pass

            def rollback(self) -> None:
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
        monkeypatch.setattr(
            "app.api.llm_advisor._interval_reference_quantity",
            lambda *args, **kwargs: 1.0,
        )
        monkeypatch.setattr(
            "app.services.llm_interaction_service.LLMInteractionService",
            lambda db: MagicMock(update_outcome=lambda *a, **kw: None),
        )
        class FakeStateService:
            def __init__(self, db: object) -> None:
                pass

            def count_analyses_last_hour(self, now: datetime) -> int:
                return 0

            def get_state(self, symbol: str, market: str):
                return schedule_state or SimpleNamespace(
                    symbol=symbol,
                    market=market,
                    last_analysis_at=None,
                    next_analysis_at=None,
                    last_status="",
                    last_skip_reason="",
                )

            def record_analysis(self, symbol: str, market: str, *, analyzed_at: datetime, next_analysis_at: datetime | None) -> None:
                if schedule_state is not None:
                    schedule_state.last_status = "ANALYZED"
                    schedule_state.last_analysis_at = analyzed_at
                    schedule_state.next_analysis_at = next_analysis_at
                return None

            def record_skip(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
                if schedule_state is not None:
                    schedule_state.last_status = "SKIPPED"
                    schedule_state.next_analysis_at = next_analysis_at
                return None

            def record_failure(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
                if schedule_state is not None:
                    schedule_state.last_status = "FAILED"
                    schedule_state.next_analysis_at = next_analysis_at
                return None

        monkeypatch.setattr("app.main.LLMSymbolStateService", FakeStateService)


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
                apply_suggestion=lambda **kw: {"applied": True, "reason": "ok"}
            ),
        )
        monkeypatch.setattr("app.api.strategy._reload_strategy_after_save", lambda: None)

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
    async def test_tick_honors_failed_provider_backoff_without_overwriting_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=10))
        runner = self._make_fake_runner(last_price=100.0)
        runner.fresh_market_price.return_value = None
        state = SimpleNamespace(
            symbol="AAPL.US",
            market="US",
            last_analysis_at=now - timedelta(minutes=10),
            next_analysis_at=now + timedelta(minutes=5),
            last_status="FAILED",
            last_skip_reason="MiniMax overloaded",
        )
        calls = self._patch_tick_deps(
            monkeypatch,
            config,
            runner,
            schedule_state=state,
        )

        await main_module._llm_analysis_tick()

        assert calls == []
        assert state.last_status == "FAILED"
        runner.fresh_market_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_starts_provider_backoff_at_failure_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=10))
        runner = self._make_fake_runner(last_price=100.0)
        state = SimpleNamespace(
            symbol="AAPL.US",
            market="US",
            last_analysis_at=now - timedelta(minutes=10),
            next_analysis_at=None,
            last_status="",
            last_skip_reason="",
        )
        self._patch_tick_deps(monkeypatch, config, runner, schedule_state=state)

        class FailedAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                time.sleep(0.05)
                return {
                    "success": False,
                    "error": "MiniMax overloaded",
                    "failure_kind": "HTTP_529",
                    "transient": True,
                    "retry_after_seconds": 300,
                }

        monkeypatch.setattr(
            "app.services.llm_advisor_service.LLMAdvisorService",
            FailedAdvisor,
        )

        await main_module._llm_analysis_tick()
        finished_at = datetime.now(timezone.utc)

        assert state.last_status == "FAILED"
        assert state.next_analysis_at is not None
        assert state.next_analysis_at >= finished_at + timedelta(seconds=299)

    @pytest.mark.asyncio
    async def test_tick_schedules_next_analysis_from_completion_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=10))
        runner = self._make_fake_runner(last_price=100.0)
        state = SimpleNamespace(
            symbol="AAPL.US",
            market="US",
            last_analysis_at=now - timedelta(minutes=10),
            next_analysis_at=None,
            last_status="",
            last_skip_reason="",
        )
        self._patch_tick_deps(monkeypatch, config, runner, schedule_state=state)

        class SlowSuccessfulAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                time.sleep(0.05)
                return {
                    "success": True,
                    "suggested_buy_low": 98.0,
                    "suggested_sell_high": 112.0,
                    "confidence_score": 0.85,
                    "order_action": "NONE",
                    "interaction_id": 42,
                }

        monkeypatch.setattr(
            "app.services.llm_advisor_service.LLMAdvisorService",
            SlowSuccessfulAdvisor,
        )
        started_at = datetime.now(timezone.utc)

        await main_module._llm_analysis_tick()

        assert state.last_analysis_at >= started_at + timedelta(seconds=0.04)
        assert state.next_analysis_at == state.last_analysis_at + timedelta(minutes=2)

    @pytest.mark.asyncio
    async def test_tick_collects_blocking_context_in_worker_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=100.0)
        self._patch_tick_deps(monkeypatch, config, runner)
        to_thread_calls: list[str] = []
        original_to_thread = asyncio.to_thread

        async def fake_to_thread(fn, /, *args, **kwargs):
            to_thread_calls.append(getattr(fn, "__name__", str(fn)))
            return await original_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(main_module.asyncio, "to_thread", fake_to_thread)

        await main_module._llm_analysis_tick()

        assert "_collect_llm_contexts" in to_thread_calls

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

    @pytest.mark.asyncio
    async def test_tick_respects_symbol_budget_and_only_applies_primary_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(symbol="AAPL.US", market="US", llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=101.0)
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._symbol_runtimes = {
            "AAPL.US": SimpleNamespace(symbol="AAPL.US", market="US", engine=runner.engine),
            "NVDA.US": SimpleNamespace(
                symbol="NVDA.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=220.0,
                    params=StrategyParams(symbol="NVDA.US", market="US", buy_low=0.0, sell_high=0.0),
                ),
            ),
            "MSFT.US": SimpleNamespace(
                symbol="MSFT.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=330.0,
                    params=StrategyParams(symbol="MSFT.US", market="US", buy_low=0.0, sell_high=0.0),
                ),
            ),
        }
        runner.recent_price_context.side_effect = lambda symbol=None: [{"symbol": symbol}] if symbol else []
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        apply_calls: list[dict[str, object]] = []
        monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 2)
        monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)
        monkeypatch.setattr(
            "app.main.IntervalApplicationService",
            lambda: MagicMock(apply_suggestion=lambda **kw: apply_calls.append(kw) or {"applied": True, "reason": "ok"}),
        )

        await main_module._llm_analysis_tick()

        assert [call["symbol"] for call in calls] == ["AAPL.US", "NVDA.US"]
        assert calls[0]["persist"] is True
        assert calls[1]["persist"] is False
        assert apply_calls and apply_calls[0]["reference_quantity"] == 1.0

    @pytest.mark.asyncio
    async def test_tick_shadow_still_delegates_to_interval_policy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=100.0)
        self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr(main_module.settings, "llm_shadow_mode", True)
        apply_calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            "app.main.IntervalApplicationService",
            lambda: MagicMock(
                apply_suggestion=lambda **kwargs: apply_calls.append(kwargs)
                or {
                    "success": True,
                    "applied": False,
                    "reason": "validated shadow interval",
                    "policy_status": "SHADOW",
                }
            ),
        )

        await main_module._llm_analysis_tick()

        assert len(apply_calls) == 1
        assert apply_calls[0]["current_price"] == 100.0

    @pytest.mark.asyncio
    async def test_tick_records_order_policy_outcome_in_interaction_and_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=100.0)
        self._patch_tick_deps(monkeypatch, config, runner)
        runner.execute_llm_order_decision.return_value = {
            "executed": False,
            "status": "POLICY_REJECTED",
            "order_id": None,
            "policy_code": "PRICE_DEVIATION",
            "confidence": 0.85,
            "reference_price": 100.0,
            "candidate_price": 102.0,
            "deviation_pct": 2.0,
        }
        outcome_updates: list[dict[str, object]] = []
        analysis_events: list[dict[str, object]] = []

        class FakeAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **_kwargs: object) -> dict[str, object]:
                return {
                    "success": True,
                    "suggested_buy_low": 98.0,
                    "suggested_sell_high": 102.0,
                    "confidence_score": 0.85,
                    "order_action": "BUY_NOW",
                    "order_price": 102.0,
                    "interaction_id": 77,
                }

        class FakeInteractionService:
            def __init__(self, _db: object) -> None:
                pass

            def update_outcome(self, interaction_id: int, **kwargs: object) -> None:
                outcome_updates.append({"interaction_id": interaction_id, **kwargs})

        monkeypatch.setattr("app.services.llm_advisor_service.LLMAdvisorService", FakeAdvisor)
        monkeypatch.setattr(
            "app.services.llm_interaction_service.LLMInteractionService",
            FakeInteractionService,
        )
        monkeypatch.setattr(
            main_module,
            "record_trade_event",
            lambda *_args, **kwargs: analysis_events.append(kwargs),
        )

        await main_module._llm_analysis_tick()

        expected_policy_outcome = {
            "code": "PRICE_DEVIATION",
            "reference_price": 100.0,
            "candidate_price": 102.0,
            "deviation_pct": 2.0,
            "confidence": 0.85,
            "disposition": "REJECT",
        }
        assert outcome_updates[0]["interaction_id"] == 77
        assert outcome_updates[0]["policy_outcome"] == expected_policy_outcome
        event_payload = analysis_events[0]["payload"]
        assert isinstance(event_payload, dict)
        assert event_payload["policy_outcome"] == expected_policy_outcome

    @pytest.mark.asyncio
    async def test_tick_uses_secondary_symbol_interval_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(symbol="AAPL.US", market="US", llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=101.0)
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._symbol_runtimes = {
            "AAPL.US": SimpleNamespace(symbol="AAPL.US", market="US", engine=runner.engine),
            "NVDA.US": SimpleNamespace(
                symbol="NVDA.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=220.0,
                    params=StrategyParams(symbol="NVDA.US", market="US", buy_low=190.0, sell_high=230.0),
                ),
            ),
        }
        runner.recent_price_context.side_effect = lambda symbol=None: []
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 2)
        monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)

        class FakeAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                if kwargs["symbol"] == "NVDA.US":
                    assert kwargs["current_buy_low"] == 190.0
                    assert kwargs["current_sell_high"] == 230.0
                return {
                    "success": True,
                    "suggested_buy_low": 188.0,
                    "suggested_sell_high": 232.0,
                    "confidence_score": 0.9,
                    "order_action": "NONE",
                    "interaction_id": 44,
                }

        monkeypatch.setattr("app.services.llm_advisor_service.LLMAdvisorService", FakeAdvisor)

        await main_module._llm_analysis_tick()

        assert any(call["symbol"] == "NVDA.US" for call in calls)

    @pytest.mark.asyncio
    async def test_tick_keeps_secondary_symbol_order_action_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(symbol="AAPL.US", market="US", llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=101.0)
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._symbol_runtimes = {
            "AAPL.US": SimpleNamespace(symbol="AAPL.US", market="US", engine=runner.engine),
            "NVDA.US": SimpleNamespace(
                symbol="NVDA.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=220.0,
                    params=StrategyParams(symbol="NVDA.US", market="US", buy_low=0.0, sell_high=0.0),
                ),
            ),
        }
        runner.recent_price_context.side_effect = lambda symbol=None: []
        execute_calls: list[dict[str, object]] = []
        runner.execute_llm_order_decision.side_effect = lambda decision: execute_calls.append(decision) or {"status": "SUBMITTED", "order_id": "order-nvda"}
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 2)
        monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)
        monkeypatch.setattr(
            "app.main.IntervalApplicationService",
            lambda: MagicMock(apply_suggestion=lambda **kw: {"applied": True, "reason": "ok"}),
        )

        class FakeAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                if kwargs["symbol"] == "NVDA.US":
                    return {
                        "success": True,
                        "suggested_buy_low": 0.0,
                        "suggested_sell_high": 0.0,
                        "confidence_score": 0.7,
                        "order_action": "BUY_NOW",
                        "interaction_id": 43,
                    }
                return {
                    "success": True,
                    "suggested_buy_low": 98.0,
                    "suggested_sell_high": 112.0,
                    "confidence_score": 0.85,
                    "order_action": "NONE",
                    "interaction_id": 42,
                }

        monkeypatch.setattr("app.services.llm_advisor_service.LLMAdvisorService", FakeAdvisor)

        await main_module._llm_analysis_tick()

        assert [call["symbol"] for call in calls] == ["AAPL.US", "NVDA.US"]
        assert execute_calls == []

    @pytest.mark.asyncio
    async def test_tick_records_budget_skip_reason_for_unanalyzed_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(symbol="AAPL.US", market="US", llm_last_analysis_at=now - timedelta(minutes=3))
        runner = self._make_fake_runner(last_price=101.0)
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._symbol_runtimes = {
            "AAPL.US": SimpleNamespace(symbol="AAPL.US", market="US", engine=runner.engine),
            "NVDA.US": SimpleNamespace(
                symbol="NVDA.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=220.0,
                    params=StrategyParams(symbol="NVDA.US", market="US", buy_low=0.0, sell_high=0.0),
                ),
            ),
            "MSFT.US": SimpleNamespace(
                symbol="MSFT.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=330.0,
                    params=StrategyParams(symbol="MSFT.US", market="US", buy_low=0.0, sell_high=0.0),
                ),
            ),
        }
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 1)
        monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)

        skip_calls: list[tuple[str, str]] = []

        class FakeStateService:
            def __init__(self, db: object) -> None:
                pass

            def count_analyses_last_hour(self, now: datetime) -> int:
                return 0

            def get_state(self, symbol: str, market: str):
                return SimpleNamespace(symbol=symbol, market=market, last_analysis_at=None, next_analysis_at=None, last_skip_reason="")
            def record_analysis(self, symbol: str, market: str, *, analyzed_at: datetime, next_analysis_at: datetime | None) -> None:
                return None

            def record_failure(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
                return None

            def record_skip(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
                skip_calls.append((symbol, reason))

        monkeypatch.setattr("app.main.LLMSymbolStateService", FakeStateService)

        await main_module._llm_analysis_tick()

        assert [call["symbol"] for call in calls] == ["AAPL.US"]
        assert skip_calls == [
            ("NVDA.US", "cycle budget exhausted"),
            ("MSFT.US", "cycle budget exhausted"),
        ]

    @pytest.mark.asyncio
    async def test_failed_provider_call_consumes_cycle_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        config = self._make_fake_config(
            symbol="AAPL.US",
            market="US",
            llm_last_analysis_at=now - timedelta(minutes=10),
        )
        runner = self._make_fake_runner(last_price=101.0)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        runner._symbol_runtimes = {
            "AAPL.US": SimpleNamespace(
                symbol="AAPL.US",
                market="US",
                engine=runner.engine,
            ),
            "NVDA.US": SimpleNamespace(
                symbol="NVDA.US",
                market="US",
                engine=SimpleNamespace(
                    last_price=220.0,
                    params=StrategyParams(symbol="NVDA.US", market="US"),
                ),
            ),
        }
        calls = self._patch_tick_deps(monkeypatch, config, runner)
        monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 1)
        monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)

        class FailedAdvisor:
            def __init__(self, broker: object = None) -> None:
                pass

            def analyze(self, **kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return {
                    "success": False,
                    "error": "MiniMax overloaded",
                    "failure_kind": "HTTP_529",
                    "transient": True,
                    "retry_after_seconds": 300,
                }

        monkeypatch.setattr(
            "app.services.llm_advisor_service.LLMAdvisorService",
            FailedAdvisor,
        )

        await main_module._llm_analysis_tick()

        assert [call["symbol"] for call in calls] == ["AAPL.US"]


@pytest.mark.asyncio
async def test_llm_tick_persists_skip_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base, LLMSymbolScheduleState

    db_path = tmp_path / "llm_skip_persist.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", session_factory)

    now = datetime.now(timezone.utc)
    config = SimpleNamespace(
        auto_interval_enabled=True,
        symbol="AAPL.US",
        market="US",
        buy_low=100.0,
        sell_high=110.0,
        short_selling=False,
        min_profit_amount=0.0,
        llm_interval_minutes=2,
        llm_last_analysis_at=now - timedelta(minutes=3),
        trading_session_mode="ANY",
    )
    runner = MagicMock()
    runner.engine = SimpleNamespace(
        params=StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0),
        last_price=101.0,
    )
    runner.broker = MagicMock()
    runner._symbol_runtimes = {
        "AAPL.US": SimpleNamespace(symbol="AAPL.US", market="US", engine=runner.engine, recent_quotes=[]),
        "MSFT.US": SimpleNamespace(
            symbol="MSFT.US",
            market="US",
            engine=SimpleNamespace(
                last_price=330.0,
                params=StrategyParams(symbol="MSFT.US", market="US"),
            ),
            recent_quotes=[],
        ),
    }
    runner.execute_llm_order_decision.return_value = {"status": "NO_ACTION", "order_id": None}
    runner.fresh_market_price.side_effect = lambda symbol: (
        101.0 if symbol == "AAPL.US" else 330.0
    )

    class FakeStrategyService:
        def __init__(self, db: object) -> None:
            pass

        def get_config(self) -> SimpleNamespace:
            return config

    monkeypatch.setattr("app.services.strategy_service.StrategyService", FakeStrategyService)
    monkeypatch.setattr("app.runner.get_runner", lambda: runner)
    monkeypatch.setattr("app.services.llm_advisor_service.build_recent_analysis_context", lambda cfg: [])
    monkeypatch.setattr(main_module, "record_trade_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.api.llm_advisor._position_context",
        lambda symbol, price: {"side": "FLAT", "quantity": 0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0},
    )
    monkeypatch.setattr("app.api.llm_advisor._account_context", lambda *args: {})
    monkeypatch.setattr(
        "app.api.llm_advisor._interval_reference_quantity",
        lambda *args, **kwargs: 1.0,
    )
    monkeypatch.setattr(
        "app.services.llm_interaction_service.LLMInteractionService",
        lambda db: MagicMock(update_outcome=lambda *args, **kwargs: None),
    )

    class FakeAdvisor:
        def __init__(self, broker: object = None) -> None:
            pass

        def analyze(self, **kwargs: object) -> dict[str, object]:
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
        main_module,
        "IntervalApplicationService",
        lambda: MagicMock(apply_suggestion=lambda **kwargs: {"applied": True, "reason": "ok"}),
    )
    monkeypatch.setattr(main_module.settings, "llm_max_symbols_per_cycle", 1)
    monkeypatch.setattr(main_module.settings, "llm_max_analyses_per_hour", 10)

    await main_module._llm_analysis_tick()

    db = session_factory()
    try:
        skipped = db.query(LLMSymbolScheduleState).filter(LLMSymbolScheduleState.symbol == "MSFT.US").one()
        assert skipped.last_status == "SKIPPED"
        assert skipped.last_skip_reason == "cycle budget exhausted"
    finally:
        db.close()
