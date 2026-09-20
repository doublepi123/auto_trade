from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from contextlib import contextmanager
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import database, runner as runner_module
from app.core.broker import OrderResult, Position, Quote
from app.core.engine import EngineState, StrategyParams
from app.core.risk import DailyLossSnapshot
from app.models import Base, RuntimeState
from app.runner import AppRunner, _ReductionIntent, _ReduceOnlyPriceFloor
from app.services.trade_execution_service import FinalOrderQuoteCheckResult


database.init_db()


def _quote(last: float = 100.45, bid: float = 100.45, ask: float = 100.46, *, age: int = 0) -> Quote:
    return Quote("NVDA.US", last, bid, ask, (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat())


def _runner(monkeypatch: pytest.MonkeyPatch, *, minutes: int = 5) -> AppRunner:
    monkeypatch.setattr(runner_module, "is_closing_window", lambda *_: False)
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(
        symbol="NVDA.US", market="US", buy_low=99, sell_high=110,
        stop_loss_pct=1, max_holding_minutes=60,
    )
    runner.engine.state = EngineState.LONG
    runner._trade_svc.load_tracked_entries({
        "NVDA.US": (Decimal("5"), Decimal("500"), "LONG", datetime.now(timezone.utc) - timedelta(minutes=minutes)),
    })
    return runner


def _intent(runner: AppRunner, quote: Quote, *, realized: float = 0, limit: float = 5000):
    return runner._reduction_intent_for_quote_locked(
        quote, runner.engine, "US",
        daily_loss_snapshot=DailyLossSnapshot(realized, limit, date.today(), False, False),
    )


def _latch(runner: AppRunner) -> _ReductionIntent:
    intent = _ReductionIntent("SELL", "TIME_STOP", "holding limit reached", 100, datetime.now(timezone.utc))
    runner._reduction_intents["NVDA.US"] = intent
    return intent


class _FakeBroker:
    def __init__(self, quote: Quote) -> None:
        self.quote = quote
        self.submitted: list[tuple[str, str, Decimal, Decimal]] = []

    def get_positions(self) -> list[Position]:
        return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        assert symbols == ["NVDA.US"]
        return [self.quote]

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        self.submitted.append((symbol, side, quantity, price))
        return OrderResult("degraded-exit", symbol, side, quantity, price, "SUBMITTED")


class _FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        self.alerts.append((event_type, reason))
        return True

    def notify_order(self, *args: object, **kwargs: object) -> bool:
        return True


@contextmanager
def _execution_sandbox(runner: AppRunner, monkeypatch: pytest.MonkeyPatch, quote: Quote) -> Iterator[tuple[_FakeBroker, _FakeNotifier]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    broker, notifier = _FakeBroker(quote), _FakeNotifier()
    monkeypatch.setattr(runner_module, "SessionLocal", sessions)
    monkeypatch.setattr(runner, "broker", broker)
    monkeypatch.setattr(runner, "notifier", notifier)
    monkeypatch.setattr(runner, "refresh_opening_execution_registry", lambda: None)
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    monkeypatch.setattr(runner._trade_svc, "_record_order", lambda *a, **kw: None)
    monkeypatch.setattr(runner._trade_svc, "_update_order_status", lambda *a, **kw: None)
    monkeypatch.setattr(runner._trade_svc, "_record_order_skipped", lambda *a, **kw: None)
    try:
        yield broker, notifier
    finally:
        engine.dispose()


class TestReductionIntentTrust:
    def test_stale_quote_does_not_advance_profit_peak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        _intent(runner, _quote())
        _intent(runner, _quote(150, 150, 150.01, age=60))
        assert runner._position_peak_executable["NVDA.US"] == 100.45

    def test_inconsistent_bbo_does_not_advance_profit_peak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        _intent(runner, _quote())
        _intent(runner, _quote(100.45, 120, 121))
        assert runner._position_peak_executable["NVDA.US"] == 100.45

    @pytest.mark.parametrize("bid", [float("nan"), float("inf")])
    def test_nan_bid_does_not_latch_price_stop_off_last(self, monkeypatch: pytest.MonkeyPatch, bid: float) -> None:
        runner = _runner(monkeypatch)
        intent, _, _ = _intent(runner, _quote(98, bid, bid))
        assert intent is None
        assert runner._position_peak_executable == {}

    def test_stale_quote_does_not_newly_latch_price_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        intent, _, _ = _intent(runner, _quote(98, 98, 98.01, age=60))
        assert intent is None

    def test_zero_bid_quote_still_latches_time_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        intent, newly_latched, _ = _intent(runner, _quote(0, 0, 0))
        assert intent is not None and intent.cause == "TIME_STOP"
        assert newly_latched

    def test_realized_daily_loss_latches_without_usable_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        intent, _, _ = _intent(runner, _quote(0, 0, 0), realized=-5000)
        assert intent is not None and intent.cause == "DAILY_LOSS"

    def test_unrealized_only_breach_needs_trusted_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        stale, _, _ = _intent(runner, _quote(98, 98, 98.01, age=60), limit=5)
        assert stale is None
        trusted, _, _ = _intent(runner, _quote(98, 98, 98.01), limit=5)
        assert trusted is not None and trusted.cause == "DAILY_LOSS"

    def test_stale_quote_keeps_existing_latched_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        existing = _latch(runner)
        intent, newly_latched, should_clear = _intent(runner, _quote(age=60))
        assert intent is existing
        assert not newly_latched and not should_clear


class TestDegradedQuoteTrigger:
    def test_wide_spread_consistent_last_latches_price_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner._remember_quote(_quote(97.6, 97.5, 97.7))
        decision = runner._evaluate_quote_trigger(_quote(100.2, 97.5, 103))
        assert decision.reduction_intent is not None
        assert decision.reduction_intent.cause == "PRICE_STOP"
        assert decision.result is not None and decision.result.action == "SELL"
        assert decision.reduce_only and decision.allow_loss_exit
        assert not decision.early_return

    def test_zero_bbo_quote_latches_time_stop_on_primary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        decision = runner._evaluate_quote_trigger(_quote(0, 0, 0))
        assert decision.reduction_intent is not None
        assert decision.reduction_intent.cause == "TIME_STOP"
        # Step 3 preserves the latch but holds execution without price evidence.
        assert decision.early_return and decision.result is None
        assert decision.exit_hold_reason == "no trusted reference price within window"

    def test_stale_quote_keeps_latched_intent_alive_through_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        existing = _latch(runner)
        decision = runner._evaluate_quote_trigger(_quote(age=60))
        assert decision.reduction_intent is existing
        assert not decision.reduction_newly_latched

    def test_degraded_quote_never_evaluates_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner._trade_svc.load_tracked_entries({})
        runner.engine.state = EngineState.FLAT
        decision = runner._evaluate_quote_trigger(_quote(99, 50, 150))
        assert decision.result is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 0
        counters = runner.decision_funnel.snapshot()
        assert counters.quality_rejections == 1 and counters.evaluations == 0

    def test_degraded_exit_quote_is_not_a_trusted_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        decision = runner._evaluate_quote_trigger(_quote(100.2, 97.5, 103))
        assert decision.reduction_intent is not None
        assert runner._last_trusted_push_quote_at == 0
        assert runner._last_quote_at == 0
        assert runner.decision_funnel.snapshot().evaluations == 0

    def test_rth_only_session_guard_still_applies_to_degraded_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        monkeypatch.setattr(runner, "_get_trading_session_mode", lambda: "RTH_ONLY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda _: False)
        decision = runner._evaluate_quote_trigger(_quote(0, 0, 0))
        assert decision.reduction_intent is not None
        assert decision.result is None
        assert runner.decision_funnel.snapshot().skips_by_category["SESSION"] == 1

    def test_degraded_quote_does_not_record_bad_price_into_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner.engine.record_price(100.45)
        _latch(runner)
        decision = runner._evaluate_quote_trigger(_quote(0, 0, 0))
        assert decision.reduction_intent is not None
        assert runner.engine.last_price == 100.45


class TestDegradedExitPricing:
    def test_floor_protected_limit_is_bound_to_execution_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner._remember_quote(_quote(97.6, 97.5, 97.7))
        quote = _quote(100.2, 97.5, 103)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, _):
            # Isolate price binding from step 4's still-closed final quality gate.
            monkeypatch.setattr(runner._trade_svc, "_final_order_quote_check", lambda _b, _s, _a, p: FinalOrderQuoteCheckResult(executable_price=p, bid=p, ask=p))
            decision = runner._evaluate_quote_trigger(quote)
            assert decision.exit_limit_price == 97.5
            assert decision.exit_price_floor == pytest.approx(97.0125)
            assert decision.reduction_intent is not None
            assert runner._persist_reduction(decision.reduction_intent, "NVDA.US")
            # A later caller must not accidentally substitute a different quote.
            runner._execute_triggered_order(decision, _quote(100.2, 98, 103))
            assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), Decimal("97.50"))], runner._last_action_message

    def test_bid_below_floor_holds_intent_and_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner._remember_quote(_quote(100.4, 100.4, 100.41))
        quote = _quote(100.2, 97.5, 103)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, notifier):
            runner._on_quote(quote)
            assert broker.submitted == []
            assert runner._reduction_intents["NVDA.US"].cause == "PRICE_STOP"
            assert any("below floor" in reason for _, reason in notifier.alerts)
            with runner_module.SessionLocal() as db:
                persisted = db.scalar(select(RuntimeState).where(RuntimeState.symbol == "NVDA.US"))
                assert persisted is not None and persisted.reduction_cause == "PRICE_STOP"
            assert runner.engine.state == EngineState.LONG
            assert not runner._trigger_in_flight

    def test_expired_reference_holds_intent_without_fabricating_price(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        runner._recent_quotes.append({"symbol": "NVDA.US", "bid": 100.4, "ask": 100.41, "timestamp": _quote(age=301).timestamp, "trusted": True})
        runner._position_peak_executable["NVDA.US"] = 100.45
        decision = runner._evaluate_quote_trigger(_quote(0, 0, 0))
        assert decision.reduction_intent is not None and decision.reduction_intent.cause == "TIME_STOP"
        assert decision.exit_hold_reason == "no trusted reference price within window"
        assert decision.exit_limit_price is None and decision.result is None
        assert runner._position_peak_executable["NVDA.US"] == 100.45

    def test_healthy_quote_price_unchanged_by_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        quote = _quote(98.9, 98.8, 99)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, _):
            runner._on_quote(quote)
            assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), Decimal("98.80"))]

    def test_hold_is_throttled_and_never_clears_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        runner._remember_quote(_quote(100.4, 100.4, 100.41))
        quote = _quote(100.2, 97.5, 103)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, notifier):
            for _ in range(20):
                runner._on_quote(quote)
            assert len([reason for _, reason in notifier.alerts if "below floor" in reason]) == 1
            assert broker.submitted == []
            assert "NVDA.US" in runner._reduction_intents


def _floor(runner: AppRunner, *, age: int = 0, action: str = "SELL") -> None:
    source = datetime.now(timezone.utc) - timedelta(seconds=age)
    runner._reduce_only_price_floors["NVDA.US"] = _ReduceOnlyPriceFloor(
        action, 99.898, source, source + timedelta(seconds=300),
    )


class TestFinalQuoteCheck:
    @pytest.mark.parametrize("expired", [False, True])
    def test_reduce_only_without_registered_floor_uses_legacy_quote_gate(
        self, monkeypatch: pytest.MonkeyPatch, expired: bool,
    ) -> None:
        runner = _runner(monkeypatch)
        if expired:
            _floor(runner, age=301)
        quote = _quote(98.9, 98.8, 99)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, _):
            result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("98.9"))
            assert isinstance(result, FinalOrderQuoteCheckResult)
            assert result.price_floor is None
            status = runner._trade_svc.execute(
                "SELL", "NVDA.US", quote, runner.broker, runner.risk,
                runner.notifier, "USD", allow_loss_exit=True, reduce_only=True,
            )
            assert status is not None and status.status == "SUBMITTED"
            assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), Decimal("98.80"))]

            broker.quote = _quote(103, 100, 106)
            rejected = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100"))
            assert rejected == "fresh executable quote failed the final quality gate"
            broker.quote = _quote(100, 100, 100.01)
            deviation = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("101"))
            assert deviation == "submitted limit price deviates from fresh executable BBO by 1.00%"

    def test_reduce_only_accepts_wide_spread_when_bid_clears_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        _floor(runner)
        monkeypatch.setattr(runner, "broker", _FakeBroker(_quote(103, 100, 106)))
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100"))
        assert isinstance(result, FinalOrderQuoteCheckResult)
        assert result.price_floor == Decimal("99.898")

    @pytest.mark.parametrize(
        ("reference_bid", "fresh_bid", "expected_price"),
        [(100.4, 99.899, Decimal("99.90")), (100.392, 99.891, Decimal("99.90"))],
    )
    def test_reduce_only_never_normalizes_below_floor_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, reference_bid: float, fresh_bid: float, expected_price: Decimal,
    ) -> None:
        runner = _runner(monkeypatch, minutes=61)
        runner._remember_quote(_quote(reference_bid, reference_bid, reference_bid + 0.01))
        quote = _quote(103, fresh_bid, 106)
        with _execution_sandbox(runner, monkeypatch, quote) as (broker, _):
            runner._on_quote(quote)
            assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), expected_price)], runner._last_action_message
            assert broker.submitted[0][3] >= Decimal(str(runner._reduce_only_price_floors["NVDA.US"].price))

    def test_reduce_only_rejects_fresh_bid_below_floor_without_submitting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch, minutes=61)
        runner._remember_quote(_quote(100.4, 100.4, 100.41))
        with _execution_sandbox(runner, monkeypatch, _quote(99.85, 99.8, 99.9)) as (broker, _):
            runner._on_quote(_quote(103, 100, 106))
            assert broker.submitted == []
            assert "NVDA.US" in runner._reduction_intents
            assert "below floor" in runner._last_action_message

    @pytest.mark.parametrize("bid", [float("nan"), float("inf"), 0])
    def test_reduce_only_rejects_nan_or_zero_bid(self, monkeypatch: pytest.MonkeyPatch, bid: float) -> None:
        runner = _runner(monkeypatch)
        _floor(runner)
        broker = _FakeBroker(_quote(100, bid, 100.1))
        monkeypatch.setattr(runner, "broker", broker)
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100"))
        assert result == "fresh executable BBO price is unavailable"
        assert broker.submitted == []

    def test_entry_final_check_still_requires_all_four_predicates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        monkeypatch.setattr(runner, "broker", _FakeBroker(_quote(103, 100, 106)))
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "BUY", Decimal("106"))
        assert result == "fresh executable quote failed the final quality gate"

    def test_entry_final_check_keeps_deviation_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _runner(monkeypatch)
        monkeypatch.setattr(runner, "broker", _FakeBroker(_quote(99.95, 99.9, 100)))
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "BUY", Decimal("101"))
        assert result == "submitted limit price deviates from fresh executable BBO by 1.00%"

    @pytest.mark.parametrize("evidence", ["missing", "expired", "opposite_side"])
    def test_reduction_enforces_new_floor_after_missing_or_expired_evidence(self, monkeypatch: pytest.MonkeyPatch, evidence: str) -> None:
        runner = _runner(monkeypatch)
        if evidence != "missing":
            _floor(runner, age=301 if evidence == "expired" else 0, action="BUY_TO_COVER" if evidence == "opposite_side" else "SELL")
        if evidence != "opposite_side":
            # Missing/expired evidence may fall back; a newly armed floor still binds.
            _floor(runner)
        monkeypatch.setattr(runner, "broker", _FakeBroker(_quote(99.85, 99.8, 99.9)))
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100"))
        assert isinstance(result, str)
        assert "floor" in result
