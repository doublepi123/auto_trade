from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import main as main_module
from app.core.engine import StrategyParams


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
    def reset_trigger_price(self) -> Generator[None, None, None]:
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
        runner.recent_price_context.return_value = []
        runner.execute_llm_order_decision.return_value = {"status": "NO_ACTION", "order_id": None}
        return runner

    def _patch_tick_deps(self, monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace, runner: MagicMock) -> list[dict[str, object]]:
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
        monkeypatch.setattr("app.api.llm_advisor._interval_reference_quantity", lambda *a: 1.0)
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
                return SimpleNamespace(symbol=symbol, market=market, last_analysis_at=None, next_analysis_at=None, last_skip_reason="")

            def record_analysis(self, symbol: str, market: str, *, analyzed_at: datetime, next_analysis_at: datetime | None) -> None:
                return None

            def record_skip(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
                return None

            def record_failure(self, symbol: str, market: str, reason: str, *, next_analysis_at: datetime | None) -> None:
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
    async def test_tick_executes_secondary_symbol_order_action_with_budgeted_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert execute_calls[0]["symbol"] == "NVDA.US"
        assert execute_calls[0]["order_action"] == "BUY_NOW"

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
    monkeypatch.setattr("app.api.llm_advisor._interval_reference_quantity", lambda *args: 1.0)
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
