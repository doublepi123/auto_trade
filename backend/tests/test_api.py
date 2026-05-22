import os
import time

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_api.db"


from fastapi.testclient import TestClient

from app.api import llm_advisor as llm_api
from app.api import strategy as strategy_api
from app.api import trade as trade_api
from app import database
from app.database import engine as db_engine, SessionLocal
from app.models import Base, CredentialConfig, LLMInteraction, StrategyConfig
from app.main import app


Base.metadata.create_all(bind=db_engine)
database._ensure_strategy_config_llm_columns(db_engine)
database._ensure_runtime_state_daily_pnl_date_column(db_engine)

client = TestClient(app)


def _clean_strategy() -> None:
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()


def _clean_credentials() -> None:
    db = SessionLocal()
    db.query(CredentialConfig).delete()
    db.commit()
    db.close()


def _clean_llm_interactions() -> None:
    db = SessionLocal()
    db.query(LLMInteraction).delete()
    db.commit()
    db.close()


class TestAPI:
    def test_health(self) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_get_strategy_default(self) -> None:
        _clean_strategy()
        resp = client.get("/api/strategy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == ""
        assert data["llm_interval_minutes"] == 2
        assert data["auto_resume_minutes"] == 3
        assert "sct_key" not in data

    def test_get_credentials_default(self) -> None:
        _clean_credentials()
        resp = client.get("/api/credentials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""

    def test_update_strategy_valid(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 5000.0,
            "max_consecutive_losses": 3,
            "min_profit_amount": 8.5,
            "auto_resume_minutes": 4,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["buy_low"] == 100.0
        assert data["sell_high"] == 200.0
        assert data["min_profit_amount"] == 8.5
        assert data["auto_resume_minutes"] == 4
        assert data["llm_interval_minutes"] == 2
        assert "sct_key" not in data

    def test_update_strategy_allows_llm_interval_minutes(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "llm_interval_minutes": 1,
        })

        assert resp.status_code == 200
        assert resp.json()["llm_interval_minutes"] == 1

    def test_update_strategy_does_not_wait_for_running_runner_reload(self, monkeypatch) -> None:
        _clean_strategy()

        class SlowRunner:
            is_running = True

            def reload_strategy(self) -> None:
                time.sleep(0.2)

        monkeypatch.setattr(strategy_api, "get_runner", lambda: SlowRunner())

        started_at = time.perf_counter()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        elapsed = time.perf_counter() - started_at

        assert resp.status_code == 200
        assert elapsed < 0.15

    def test_update_strategy_rejects_invalid_llm_interval_minutes(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "llm_interval_minutes": 0,
        })

        assert resp.status_code == 422

    def test_update_strategy_rejects_negative_min_profit_amount(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "min_profit_amount": -1,
        })

        assert resp.status_code == 422

    def test_update_strategy_rejects_negative_auto_resume_minutes(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "auto_resume_minutes": -1,
        })

        assert resp.status_code == 422

    def test_update_strategy_invalid_market(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "CN",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_sell_lt_buy(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 200.0,
            "sell_high": 100.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_rejects_blank_symbol(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "   ",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_rejects_partial_threshold_inversion(self) -> None:
        _clean_strategy()
        initial = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert initial.status_code == 200

        resp = client.put("/api/strategy", json={"buy_low": 250.0})

        assert resp.status_code == 422

    def test_get_status(self) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "engine_state" in data

    def test_pause_trading(self) -> None:
        resp = client.post("/api/control/pause", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading paused"

    def test_resume_trading(self) -> None:
        resp = client.post("/api/control/resume")
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading resumed"

    def test_kill_switch(self) -> None:
        resp = client.post("/api/control/kill-switch", json={"reason": "testing"})
        assert resp.status_code == 200
        assert "kill switch activated" in resp.json()["message"]

    def test_start_runner(self) -> None:
        client.post("/api/control/disable-kill-switch")
        client.post("/api/control/resume")
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner started"

    def test_stop_runner(self) -> None:
        resp = client.post("/api/control/stop", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner stopped"

    def test_get_orders_empty(self) -> None:
        resp = client.get("/api/orders")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_lifespan_initializes_db(self) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_get_account(self) -> None:
        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_assets" in data
        assert "cash_balances" in data
        assert "positions" in data

    def test_app_title(self) -> None:
        assert app.title == "Auto Trade"

    def test_disable_kill_switch(self) -> None:
        resp = client.post("/api/control/kill-switch", json={"reason": "testing"})
        assert resp.status_code == 200
        resp = client.post("/api/control/disable-kill-switch")
        assert resp.status_code == 200
        assert "kill switch disabled" in resp.json()["message"]

    def test_put_credentials(self) -> None:
        _clean_credentials()
        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "test_key",
            "longbridge_app_secret": "test_secret",
            "longbridge_access_token": "test_token",
            "sct_key": "",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""
        assert data["has_longbridge_app_key"] is True
        assert data["has_longbridge_app_secret"] is True
        assert data["has_longbridge_access_token"] is True
        assert data["has_sct_key"] is False
        assert "updated_at" in data

    def test_orders_with_limit(self) -> None:
        resp = client.get("/api/orders?limit=10")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_account_endpoint_returns_default_structure(self) -> None:
        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["total_assets"], (int, float))
        assert isinstance(data["cash_balances"], list)
        assert isinstance(data["positions"], list)

    def test_account_endpoint_marks_unavailable_on_broker_error(self, monkeypatch) -> None:
        class FailingBroker:
            def get_account(self):
                raise RuntimeError("account unavailable")

            def get_positions(self):
                raise RuntimeError("positions unavailable")

        class Runner:
            broker = FailingBroker()

        monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

        resp = client.get("/api/account")

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["error"] == "Account data unavailable"

    def test_status_has_all_fields(self) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        expected_fields = [
            "engine_state", "paused", "kill_switch",
            "daily_pnl", "consecutive_losses",
            "last_price", "last_trigger_price", "last_trigger_at",
            "runner_running", "last_action_message",
        ]
        for field in expected_fields:
            assert field in data

    def test_status_reports_live_runner_state(self, monkeypatch) -> None:
        class RunningRunner:
            is_running = True

        monkeypatch.setattr(strategy_api, "get_runner", lambda: RunningRunner())

        resp = client.get("/api/status")

        assert resp.status_code == 200
        assert resp.json()["runner_running"] is True

    def test_llm_analyze_returns_structured_failure(self, monkeypatch) -> None:
        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert setup.status_code == 200

        class MissingKeyAdvisor:
            def analyze(self, **_kwargs):
                return {
                    "success": False,
                    "error": "LLM analysis failed: DEEPSEEK_API_KEY is not configured",
                }

        monkeypatch.setattr(llm_api, "LLMAdvisorService", MissingKeyAdvisor)

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["applied"] is False
        assert data["reason"] == "LLM analysis failed: DEEPSEEK_API_KEY is not configured"
        assert data["suggested_buy_low"] is None
        assert data["order_action"] is None

    def test_llm_analyze_applies_suggested_interval(self, monkeypatch) -> None:
        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert setup.status_code == 200

        class SuccessfulAdvisor:
            def analyze(self, **_kwargs):
                return {
                    "success": True,
                    "suggested_buy_low": 195.0,
                    "suggested_sell_high": 205.0,
                    "confidence_score": 0.85,
                    "analysis": "test analysis",
                    "next_analysis_at": "2026-05-19T21:45:55+00:00",
                }

        class Runner:
            class Engine:
                last_price = 200.0

                class State:
                    value = "long"

                state = State()

            engine = Engine()

        monkeypatch.setattr(llm_api, "LLMAdvisorService", SuccessfulAdvisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["applied"] is True
        assert data["suggested_buy_low"] == 195.0
        assert data["suggested_sell_high"] == 205.0

        strategy = client.get("/api/strategy").json()
        assert strategy["buy_low"] == 195.0
        assert strategy["sell_high"] == 205.0

    def test_llm_analyze_passes_account_position_and_recent_price_context(self, monkeypatch) -> None:
        from decimal import Decimal

        from app.core.broker import Position

        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 218.0,
            "sell_high": 225.0,
            "short_selling": True,
            "min_profit_amount": 12.5,
        })
        assert setup.status_code == 200

        captured = {}

        class Advisor:
            def analyze(self, **kwargs):
                captured.update(kwargs)
                return {
                    "success": True,
                    "interaction_id": 101,
                    "suggested_buy_low": 219.0,
                    "suggested_sell_high": 224.0,
                    "confidence_score": 0.82,
                    "analysis": "context checked",
                    "next_analysis_at": "2026-05-22T10:03:00+00:00",
                    "order_action": "NONE",
                }

        class Broker:
            def get_positions(self):
                return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("220"))]

            def get_cash(self, currency=None):
                assert currency == "USD"
                return Decimal("12345.67")

            def estimate_margin_max_quantity(self, symbol, side, price, currency=None):
                assert symbol == "NVDA.US"
                assert price == Decimal("221.5")
                assert currency == "USD"
                return Decimal("42") if side == "BUY" else Decimal("8")

        class Runner:
            class Engine:
                last_price = 221.5

                class State:
                    value = "flat"

                state = State()

            engine = Engine()
            broker = Broker()

            def recent_price_context(self):
                return [{"last_price": 221.5, "bid": 221.4, "ask": 221.6, "observed_at": "2026-05-22T10:00:00Z"}]

            def execute_llm_order_decision(self, _decision):
                raise AssertionError("NONE action must not execute an order")

        monkeypatch.setattr(llm_api, "LLMAdvisorService", Advisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        assert captured["position_quantity"] == 5.0
        assert captured["position_avg_price"] == 220.0
        assert captured["min_profit_amount"] == 12.5
        assert captured["recent_prices"][0]["last_price"] == 221.5
        assert captured["account_context"]["cash_currency"] == "USD"
        assert captured["account_context"]["available_cash"] == 12345.67
        assert captured["account_context"]["buying_power"] == 42 * 221.5
        assert captured["account_context"]["max_short_quantity"] == 8.0

    def test_llm_analyze_executes_immediate_order_action(self, monkeypatch) -> None:
        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 218.0,
            "sell_high": 225.0,
        })
        assert setup.status_code == 200

        class Advisor:
            def analyze(self, **_kwargs):
                return {
                    "success": True,
                    "interaction_id": 102,
                    "suggested_buy_low": 219.0,
                    "suggested_sell_high": 224.0,
                    "confidence_score": 0.82,
                    "analysis": "buy now",
                    "next_analysis_at": "2026-05-22T10:03:00+00:00",
                    "order_action": "BUY_NOW",
                    "order_price": 221.5,
                    "order_reason": "strong signal",
                }

        class Runner:
            class Engine:
                last_price = 221.5

                class State:
                    value = "flat"

                state = State()

            engine = Engine()
            broker = object()

            def recent_price_context(self):
                return []

            def execute_llm_order_decision(self, decision):
                assert decision["order_action"] == "BUY_NOW"
                assert decision["order_price"] == 221.5
                return {"executed": True, "status": "FILLED", "order_id": "order-llm-1"}

        monkeypatch.setattr(llm_api, "LLMAdvisorService", Advisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["order_action"] == "BUY_NOW"
        assert data["order_status"] == "FILLED"
        assert data["order_id"] == "order-llm-1"

    def test_llm_interaction_history_endpoint_returns_recent_records(self) -> None:
        from datetime import datetime, timezone

        _clean_llm_interactions()
        db = SessionLocal()
        try:
            db.add(LLMInteraction(
                interaction_type="analyze",
                symbol="NVDA.US",
                market="US",
                prompt="prompt",
                raw_response='{"analysis":"ok"}',
                parsed_response='{"analysis":"ok"}',
                context_snapshot='{"current_price":221.5}',
                success=True,
                error="",
                order_action="NONE",
                created_at=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
            ))
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/strategy/llm-interval/interactions")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "NVDA.US"
        assert data[0]["success"] is True
        assert data[0]["order_action"] == "NONE"

    def test_strategy_partial_update(self) -> None:
        _clean_strategy()
        full = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 5000.0,
            "max_consecutive_losses": 3,
        })
        assert full.status_code == 200
        resp = client.put("/api/strategy", json={"symbol": "TSLA.US"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "TSLA.US"
        assert data["market"] == "US"
        assert data["buy_low"] == 100.0
        assert data["sell_high"] == 200.0
        assert data["short_selling"] is False
        assert data["max_daily_loss"] == 5000.0
        assert data["max_consecutive_losses"] == 3

    def test_credentials_response_hides_values(self) -> None:
        _clean_credentials()
        client.put("/api/credentials", json={
            "longbridge_app_key": "secret_key",
            "longbridge_app_secret": "secret_secret",
            "longbridge_access_token": "secret_token",
            "sct_key": "secret_sct",
        })
        resp = client.get("/api/credentials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""
        assert data["longbridge_app_secret"] == ""
        assert data["longbridge_access_token"] == ""
        assert data["sct_key"] == ""
        assert data["has_longbridge_app_key"] is True
        assert data["has_longbridge_app_secret"] is True
        assert data["has_longbridge_access_token"] is True
        assert data["has_sct_key"] is True
