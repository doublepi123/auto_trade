# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Decision funnel — live-path stage counters for the zero-order diagnosis.

The funnel answers one question: at which stage does the trading pipeline
stop? These tests drive scripted quote sequences through the runner and
assert each stage increments exactly as the interpretation contract on
``DecisionFunnelTracker`` promises.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_decision_funnel_{os.getpid()}.db"
)

import pytest

from app import database
from app import runner as runner_module
from app.core.broker import Quote
from app.core.engine import StrategyParams
from app.models import DecisionFunnelSessionSummary
from app.runner import AppRunner
from app.schemas import DiagnosticsResponse
from app.services.decision_funnel_service import (
    DecisionFunnelTracker,
    persist_session_summary,
)
from app.services.trade_execution_service import OrderStatus


database.init_db()


def _fresh_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote(symbol: str, price: float) -> Quote:
    return Quote(symbol, price, price - 0.01, price + 0.01, _fresh_timestamp())


class _NoopNotifier:
    dedup_suppressed_total = 0
    dedup_window_seconds = 0.0

    def notify_order(self, *args: object) -> bool:
        return True

    def notify_risk_event(self, *args: object) -> bool:
        return True


def _runner() -> AppRunner:
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(
        symbol="NVDA.US",
        market="US",
        buy_low=100.0,
        sell_high=110.0,
    )
    runner._symbol_runtimes = {
        "NVDA.US": runner._build_symbol_runtime("NVDA.US", "US", primary=True)
    }
    runner.notifier = _NoopNotifier()
    return runner


class TestDecisionFunnelPipeline:
    @pytest.fixture(autouse=True)
    def _disable_entry_crossing_requirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            runner_module.settings,
            "live_entry_crossing_required",
            False,
        )

    def test_funnel_counts_each_stage_of_a_full_entry(self) -> None:
        # Given: a running runner whose FLAT engine watches NVDA.US 100/110,
        # and an execution seam that acks and persists like the real service.
        runner = _runner()
        submissions: list[str] = []

        def _fake_execute(*, action, symbol, quote, **_kwargs):
            submissions.append(action)
            runner._record_order(
                "FUNNEL-ORDER-1",
                symbol,
                action,
                1.0,
                float(quote.last_price),
                status="SUBMITTED",
            )
            return OrderStatus("FUNNEL-ORDER-1", "SUBMITTED")

        runner._trade_svc.execute = _fake_execute

        # When: a mid-range quote (no crossing) then a quote below buy_low.
        runner._on_quote(_quote("NVDA.US", 105.0))
        runner._on_quote(_quote("NVDA.US", 99.5))

        # Then: every funnel stage advanced exactly as far as the pipeline did.
        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.fresh_primary_quote == 2
        assert snapshot.evaluations == 2
        assert snapshot.threshold_crossings == 1
        assert snapshot.triggers == 1
        assert snapshot.sized_quantity_positive == 1
        assert snapshot.submit_attempts == 1
        assert snapshot.broker_acks == 1
        assert snapshot.persisted == 1
        assert snapshot.pre_submit_risk_check_invocations == 0
        assert submissions == ["BUY"]
        assert all(count == 0 for count in snapshot.skips_by_category.values())

        # And: the funnel is exposed through the existing diagnostics payload.
        payload = runner.diagnostics()
        assert payload["decision_funnel"]["triggers"] == 1
        parsed = DiagnosticsResponse.model_validate(payload)
        assert parsed.decision_funnel.persisted == 1
        assert parsed.decision_funnel.skips_by_category == {
            "FEE": 0,
            "REPRICING": 0,
            "COOLDOWN": 0,
            "RISK": 0,
            "PENDING": 0,
            "POSITION": 0,
            "SESSION": 0,
        }

    def test_risk_skip_increments_risk_category_but_not_triggers(self) -> None:
        # Given: a paused risk controller, so the entry is suppressed before
        # any trigger can fire.
        runner = _runner()
        runner.risk.pause("test pause")
        execute_calls: list[str] = []
        runner._trade_svc.execute = lambda **kwargs: execute_calls.append("called")

        # When: a quote crosses buy_low while risk rejects everything.
        runner._on_quote(_quote("NVDA.US", 99.5))

        # Then: the skip is attributed to RISK and no trigger is counted —
        # this discrimination is what the whole instrument depends on.
        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.fresh_primary_quote == 1
        assert snapshot.evaluations == 1
        assert snapshot.threshold_crossings == 1
        assert snapshot.skips_by_category["RISK"] == 1
        assert snapshot.triggers == 0
        assert snapshot.sized_quantity_positive == 0
        assert snapshot.submit_attempts == 0
        assert snapshot.broker_acks == 0
        assert snapshot.persisted == 0
        assert execute_calls == []


class TestDecisionFunnelSessionPersistence:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        try:
            db.query(DecisionFunnelSessionSummary).delete()
            db.commit()
        finally:
            db.close()

    def test_simulated_session_produces_exactly_one_summary_row(self) -> None:
        # Given: a tracker whose exchange-local day can be advanced.
        current_day = [date(2026, 8, 28)]
        tracker = DecisionFunnelTracker(trade_day_provider=lambda: current_day[0])

        # When: counters accumulate on day 1, then the day rolls over.
        tracker.record_fresh_primary_quote()
        tracker.record_evaluation()
        tracker.record_threshold_crossing()
        tracker.record_skip("RISK")
        current_day[0] = date(2026, 8, 29)
        tracker.record_evaluation()

        # Then: exactly one closed session is drained for day 1.
        closed = tracker.drain_closed_sessions()
        assert len(closed) == 1
        assert closed[0].session_date == "2026-08-28"
        assert closed[0].fresh_primary_quote == 1
        assert closed[0].evaluations == 1
        assert closed[0].threshold_crossings == 1
        assert closed[0].skips_by_category["RISK"] == 1
        assert tracker.drain_closed_sessions() == []

        # And: persisting it — even twice — yields exactly one durable row.
        db = database.SessionLocal()
        try:
            persist_session_summary(db, closed[0], symbol="TSLA.US", market="US")
            db.commit()
            persist_session_summary(db, closed[0], symbol="TSLA.US", market="US")
            db.commit()
            rows = (
                db.query(DecisionFunnelSessionSummary)
                .filter(
                    DecisionFunnelSessionSummary.session_date == date(2026, 8, 28)
                )
                .all()
            )
            assert len(rows) == 1
            assert rows[0].symbol == "TSLA.US"
            assert rows[0].market == "US"
            assert rows[0].fresh_primary_quote == 1
            assert rows[0].evaluations == 1
            assert rows[0].threshold_crossings == 1
            assert rows[0].pre_submit_risk_check_invocations == 0
            assert db.query(DecisionFunnelSessionSummary).count() == 1
        finally:
            db.close()
