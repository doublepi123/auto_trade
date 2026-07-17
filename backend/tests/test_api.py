import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from freezegun import freeze_time
import pytest

from app.api import credentials as credentials_api
from app.api import llm_advisor as llm_api
from app.api import review as review_api
from app.api import strategy as strategy_api
from app.api import trade as trade_api
from app import database
from app.core.market_calendar import trade_day_for
from app.database import SessionLocal
from app.models import AuditLog, CredentialConfig, LLMInteraction, LLMSymbolScheduleState, OrderRecord, RiskEvent, RuntimeState, RuntimeStateSnapshot, StrategyConfig, TradeEvent
from app.main import app
from app.services.strategy_service import StrategyService


database.init_db()
client = TestClient(app)


def teardown_module() -> None:
    runner = trade_api.get_runner()
    if runner._thread is not None and runner._thread.is_alive():
        runner.stop()
    else:
        runner._running = False


def _clean_strategy() -> None:
    runner = trade_api.get_runner()
    runner._running = True
    runner._trigger_in_flight = False
    runner._reduction_intents.clear()
    runner._post_fill_expectations.clear()
    for order_id in runner._trade_svc.pending_order_ids():
        runner._trade_svc._clear_pending_order(order_id)
    runner._unresolved_live_order_ids.clear()
    runner._unrepresentable_live_order_issues.clear()
    runner._last_order_sync_succeeded = False
    runner._trade_svc.load_tracked_entries({})
    runner.risk.disable_kill_switch()
    runner.risk.resume()
    runner.engine.sync_state(False, False)
    runner.broker.get_positions = lambda: []
    with SessionLocal() as db:
        db.query(StrategyConfig).delete()
        db.commit()


def _clean_credentials() -> None:
    with SessionLocal() as db:
        db.query(CredentialConfig).delete()
        db.commit()


def _clean_llm_interactions() -> None:
    with SessionLocal() as db:
        db.query(LLMInteraction).delete()
        db.commit()


def _clean_llm_symbol_schedule_state() -> None:
    with SessionLocal() as db:
        db.query(LLMSymbolScheduleState).delete()
        db.commit()


def _clean_orders() -> None:
    with SessionLocal() as db:
        db.query(OrderRecord).delete()
        db.commit()


def _clean_status_history() -> None:
    with SessionLocal() as db:
        db.query(RuntimeStateSnapshot).delete()
        db.query(OrderRecord).delete()
        db.commit()


def _clean_runtime_state() -> None:
    with SessionLocal() as db:
        db.query(RuntimeState).delete()
        db.commit()


def _clean_trade_events() -> None:
    with SessionLocal() as db:
        db.query(TradeEvent).delete()
        db.commit()


def _clean_audit_logs() -> None:
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()


def _clean_risk_events() -> None:
    with SessionLocal() as db:
        db.query(RiskEvent).delete()
        db.commit()


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

    def test_update_strategy_waits_for_runtime_reload_confirmation(self, monkeypatch) -> None:
        _clean_strategy()

        class SlowRunner:
            is_running = True

            def assert_primary_switch_safe(self, _symbol: str, _market: str) -> None:
                return None

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
        assert elapsed >= 0.2

    def test_update_strategy_rolls_back_when_runtime_reload_fails(self, monkeypatch) -> None:
        _clean_strategy()
        with SessionLocal() as db:
            db.add(
                StrategyConfig(
                    symbol="AAPL.US",
                    market="US",
                    buy_low=100.0,
                    sell_high=110.0,
                )
            )
            db.commit()

        pauses: list[tuple[str, bool]] = []

        class Risk:
            def pause(self, reason: str, *, auto_resumable: bool) -> None:
                pauses.append((reason, auto_resumable))

        class FlakyRunner:
            risk = Risk()

            def __init__(self) -> None:
                self.reload_calls = 0

            def reload_strategy(self) -> None:
                self.reload_calls += 1
                if self.reload_calls == 1:
                    raise RuntimeError("reload failed")

            def assert_primary_switch_safe(self, _symbol: str, _market: str) -> None:
                return None

        runner = FlakyRunner()
        monkeypatch.setattr(strategy_api, "get_runner", lambda: runner)

        response = client.put(
            "/api/strategy",
            json={"buy_low": 101.0, "sell_high": 111.0},
        )

        assert response.status_code == 503
        assert runner.reload_calls == 2
        assert pauses == [("strategy runtime reload failed", False)]
        with SessionLocal() as db:
            config = db.query(StrategyConfig).first()
            assert config is not None
            assert config.buy_low == 100.0
            assert config.sell_high == 110.0

    def test_update_strategy_rejects_primary_switch_with_exposure(self, monkeypatch) -> None:
        _clean_strategy()
        with SessionLocal() as db:
            db.add(
                StrategyConfig(
                    symbol="AAPL.US",
                    market="US",
                    buy_low=100.0,
                    sell_high=110.0,
                )
            )
            db.commit()

        class ExposedRunner:
            def assert_primary_switch_safe(self, _symbol: str, _market: str) -> None:
                raise strategy_api.PrimarySwitchBlockedError("position exists")

        monkeypatch.setattr(strategy_api, "get_runner", lambda: ExposedRunner())

        response = client.put(
            "/api/strategy",
            json={"symbol": "MSFT.US", "market": "US"},
        )

        assert response.status_code == 409
        with SessionLocal() as db:
            config = db.query(StrategyConfig).first()
            assert config is not None
            assert config.symbol == "AAPL.US"

    def test_update_strategy_persists_margin_safety_factor(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "margin_safety_factor": 0.75,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["margin_safety_factor"] == 0.75

        # verify read-back
        resp2 = client.get("/api/strategy")
        assert resp2.status_code == 200
        assert resp2.json()["margin_safety_factor"] == 0.75

    def test_update_strategy_persists_p0_live_safety_fields(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "allow_position_addons": False,
            "max_position_quantity": 25,
            "max_position_notional": 5000,
            "max_risk_per_trade": 100,
            "stop_loss_pct": 0.8,
            "max_holding_minutes": 45,
            "entry_cutoff_minutes_before_close": 45,
            "flatten_minutes_before_close": 15,
            "llm_order_execution_enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["allow_position_addons"] is False
        assert data["max_position_quantity"] == 25
        assert data["max_position_notional"] == 5000
        assert data["max_risk_per_trade"] == 100
        assert data["stop_loss_pct"] == 0.8
        assert data["max_holding_minutes"] == 45
        assert data["entry_cutoff_minutes_before_close"] == 45
        assert data["flatten_minutes_before_close"] == 15
        assert data["llm_order_execution_enabled"] is False

    def test_update_strategy_rejects_short_entries_and_invalid_safety_window(self) -> None:
        _clean_strategy()
        short = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": True,
        })
        assert short.status_code == 422

        invalid_window = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "entry_cutoff_minutes_before_close": 15,
            "flatten_minutes_before_close": 30,
        })
        assert invalid_window.status_code == 422

    def test_update_strategy_persists_report_schedule_fields(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "report_schedule_enabled": True,
            "report_schedule_interval_hours": 6,
            "report_schedule_symbol": "MSFT.US",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["report_schedule_enabled"] is True
        assert data["report_schedule_interval_hours"] == 6
        assert data["report_schedule_symbol"] == "MSFT.US"

        resp2 = client.get("/api/strategy")
        assert resp2.status_code == 200
        assert resp2.json()["report_schedule_enabled"] is True
        assert resp2.json()["report_schedule_interval_hours"] == 6
        assert resp2.json()["report_schedule_symbol"] == "MSFT.US"

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

    def test_get_status_recomputes_daily_pnl_from_filled_orders(self) -> None:
        _clean_orders()
        _clean_runtime_state()
        _clean_strategy()
        assert client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        }).status_code == 200

        first_fill = datetime(2026, 6, 4, 17, 0, tzinfo=timezone.utc)
        second_fill = datetime(2026, 6, 4, 17, 5, tzinfo=timezone.utc)
        trade_day = trade_day_for("US", first_fill)
        db = SessionLocal()
        StrategyService(db).update_runtime_state(daily_pnl=999.0, daily_pnl_date=trade_day, consecutive_losses=3)
        db.add_all([
            OrderRecord(
                broker_order_id="status-buy",
                symbol="NVDA.US",
                side="BUY",
                quantity=2,
                price=100,
                executed_quantity=2,
                executed_price=100,
                status="FILLED",
                created_at=first_fill,
                filled_at=first_fill,
            ),
            OrderRecord(
                broker_order_id="status-sell",
                symbol="NVDA.US",
                side="SELL",
                quantity=2,
                price=103,
                executed_quantity=2,
                executed_price=103,
                status="FILLED",
                created_at=second_fill,
                filled_at=second_fill,
            ),
        ])
        db.commit()
        db.close()

        with freeze_time("2026-06-04 18:00:00", tz_offset=0):
            resp = client.get("/api/status")

        assert resp.status_code == 200
        assert resp.json()["daily_pnl"] == pytest.approx(5.797)
        assert resp.json()["consecutive_losses"] == 0

    def test_status_history_returns_points_and_trade_markers(self) -> None:
        _clean_status_history()
        db = SessionLocal()
        db.add(RuntimeStateSnapshot(
            engine_state="flat",
            last_price=220.1,
            daily_pnl=0.0,
            consecutive_losses=0,
            paused=False,
            kill_switch=False,
            created_at=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
        ))
        db.add(RuntimeStateSnapshot(
            engine_state="long",
            last_price=221.2,
            daily_pnl=12.5,
            consecutive_losses=0,
            paused=False,
            kill_switch=False,
            created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
        ))
        db.add(OrderRecord(
            broker_order_id="filled-1",
            symbol="NVDA.US",
            side="BUY",
            quantity=3,
            price=220.5,
            executed_quantity=3,
            executed_price=220.6,
            status="FILLED",
            created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            filled_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
        ))
        db.commit()
        db.close()

        resp = client.get("/api/status/history?from=2026-05-22T09:59:00Z&to=2026-05-22T10:02:00Z&limit=20")

        assert resp.status_code == 200
        data = resp.json()
        assert [point["last_price"] for point in data["points"]] == [220.1, 221.2]
        assert data["markers"][0]["broker_order_id"] == "filled-1"
        assert data["markers"][0]["side"] == "BUY"
        assert data["markers"][0]["price"] == 220.6


    def test_status_history_filters_points_and_markers_by_symbol(self) -> None:
        _clean_status_history()
        db = SessionLocal()
        db.add(RuntimeStateSnapshot(
            symbol="NVDA.US",
            engine_state="flat",
            last_price=220.1,
            daily_pnl=0.0,
            consecutive_losses=0,
            paused=False,
            kill_switch=False,
            created_at=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
        ))
        db.add(RuntimeStateSnapshot(
            symbol="AAPL.US",
            engine_state="long",
            last_price=199.2,
            daily_pnl=8.5,
            consecutive_losses=0,
            paused=False,
            kill_switch=False,
            created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
        ))
        db.add(OrderRecord(
            broker_order_id="filled-aapl",
            symbol="AAPL.US",
            side="BUY",
            quantity=3,
            price=199.0,
            executed_quantity=3,
            executed_price=199.1,
            status="FILLED",
            created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            filled_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
        ))
        db.add(OrderRecord(
            broker_order_id="filled-nvda",
            symbol="NVDA.US",
            side="BUY",
            quantity=2,
            price=220.0,
            executed_quantity=2,
            executed_price=220.2,
            status="FILLED",
            created_at=datetime(2026, 5, 22, 10, 2, tzinfo=timezone.utc),
            filled_at=datetime(2026, 5, 22, 10, 2, tzinfo=timezone.utc),
        ))
        db.commit()
        db.close()

        resp = client.get("/api/status/history?symbol=AAPL.US&from=2026-05-22T09:59:00Z&to=2026-05-22T10:03:00Z&limit=20")

        assert resp.status_code == 200
        data = resp.json()
        assert [point["symbol"] for point in data["points"]] == ["AAPL.US"]
        assert [point["last_price"] for point in data["points"]] == [199.2]
        assert [marker["symbol"] for marker in data["markers"]] == ["AAPL.US"]
        assert data["markers"][0]["broker_order_id"] == "filled-aapl"
    def test_pause_trading(self) -> None:
        resp = client.post("/api/control/pause", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading paused"

    def test_resume_trading(self, monkeypatch) -> None:
        runner = trade_api.get_runner()
        for order_id in runner._trade_svc.pending_order_ids():
            runner._trade_svc._clear_pending_order(order_id)
        monkeypatch.setattr(runner.broker, "get_today_orders", lambda: [])
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        resp = client.post("/api/control/resume")
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading resumed"

    def test_resume_persistence_failure_keeps_runtime_paused(
        self,
        monkeypatch,
    ) -> None:
        from app.runner import AppRunner

        runner = AppRunner()
        pause_reason = "ORDER_EXECUTION_BLOCKED: operator review"
        runner.risk.pause(pause_reason)
        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            lambda: (True, ""),
        )
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        def fail_persistence(*_args: object) -> None:
            raise RuntimeError("runtime state write failed")

        monkeypatch.setattr(
            runner._state_svc,
            "stage",
            fail_persistence,
        )

        resp = client.post("/api/control/resume")

        assert resp.status_code == 500
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == pause_reason
        assert runner.risk.check().approved is False

    @pytest.mark.parametrize(
        ("control", "endpoint"),
        [
            ("resume", "/api/control/resume"),
            ("disable_kill", "/api/control/disable-kill-switch"),
        ],
    )
    def test_control_post_commit_error_restores_durable_safe_state(
        self,
        monkeypatch,
        control: str,
        endpoint: str,
    ) -> None:
        from app.core.engine import StrategyParams
        from app.runner import AppRunner

        symbol = f"POST-COMMIT-{control.upper()}.US"
        pause_reason = "ORDER_EXECUTION_BLOCKED: operator review"
        with SessionLocal() as db:
            db.query(RuntimeStateSnapshot).filter(
                RuntimeStateSnapshot.symbol == symbol
            ).delete(synchronize_session=False)
            db.query(RuntimeState).filter(
                RuntimeState.symbol == symbol
            ).delete(synchronize_session=False)
            db.commit()

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner.risk.pause(pause_reason)
        if control == "resume":
            monkeypatch.setattr(
                runner,
                "verify_operational_resume",
                lambda: (True, ""),
            )
        else:
            runner.risk.enable_kill_switch("operator emergency stop")
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        request_db = SessionLocal()
        real_commit = request_db.commit
        commit_calls = 0

        def commit_then_report_error() -> None:
            nonlocal commit_calls
            real_commit()
            commit_calls += 1
            if commit_calls == 1:
                raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(request_db, "commit", commit_then_report_error)

        def override_db():
            try:
                yield request_db
            finally:
                request_db.close()

        app.dependency_overrides[trade_api.get_db] = override_db
        try:
            resp = client.post(endpoint)

            assert resp.status_code == 500
            assert commit_calls == 2
            assert runner.risk.paused is True
            assert runner.risk.pause_reason == pause_reason
            assert runner.risk.kill_switch is (control == "disable_kill")
            assert runner.risk.check().approved is False
            with SessionLocal() as db:
                state = db.query(RuntimeState).filter(
                    RuntimeState.symbol == symbol
                ).one()
                assert state.paused is True
                assert state.pause_reason == pause_reason
                assert state.kill_switch is (control == "disable_kill")
        finally:
            app.dependency_overrides.pop(trade_api.get_db, None)
            with SessionLocal() as db:
                db.query(RuntimeStateSnapshot).filter(
                    RuntimeStateSnapshot.symbol == symbol
                ).delete(synchronize_session=False)
                db.query(RuntimeState).filter(
                    RuntimeState.symbol == symbol
                ).delete(synchronize_session=False)
                db.commit()

    def test_manual_resume_cannot_bypass_unknown_live_order(self, monkeypatch) -> None:
        from decimal import Decimal

        from app.core.broker import BrokerOrder
        from app.core.engine import StrategyParams
        from app.runner import AppRunner

        _clean_orders()
        _clean_trade_events()
        global_runner = trade_api.get_runner()
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.risk.pause("manual review")

        class Broker:
            def get_today_orders(self):
                return [
                    BrokerOrder(
                        broker_order_id="unknown-nvda-live-1",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("1"),
                        price=Decimal("220"),
                        executed_quantity=Decimal("0"),
                        executed_price=Decimal("0"),
                        status="SUBMITTED",
                        created_at=datetime.now(timezone.utc),
                        filled_at=None,
                    )
                ]

            def get_positions(self):
                return []

        monkeypatch.setattr(runner, "broker", Broker())
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        try:
            resp = client.post("/api/control/resume")

            assert resp.status_code == 409
            assert "unknown-nvda-live-1" in resp.json()["detail"]
            assert runner.risk.paused is True
            assert runner.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
        finally:
            _clean_orders()
            runner._trade_svc._clear_pending_order("unknown-nvda-live-1")
            global_runner._trade_svc._clear_pending_order("unknown-nvda-live-1")

    def test_manual_pause_and_kill_switch_preserve_operational_latch(self, monkeypatch) -> None:
        from app.runner import AppRunner

        runner = AppRunner()
        operational_reason = (
            "ORDER_RECONCILIATION_UNCERTAIN: live_orders=AAPL.US=[latched-1]"
        )
        runner.risk.pause(operational_reason, auto_resumable=False)
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(
            runner,
            "notifier",
            SimpleNamespace(notify_risk_event=lambda *_args, **_kwargs: None),
        )
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        pause_resp = client.post("/api/control/pause", json={"reason": "operator pause"})
        kill_resp = client.post("/api/control/kill-switch", json={"reason": "panic"})

        assert pause_resp.status_code == 200
        assert runner.risk.protective_exit_permitted is False
        assert kill_resp.status_code == 200
        assert runner.risk.pause_reason == operational_reason
        assert runner.risk.kill_switch is True

    def test_protective_exit_control_keeps_operational_pause(self, monkeypatch) -> None:
        from app.runner import AppRunner

        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            lambda **_kwargs: (True, ""),
        )
        monkeypatch.setattr(
            runner,
            "_protective_exit_runtime_health",
            lambda: (True, ""),
        )
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        enabled = client.post("/api/control/protective-exit/enable")

        assert enabled.status_code == 200
        assert runner.risk.paused is True
        assert runner.risk.protective_exit_permitted is True

        disabled = client.post("/api/control/protective-exit/disable")

        assert disabled.status_code == 200
        assert runner.risk.paused is True
        assert runner.risk.protective_exit_permitted is False

        assert client.post("/api/control/protective-exit/enable").status_code == 200
        stopped = client.post("/api/control/stop", json={"reason": "operator stop"})
        assert stopped.status_code == 200
        assert runner.risk.protective_exit_permitted is False

    def test_kill_switch(self) -> None:
        resp = client.post("/api/control/kill-switch", json={"reason": "testing"})
        assert resp.status_code == 200
        assert "kill switch activated" in resp.json()["message"]

    def test_start_runner(self, monkeypatch) -> None:
        _clean_credentials()
        runner = trade_api.get_runner()
        monkeypatch.setattr(runner.broker, "subscribe_quotes_batch", lambda symbols, callback: None)
        monkeypatch.setattr(runner.broker, "subscribe_quotes", lambda symbol, callback: None)
        monkeypatch.setattr(runner.broker, "unsubscribe_quotes", lambda: None)
        client.post("/api/control/stop", json={"reason": "test reset"})
        client.post("/api/control/disable-kill-switch")
        client.post("/api/control/resume")
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner started"
        client.post("/api/control/stop", json={"reason": "test cleanup"})

    def test_stop_runner(self) -> None:
        resp = client.post("/api/control/stop", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner stopped"

    def test_get_orders_empty(self) -> None:
        resp = client.get("/api/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "today"
        assert isinstance(data["items"], list)

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

    def test_disable_kill_switch_persistence_failure_keeps_switch_enabled(
        self,
        monkeypatch,
    ) -> None:
        from app.runner import AppRunner

        runner = AppRunner()
        runner.risk.pause("operator emergency stop")
        runner.risk.enable_kill_switch("operator emergency stop")
        monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

        def fail_persistence(*_args: object) -> None:
            raise RuntimeError("runtime state write failed")

        monkeypatch.setattr(
            runner._state_svc,
            "stage",
            fail_persistence,
        )

        resp = client.post("/api/control/disable-kill-switch")

        assert resp.status_code == 500
        assert runner.risk.kill_switch is True
        assert runner.risk.check().approved is False

    def test_put_credentials(self, monkeypatch) -> None:
        monkeypatch.setattr(
            credentials_api,
            "get_runner",
            lambda: SimpleNamespace(reload_credentials=lambda **_kwargs: None),
        )
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
        assert isinstance(resp.json()["items"], list)

    def test_orders_default_returns_local_today_orders_with_pagination(self) -> None:
        _clean_orders()
        db = SessionLocal()
        # `_is_today` treats naive datetimes as UTC then compares against the *local* date.
        # Anchor both orders to local noon today (converted to UTC for storage, matching the
        # production _utcnow default). This keeps the test deterministic regardless of the
        # machine timezone or time of day — a naive "now - 30min" near local midnight would
        # otherwise straddle the date boundary and drop one order from "today".
        with freeze_time("2026-06-04 10:00:00", tz_offset=0):
            local_noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        now_local = local_noon.astimezone(timezone.utc)
        db.add_all([
            OrderRecord(
                broker_order_id="manual-1",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=220.1,
                status="SUBMITTED",
                created_at=now_local,
            ),
            OrderRecord(
                broker_order_id="manual-2",
                symbol="AAPL.US",
                side="SELL",
                quantity=3,
                price=199.5,
                executed_quantity=1,
                executed_price=199.6,
                status="PARTIAL_FILLED",
                created_at=now_local - timedelta(minutes=30),
            ),
        ])
        db.commit()
        db.close()

        with freeze_time("2026-06-04 10:00:00", tz_offset=0):
            resp = client.get("/api/orders?page=2&page_size=1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "today"
        assert data["page"] == 2
        assert data["page_size"] == 1
        assert data["total"] == 2
        assert data["items"][0]["broker_order_id"] == "manual-2"
        assert data["items"][0]["source"] == "local"
        assert data["items"][0]["cancellable"] is True

    def test_orders_today_refresh_triggers_runner_sync(self, monkeypatch) -> None:
        _clean_orders()
        calls: list[bool] = []

        class Runner:
            broker = None

            def sync_today_orders_from_broker(self, *, force: bool) -> int:
                calls.append(force)
                return 0

        monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

        resp = client.get("/api/orders?refresh=1")

        assert resp.status_code == 200
        assert calls == [True]

    def test_cancel_order_uses_broker_for_any_order_and_updates_local_record(self, monkeypatch) -> None:
        _clean_orders()
        _clean_trade_events()
        db = SessionLocal()
        db.add(OrderRecord(
            broker_order_id="manual-1",
            symbol="NVDA.US",
            side="BUY",
            quantity=10,
            price=220.1,
            status="SUBMITTED",
        ))
        db.commit()
        db.close()

        calls = {}

        class Broker:
            def cancel_order(self, order_id: str):
                calls["order_id"] = order_id
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="CANCELLED",
                    executed_quantity=0,
                    executed_price=0,
                )

        class Runner:
            broker = Broker()

            def cancel_order_by_id(self, order_id: str):
                return self.broker.cancel_order(order_id)

        monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

        resp = client.post("/api/orders/manual-1/cancel")

        assert resp.status_code == 200
        assert calls["order_id"] == "manual-1"
        assert resp.json()["broker_order_id"] == "manual-1"
        assert resp.json()["status"] == "CANCELLED"

        db = SessionLocal()
        try:
            order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == "manual-1").one()
            assert order.status == "CANCELLED"
            assert order.filled_at is None
            event = db.query(TradeEvent).filter(TradeEvent.broker_order_id == "manual-1").one()
            assert event.event_type == "ORDER_CANCELLED"
            assert event.status == "CANCELLED"
        finally:
            db.close()

    def test_cancel_order_does_not_regress_partial_fill_from_stale_submitted(self, monkeypatch) -> None:
        _clean_orders()
        _clean_trade_events()
        db = SessionLocal()
        db.add(OrderRecord(
            broker_order_id="partial-stale-1",
            symbol="NVDA.US",
            side="BUY",
            quantity=10,
            price=220.1,
            executed_quantity=3,
            executed_price=220.05,
            status="PARTIAL_FILLED",
        ))
        db.commit()
        db.close()

        class Runner:
            broker = object()

            def cancel_order_by_id(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=0,
                    executed_price=0,
                )

        monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

        resp = client.post("/api/orders/partial-stale-1/cancel")

        assert resp.status_code == 200
        db = SessionLocal()
        try:
            order = db.query(OrderRecord).filter_by(broker_order_id="partial-stale-1").one()
            assert order.status == "PARTIAL_FILLED"
            assert order.executed_quantity == 3
            assert order.executed_price == 220.05
            assert db.query(TradeEvent).filter_by(broker_order_id="partial-stale-1").count() == 0
        finally:
            db.close()

    def test_cancel_order_terminal_status_preserves_existing_partial_fill(self, monkeypatch) -> None:
        _clean_orders()
        _clean_trade_events()
        with SessionLocal() as db:
            db.add(OrderRecord(
                broker_order_id="partial-cancelled-1",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=220.1,
                executed_quantity=3,
                executed_price=220.05,
                status="PARTIAL_FILLED",
            ))
            db.commit()

        class Runner:
            broker = object()

            def cancel_order_by_id(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="CANCELLED",
                    executed_quantity=0,
                    executed_price=0,
                )

        monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

        resp = client.post("/api/orders/partial-cancelled-1/cancel")

        assert resp.status_code == 200
        with SessionLocal() as db:
            order = db.query(OrderRecord).filter_by(broker_order_id="partial-cancelled-1").one()
            assert order.status == "CANCELLED"
            assert order.executed_quantity == 3
            assert order.executed_price == 220.05
            event = db.query(TradeEvent).filter_by(broker_order_id="partial-cancelled-1").one()
            assert event.event_type == "ORDER_CANCELLED"
            assert event.status == "CANCELLED"

    def test_local_order_status_merge_does_not_regress_terminal_fill(self) -> None:
        _clean_orders()
        _clean_trade_events()
        filled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db = SessionLocal()
        db.add(OrderRecord(
            broker_order_id="filled-stale-1",
            symbol="NVDA.US",
            side="BUY",
            quantity=10,
            price=220.1,
            executed_quantity=10,
            executed_price=220.08,
            status="FILLED",
            filled_at=filled_at,
        ))
        db.commit()
        try:
            trade_api._update_local_order_from_status(
                db,
                "filled-stale-1",
                SimpleNamespace(status="SUBMITTED", executed_quantity=None, executed_price=None),
            )

            order = db.query(OrderRecord).filter_by(broker_order_id="filled-stale-1").one()
            assert order.status == "FILLED"
            assert order.executed_quantity == 10
            assert order.executed_price == 220.08
            assert order.filled_at == filled_at.replace(tzinfo=None)
            assert db.query(TradeEvent).filter_by(broker_order_id="filled-stale-1").count() == 0
        finally:
            db.close()

    def test_trade_events_endpoint_returns_recent_events(self) -> None:
        _clean_trade_events()
        db = SessionLocal()
        db.add(TradeEvent(
            event_type="LLM_ANALYSIS",
            symbol="NVDA.US",
            broker_order_id="",
            side="",
            status="SUCCESS",
            message="analysis refreshed",
            payload_json='{"confidence": 0.75}',
        ))
        db.commit()
        db.close()

        resp = client.get("/api/events?limit=5&source=trade")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["event_type"] == "LLM_ANALYSIS"
        assert data["items"][0]["symbol"] == "NVDA.US"
        assert data["items"][0]["source"] == "trade"
        assert data["items"][0]["payload"]["confidence"] == 0.75

    def test_timeline_endpoint_merges_audit_and_trade(self) -> None:
        _clean_trade_events()
        _clean_audit_logs()
        _clean_llm_interactions()
        _clean_risk_events()
        db = SessionLocal()
        db.add(
            TradeEvent(
                event_type="ORDER_FILLED",
                symbol="AAPL.US",
                broker_order_id="o1",
                side="BUY",
                status="FILLED",
                message="ok",
                payload_json="{}",
            )
        )
        db.add(
            AuditLog(
                action="KILL_SWITCH",
                severity="CRITICAL",
                actor_hash="abc",
                source_ip="127.0.0.1",
                request_summary='{"reason":"panic"}',
                result="SUCCESS",
            )
        )
        db.commit()
        db.close()

        resp = client.get("/api/events?page=1&page_size=20&source=all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        types = {(row["source"], row["event_type"]) for row in data["items"]}
        assert ("trade", "ORDER_FILLED") in types
        assert ("audit", "KILL_SWITCH") in types
        ks = next(r for r in data["items"] if r["source"] == "audit")
        assert ks["severity"] == "CRITICAL"
        assert ks["actor_hash"] == "abc"

    def test_timeline_symbol_filter_excludes_unrelated_audit_rows(self) -> None:
        _clean_trade_events()
        _clean_audit_logs()
        db = SessionLocal()
        db.add(
            TradeEvent(
                event_type="ORDER_FILLED",
                symbol="AAPL.US",
                broker_order_id="o1",
                side="BUY",
                status="FILLED",
                message="ok",
                payload_json="{}",
            )
        )
        db.add(
            AuditLog(
                action="KILL_SWITCH",
                severity="CRITICAL",
                actor_hash="abc",
                source_ip="127.0.0.1",
                request_summary='{"reason":"panic"}',
                result="SUCCESS",
            )
        )
        db.commit()
        db.close()

        resp = client.get("/api/events?page=1&page_size=20&source=all&symbol=AAPL.US")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert [(row["source"], row["event_type"]) for row in data["items"]] == [
            ("trade", "ORDER_FILLED")
        ]

    def test_trade_events_export_returns_csv(self) -> None:
        _clean_trade_events()
        db = SessionLocal()
        db.add(TradeEvent(
            event_type="ORDER_FILLED",
            symbol="NVDA.US",
            broker_order_id="order-1",
            side="SELL",
            status="FILLED",
            message="order filled",
            payload_json='{"executed_price": 221.5}',
        ))
        db.commit()
        db.close()

        resp = client.get("/api/events/export?format=csv")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "ORDER_FILLED" in resp.text
        assert "order-1" in resp.text

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
            "trading_session_mode", "is_trading_hours",
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

    def test_diagnostics_returns_runner_health_snapshot(self, monkeypatch) -> None:
        class DiagnosticsRunner:
            def diagnostics(self):
                return {
                    "runner_running": True,
                    "thread_alive": True,
                    "quotes_subscribed": True,
                    "trigger_in_flight": False,
                    "pending_order_symbols": ["AAPL.US"],
                    "live_safety": {
                        "short_entries_enabled": False,
                        "allow_position_addons": False,
                        "max_position_quantity": 100,
                        "max_position_notional": 5000.0,
                        "max_risk_per_trade": 250.0,
                        "stop_loss_pct": 1.0,
                        "max_holding_minutes": 60,
                        "entry_cutoff_minutes_before_close": 45,
                        "flatten_minutes_before_close": 15,
                        "llm_shadow_mode": True,
                        "llm_order_execution_enabled": False,
                    },
                    "quote_stream": {
                        "last_push_age_seconds": 3.0,
                        "last_quote_age_seconds": 2.0,
                        "recent_quote_count": 4,
                    },
                    "risk": {
                        "paused": False,
                        "kill_switch": False,
                        "pause_reason": "",
                        "daily_pnl": 12.5,
                        "consecutive_losses": 1,
                    },
                    "symbol_runtimes": [
                        {
                            "symbol": "NVDA.US",
                            "market": "US",
                            "is_primary": True,
                            "engine_state": "flat",
                            "last_price": 220.5,
                            "last_trigger_price": 0.0,
                            "recent_quote_count": 0,
                            "has_pending_order": False,
                        },
                        {
                            "symbol": "AAPL.US",
                            "market": "US",
                            "is_primary": False,
                            "engine_state": "long",
                            "last_price": 199.5,
                            "last_trigger_price": 198.0,
                            "recent_quote_count": 2,
                            "has_pending_order": True,
                        },
                    ],
                }

        monkeypatch.setattr(strategy_api, "get_runner", lambda: DiagnosticsRunner())

        resp = client.get("/api/diagnostics")

        assert resp.status_code == 200
        data = resp.json()
        assert data["runner_running"] is True
        assert data["pending_order_symbols"] == ["AAPL.US"]
        assert data["quote_stream"]["recent_quote_count"] == 4
        assert data["risk"]["daily_pnl"] == 12.5
        assert data["live_safety"]["short_entries_enabled"] is False
        assert data["live_safety"]["max_position_notional"] == 5000.0
        assert data["live_safety"]["llm_shadow_mode"] is True
        assert data["symbol_runtimes"][1]["symbol"] == "AAPL.US"
        assert data["symbol_runtimes"][1]["has_pending_order"] is True

    def test_llm_account_context_uses_pending_order_for_requested_symbol(self, monkeypatch) -> None:
        class Broker:
            def get_cash(self, currency: str):
                return 10000

            def estimate_margin_max_quantity(self, symbol, side, price, currency=None):
                return 10

        nvda_pending = SimpleNamespace(
            broker_order_id="order-nvda",
            action="BUY",
            price=220.0,
            quantity=5,
        )
        aapl_pending = SimpleNamespace(
            broker_order_id="order-aapl",
            action="SELL",
            price=199.0,
            quantity=3,
        )

        class TradeService:
            pending_order = nvda_pending

            def pending_order_for(self, symbol: str):
                return aapl_pending if symbol == "AAPL.US" else None

        runner = SimpleNamespace(broker=Broker(), _trade_svc=TradeService())
        monkeypatch.setattr(llm_api, "get_runner", lambda: runner)

        context = llm_api._account_context("AAPL.US", "US", 199.0, False)

        assert context["pending_order"] == {
            "broker_order_id": "order-aapl",
            "side": "SELL",
            "price": 199.0,
            "quantity": 3.0,
        }

    def test_interval_reference_quantity_uses_effective_live_caps(self) -> None:
        trade_service = SimpleNamespace(
            margin_safety_factor=0.9,
            max_position_quantity=100,
            max_position_notional=5000.0,
            max_risk_per_trade=250.0,
            stop_loss_pct=1.0,
        )

        quantity = llm_api._interval_reference_quantity(
            {"quantity": 0},
            {"max_buy_quantity": 1000},
            current_price=200.0,
            trade_service=trade_service,
        )

        assert quantity == 25.0
        assert llm_api._interval_reference_quantity(
            {"quantity": 7},
            {"max_buy_quantity": 1000},
            current_price=200.0,
            trade_service=trade_service,
        ) == 7.0

    def test_llm_interval_status_includes_budget_and_symbol_statuses(self, monkeypatch) -> None:
        _clean_strategy()
        _clean_llm_interactions()
        resp = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 200.0,
            "sell_high": 220.0,
            "llm_interval_minutes": 5,
        })
        assert resp.status_code == 200

        class Runner:
            def llm_symbol_statuses(self):
                return [
                    {
                        "symbol": "NVDA.US",
                        "market": "US",
                        "is_primary": True,
                        "has_pending_order": False,
                        "buy_cooldown_remaining_seconds": 12.0,
                        "sell_cooldown_remaining_seconds": None,
                    },
                    {
                        "symbol": "AAPL.US",
                        "market": "US",
                        "is_primary": False,
                        "has_pending_order": True,
                        "buy_cooldown_remaining_seconds": None,
                        "sell_cooldown_remaining_seconds": 8.0,
                    },
                ]

        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())
        monkeypatch.setattr(llm_api.settings, "llm_max_symbols_per_cycle", 2)
        monkeypatch.setattr(llm_api.settings, "llm_max_analyses_per_hour", 40)

        resp = client.get("/api/strategy/llm-interval/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["shadow_mode"] is True
        assert data["policy_status"] == "SHADOW"
        assert data["budget"] == {
            "max_symbols_per_cycle": 2,
            "max_analyses_per_hour": 40,
            "tracked_symbol_count": 2,
            "effective_symbol_budget": 2,
            "used_analyses_last_hour": 0,
            "remaining_analyses_this_hour": 40,
        }
        assert data["symbol_statuses"][0]["symbol"] == "NVDA.US"
        assert data["symbol_statuses"][0]["buy_cooldown_remaining_seconds"] == 12.0
        assert data["symbol_statuses"][1]["symbol"] == "AAPL.US"
        assert data["symbol_statuses"][1]["has_pending_order"] is True

    def test_llm_shadow_status_never_presents_stale_values_as_current(self, monkeypatch) -> None:
        _clean_strategy()
        with SessionLocal() as db:
            config = StrategyService(db).get_config()
            config.llm_applied_buy_low = 100.0
            config.llm_applied_sell_high = 101.0
            db.commit()

        class Runner:
            def llm_symbol_statuses(self):
                return []

        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())
        monkeypatch.setattr(llm_api.settings, "llm_shadow_mode", True)

        resp = client.get("/api/strategy/llm-interval/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["policy_status"] == "SHADOW"
        assert data["applied_values"] is None
        assert data["last_applied_values"] == {"buy_low": 100.0, "sell_high": 101.0}

    def test_llm_interval_status_includes_persisted_schedule_state_and_usage(self, monkeypatch) -> None:
        _clean_strategy()
        _clean_llm_interactions()
        from app.models import LLMSymbolScheduleState

        resp = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 200.0,
            "sell_high": 220.0,
            "llm_interval_minutes": 5,
        })
        assert resp.status_code == 200
        _clean_llm_interactions()
        _clean_llm_symbol_schedule_state()

        db = SessionLocal()
        try:
            db.add(LLMSymbolScheduleState(
                symbol="NVDA.US",
                market="US",
                last_analysis_at=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
                next_analysis_at=datetime(2026, 6, 4, 10, 5, tzinfo=timezone.utc),
                last_status="ANALYZED",
                last_skip_reason="",
            ))
            db.add(LLMSymbolScheduleState(
                symbol="AAPL.US",
                market="US",
                last_analysis_at=datetime(2026, 6, 4, 9, 58, tzinfo=timezone.utc),
                next_analysis_at=datetime(2026, 6, 4, 10, 3, tzinfo=timezone.utc),
                last_status="SKIPPED",
                last_skip_reason="cycle budget exhausted",
            ))
            db.add(LLMInteraction(
                interaction_type="analyze",
                symbol="NVDA.US",
                market="US",
                prompt="p",
                raw_response="{}",
                parsed_response="{}",
                context_snapshot="{}",
                success=True,
                created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            ))
            db.add(LLMInteraction(
                interaction_type="analyze",
                symbol="AAPL.US",
                market="US",
                prompt="p",
                raw_response="{}",
                parsed_response="{}",
                context_snapshot="{}",
                success=True,
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            ))
            db.commit()
        finally:
            db.close()

        class Runner:
            def llm_symbol_statuses(self):
                return [
                    {
                        "symbol": "NVDA.US",
                        "market": "US",
                        "is_primary": True,
                        "has_pending_order": False,
                        "buy_cooldown_remaining_seconds": 12.0,
                        "sell_cooldown_remaining_seconds": None,
                    },
                    {
                        "symbol": "AAPL.US",
                        "market": "US",
                        "is_primary": False,
                        "has_pending_order": True,
                        "buy_cooldown_remaining_seconds": None,
                        "sell_cooldown_remaining_seconds": 8.0,
                    },
                ]

        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())
        monkeypatch.setattr(llm_api.settings, "llm_max_symbols_per_cycle", 2)
        monkeypatch.setattr(llm_api.settings, "llm_max_analyses_per_hour", 40)

        resp = client.get("/api/strategy/llm-interval/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["budget"]["used_analyses_last_hour"] == 2
        assert data["budget"]["remaining_analyses_this_hour"] == 38
        assert data["symbol_statuses"][0]["last_status"] == "ANALYZED"
        assert data["symbol_statuses"][0]["next_analysis_at"].startswith("2026-06-04T10:05:00")
        assert data["symbol_statuses"][1]["last_status"] == "SKIPPED"
        assert data["symbol_statuses"][1]["last_skip_reason"] == "cycle budget exhausted"

    def test_review_export_json_includes_filtered_diagnostics(self, monkeypatch) -> None:
        _clean_status_history()
        db = SessionLocal()
        try:
            db.add(RuntimeStateSnapshot(
                symbol="AAPL.US",
                engine_state="long",
                last_price=199.2,
                daily_pnl=8.5,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            ))
            db.commit()
        finally:
            db.close()

        class Runner:
            def diagnostics(self):
                return {
                    "runner_running": True,
                    "thread_alive": True,
                    "quotes_subscribed": True,
                    "trigger_in_flight": False,
                    "pending_order_symbols": ["AAPL.US", "NVDA.US"],
                    "quote_stream": {
                        "last_push_age_seconds": 2.0,
                        "last_quote_age_seconds": 1.0,
                        "recent_quote_count": 4,
                    },
                    "risk": {
                        "paused": False,
                        "kill_switch": False,
                        "pause_reason": "",
                        "daily_pnl": 0.0,
                        "consecutive_losses": 0,
                    },
                    "symbol_runtimes": [
                        {"symbol": "AAPL.US", "engine_state": "long"},
                        {"symbol": "NVDA.US", "engine_state": "flat"},
                    ],
                }

        monkeypatch.setattr(review_api, "get_runner", lambda: Runner())

        resp = client.get("/api/review/export?symbol=AAPL.US&from_date=2026-05-22&to_date=2026-05-22&format=json")

        assert resp.status_code == 200
        import json

        data = json.loads(resp.content.decode("utf-8"))
        assert data["review"]["symbol"] == "AAPL.US"
        assert [point["symbol"] for point in data["runtime_history"]["points"]] == ["AAPL.US"]
        assert data["diagnostics"]["pending_order_symbols"] == ["AAPL.US", "NVDA.US"]
        assert data["diagnostics"]["symbol_runtimes"] == [{"symbol": "AAPL.US", "engine_state": "long"}]

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
            def __init__(self, **_kwargs): pass
            def analyze(self, **_kwargs):
                return {
                    "success": False,
                    "error": "LLM analysis failed: DEEPSEEK_API_KEY is not configured",
                }

        monkeypatch.setattr(llm_api, "LLMAdvisorService", MissingKeyAdvisor)
        monkeypatch.setattr(
            llm_api.get_runner(),
            "fresh_market_price",
            lambda _symbol: 100.0,
        )

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
            def __init__(self, **_kwargs): pass
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
            broker = None

            @staticmethod
            def fresh_market_price(_symbol):
                return 200.0

        monkeypatch.setattr(llm_api, "LLMAdvisorService", SuccessfulAdvisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())
        monkeypatch.setattr(llm_api.settings, "llm_shadow_mode", False)

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["applied"] is True
        assert data["suggested_buy_low"] == 195.0
        assert data["suggested_sell_high"] == 205.0

        strategy = client.get("/api/strategy").json()
        # LONG state: buy_low must not chase upward; sell_high may be raised.
        assert strategy["buy_low"] == 100.0
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
            "short_selling": False,
            "min_profit_amount": 12.5,
        })
        assert setup.status_code == 200

        captured = {}

        class Advisor:
            def __init__(self, **_kwargs): pass
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

            @staticmethod
            def fresh_market_price(_symbol):
                return 221.5

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
        assert captured["account_context"]["max_short_quantity"] == 0.0

    def test_llm_analyze_records_immediate_order_action_in_shadow(self, monkeypatch) -> None:
        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 218.0,
            "sell_high": 225.0,
        })
        assert setup.status_code == 200
        outcome_updates: list[dict[str, object]] = []
        analysis_events: list[dict[str, object]] = []

        class InteractionService:
            def __init__(self, _db):
                pass

            def update_outcome(self, interaction_id, **kwargs):
                outcome_updates.append({"interaction_id": interaction_id, **kwargs})

        class Advisor:
            def __init__(self, **_kwargs): pass
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

            @staticmethod
            def fresh_market_price(_symbol):
                return 221.5

            def recent_price_context(self):
                return []

            def execute_llm_order_decision(self, decision):
                assert decision["order_action"] == "BUY_NOW"
                assert decision["order_price"] == 221.5
                return {
                    "executed": False,
                    "status": "SHADOW_ONLY",
                    "order_id": None,
                    "policy_code": "SHADOW_MODE",
                    "policy_disposition": "SHADOW",
                    "confidence": 0.82,
                    "reference_price": 221.4,
                    "candidate_price": 221.5,
                    "deviation_pct": 0.045,
                }

        monkeypatch.setattr(llm_api, "LLMAdvisorService", Advisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())
        monkeypatch.setattr(llm_api, "LLMInteractionService", InteractionService)
        monkeypatch.setattr(
            llm_api,
            "record_trade_event",
            lambda *_args, **kwargs: analysis_events.append(kwargs),
        )

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["order_action"] == "BUY_NOW"
        assert data["order_status"] == "SHADOW_ONLY"
        assert data["order_id"] is None
        assert "shadow mode" in data["reason"].lower()
        expected_policy_outcome = {
            "code": "SHADOW_MODE",
            "reference_price": 221.4,
            "candidate_price": 221.5,
            "deviation_pct": 0.045,
            "confidence": 0.82,
            "disposition": "SHADOW",
        }
        assert outcome_updates[0]["policy_outcome"] == expected_policy_outcome
        event_payload = analysis_events[0]["payload"]
        assert isinstance(event_payload, dict)
        assert event_payload["policy_outcome"] == expected_policy_outcome
        db = SessionLocal()
        try:
            config = StrategyService(db).get_config()
            assert config.buy_low == 218.0
            assert config.sell_high == 225.0
            assert config.llm_reject_reason is None
        finally:
            db.close()

    def test_llm_analyze_rejects_low_confidence_immediate_order_action(self, monkeypatch) -> None:
        _clean_strategy()
        setup = client.put("/api/strategy", json={
            "symbol": "NVDA.US",
            "market": "US",
            "buy_low": 218.0,
            "sell_high": 225.0,
        })
        assert setup.status_code == 200

        class Advisor:
            def __init__(self, **_kwargs): pass
            def analyze(self, **_kwargs):
                return {
                    "success": True,
                    "interaction_id": 103,
                    "suggested_buy_low": 219.0,
                    "suggested_sell_high": 224.0,
                    "confidence_score": 0.30,
                    "analysis": "panic sell",
                    "next_analysis_at": "2026-05-22T10:03:00+00:00",
                    "order_action": "STOP_LOSS_SELL_NOW",
                    "order_price": 215.0,
                    "order_reason": "support failed",
                }

        class Runner:
            class Engine:
                last_price = 221.5

                class State:
                    value = "long"

                state = State()

            engine = Engine()
            broker = object()

            @staticmethod
            def fresh_market_price(_symbol):
                return 221.5

            def recent_price_context(self):
                return []

            def execute_llm_order_decision(self, decision):
                assert decision["confidence_score"] == 0.30
                return {
                    "executed": False,
                    "status": "POLICY_REJECTED",
                    "order_id": None,
                    "reason": "confidence below threshold",
                }

        monkeypatch.setattr(llm_api, "LLMAdvisorService", Advisor)
        monkeypatch.setattr(llm_api, "get_runner", lambda: Runner())

        resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["applied"] is False
        assert data["order_action"] == "STOP_LOSS_SELL_NOW"
        assert data["order_status"] == "POLICY_REJECTED"
        assert data["order_id"] is None

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

    def test_update_strategy_allows_trade_safety_settings(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "fee_rate_us": 0.001,
            "fee_rate_hk": 0.004,
            "min_repricing_pct": 0.004,
            "llm_action_cooldown_seconds": 120,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["fee_rate_us"] == 0.001
        assert data["fee_rate_hk"] == 0.004
        assert data["min_repricing_pct"] == 0.004
        assert data["llm_action_cooldown_seconds"] == 120

    def test_update_strategy_rejects_invalid_trade_safety_settings(self) -> None:
        _clean_strategy()
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "fee_rate_us": -0.01,
            "min_repricing_pct": 0.06,
            "llm_action_cooldown_seconds": 3601,
        })

        assert resp.status_code == 422

    def test_credentials_response_hides_values(self, monkeypatch) -> None:
        monkeypatch.setattr(
            credentials_api,
            "get_runner",
            lambda: SimpleNamespace(reload_credentials=lambda **_kwargs: None),
        )
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


def test_start_endpoint_writes_audit_success() -> None:
    _clean_audit_logs()
    resp = client.post("/api/control/start", headers={"x-api-key": "k1"})
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter_by(action="START").all()
        assert len(rows) == 1
        assert rows[0].result == "SUCCESS"
        assert rows[0].actor_hash != "anonymous"
    finally:
        db.close()


def test_kill_switch_endpoint_writes_critical() -> None:
    _clean_audit_logs()
    resp = client.post("/api/control/kill-switch", json={"reason": "test"})
    assert resp.status_code == 200
    import json

    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter_by(action="KILL_SWITCH").one()
        assert row.severity == "CRITICAL"
        assert json.loads(row.request_summary)["reason"] == "test"
    finally:
        db.close()


def test_start_failure_writes_failed_audit(monkeypatch) -> None:
    _clean_audit_logs()

    class Runner:
        risk = type("Risk", (), {"kill_switch": True})()

        def start(self):
            return True

    monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())
    resp = client.post("/api/control/start")
    assert resp.status_code == 403
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter_by(action="START").all()
        assert any(r.result == "FAILED" for r in rows)
    finally:
        db.close()


def test_start_unexpected_exception_writes_failed_audit(monkeypatch) -> None:
    _clean_audit_logs()

    class Runner:
        risk = type("Risk", (), {"kill_switch": False})()

        def start(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())
    resp = client.post("/api/control/start")
    assert resp.status_code == 500
    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter_by(action="START").order_by(AuditLog.id.desc()).first()
        assert row is not None
        assert row.result == "FAILED"
        assert "boom" in row.request_summary
    finally:
        db.close()


def test_start_endpoint_writes_global_scope_to_audit_and_event(monkeypatch) -> None:
    _clean_audit_logs()
    _clean_trade_events()

    class Risk:
        kill_switch = False
        paused = False
        pause_reason = ""
        paused_at = None
        pause_auto_resumable = False

    runner = SimpleNamespace(
        risk=Risk(),
        engine=SimpleNamespace(params=SimpleNamespace(symbol="AAPL.US")),
        _symbol_runtimes={
            "AAPL.US": SimpleNamespace(),
            "NVDA.US": SimpleNamespace(),
        },
        start=lambda: True,
    )
    monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

    resp = client.post("/api/control/start", headers={"x-api-key": "k1"})

    assert resp.status_code == 200
    import json

    db = SessionLocal()
    try:
        audit_row = db.query(AuditLog).filter_by(action="START").order_by(AuditLog.id.desc()).one()
        summary = json.loads(audit_row.request_summary)
        assert summary["global_scope"] is True
        assert summary["primary_symbol"] == "AAPL.US"
        assert summary["affected_symbols"] == ["AAPL.US", "NVDA.US"]
        assert summary["runtime_count"] == 2

        event_row = db.query(TradeEvent).filter_by(event_type="CONTROL_START").order_by(TradeEvent.id.desc()).one()
        payload = json.loads(event_row.payload_json)
        assert payload["global_scope"] is True
        assert payload["affected_symbols"] == ["AAPL.US", "NVDA.US"]
        assert payload["runtime_count"] == 2
    finally:
        db.close()


def test_kill_switch_writes_global_scope_to_audit_and_event(monkeypatch) -> None:
    _clean_audit_logs()
    _clean_trade_events()

    class Risk:
        def __init__(self) -> None:
            self.kill_switch = False
            self._pause_reason = ""
            self._paused_at = None
            self._pause_auto_resumable = False

        def pause(self, reason: str) -> None:
            self._pause_reason = reason
            self._paused_at = datetime.now(timezone.utc)

        def enable_kill_switch(self, reason: str) -> None:
            self.kill_switch = True

        @property
        def pause_reason(self) -> str:
            return self._pause_reason

        @property
        def paused_at(self):
            return self._paused_at

        @property
        def pause_auto_resumable(self) -> bool:
            return self._pause_auto_resumable

    runner = SimpleNamespace(
        risk=Risk(),
        notifier=SimpleNamespace(notify_risk_event=lambda *args, **kwargs: None),
        engine=SimpleNamespace(params=SimpleNamespace(symbol="AAPL.US")),
        _symbol_runtimes={
            "AAPL.US": SimpleNamespace(),
            "NVDA.US": SimpleNamespace(),
            "MSFT.US": SimpleNamespace(),
        },
    )
    monkeypatch.setattr(trade_api, "get_runner", lambda: runner)

    resp = client.post("/api/control/kill-switch", json={"reason": "panic"})

    assert resp.status_code == 200
    import json

    db = SessionLocal()
    try:
        audit_row = db.query(AuditLog).filter_by(action="KILL_SWITCH").order_by(AuditLog.id.desc()).one()
        summary = json.loads(audit_row.request_summary)
        assert summary["reason"] == "panic"
        assert summary["global_scope"] is True
        assert summary["affected_symbols"] == ["AAPL.US", "MSFT.US", "NVDA.US"]
        assert summary["runtime_count"] == 3

        event_row = db.query(TradeEvent).filter_by(event_type="CONTROL_KILL_SWITCH").order_by(TradeEvent.id.desc()).one()
        payload = json.loads(event_row.payload_json)
        assert payload["reason"] == "panic"
        assert payload["primary_symbol"] == "AAPL.US"
        assert payload["affected_symbols"] == ["AAPL.US", "MSFT.US", "NVDA.US"]
        assert payload["runtime_count"] == 3
    finally:
        db.close()

def test_strategy_update_writes_diff_audit() -> None:
    _clean_audit_logs()
    _clean_strategy()
    import json

    client.put(
        "/api/strategy",
        json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        },
    )
    resp = client.put("/api/strategy", json={"buy_low": 99.5, "sell_high": 105.0})
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter_by(action="STRATEGY_UPDATE")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.result == "SUCCESS"
        changed = json.loads(row.request_summary)["changed"]
        assert "buy_low" in changed
        assert changed["buy_low"]["new"] == 99.5
        assert "sell_high" in changed
    finally:
        db.close()


def test_strategy_update_audits_symbol_market_and_mode_changes() -> None:
    _clean_audit_logs()
    _clean_strategy()
    import json

    client.put(
        "/api/strategy",
        json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "trading_session_mode": "ANY",
        },
    )
    resp = client.put(
        "/api/strategy",
        json={
            "symbol": "0700.HK",
            "market": "HK",
            "trading_session_mode": "RTH_ONLY",
        },
    )
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter_by(action="STRATEGY_UPDATE")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        changed = json.loads(row.request_summary)["changed"]
        assert changed["symbol"]["new"] == "0700.HK"
        assert changed["market"]["new"] == "HK"
        assert changed["trading_session_mode"]["new"] == "RTH_ONLY"
    finally:
        db.close()


def test_strategy_update_no_change_still_writes_audit() -> None:
    _clean_audit_logs()
    import json

    cur = client.get("/api/strategy").json()
    resp = client.put("/api/strategy", json={"buy_low": cur["buy_low"]})
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter_by(action="STRATEGY_UPDATE")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert json.loads(row.request_summary)["changed"] == {}
    finally:
        db.close()


def test_strategy_update_returns_consistency_warnings() -> None:
    """Verify consistency_warnings field is present in strategy update response."""
    _clean_strategy()
    # min_profit_amount below round-trip fee triggers a consistency warning.
    resp = client.put("/api/strategy", json={
        "symbol": "AAPL.US",
        "market": "US",
        "buy_low": 100.0,
        "sell_high": 200.0,
        "min_profit_amount": 0.0001,
        "fee_rate_us": 0.005,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "consistency_warnings" in data
    assert len(data["consistency_warnings"]) > 0
    assert any(w["field"] == "min_profit_amount" for w in data["consistency_warnings"])


def test_cancel_order_returns_200_when_local_db_update_fails(monkeypatch) -> None:
    """Broker-side cancel succeeded; local DB update failure should still return 200."""
    _clean_orders()
    _clean_trade_events()
    _clean_audit_logs()
    db = SessionLocal()
    db.add(OrderRecord(
        broker_order_id="cancel-dbfail-1",
        symbol="NVDA.US",
        side="BUY",
        quantity=10,
        price=220.1,
        status="SUBMITTED",
    ))
    db.commit()
    db.close()

    class Broker:
        def cancel_order(self, order_id: str):
            return SimpleNamespace(
                broker_order_id=order_id,
                status="CANCELLED",
                executed_quantity=0,
                executed_price=0,
            )

    class Runner:
        broker = Broker()
        def cancel_order_by_id(self, order_id: str):
            return self.broker.cancel_order(order_id)

    monkeypatch.setattr(trade_api, "get_runner", lambda: Runner())

    def _failing_update(*args: object, **kwargs: object) -> None:
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(trade_api, "_update_local_order_from_status", _failing_update)

    resp = client.post("/api/orders/cancel-dbfail-1/cancel")

    assert resp.status_code == 200
    data = resp.json()
    assert data["broker_order_id"] == "cancel-dbfail-1"
    assert data["status"] == "CANCELLED"


def test_llm_analyze_returns_400_when_price_unavailable(monkeypatch) -> None:
    """When last_price is 0/None and buy_low is not set, return 400."""
    _clean_strategy()
    setup = client.put("/api/strategy", json={
        "symbol": "AAPL.US",
        "market": "US",
        "buy_low": 100.0,
        "sell_high": 200.0,
    })
    assert setup.status_code == 200

    # Override strategy to have buy_low = 0 (must be >0 to avoid the 400,
    # but setting it to 0 makes the guard trigger since current_price <= 0).
    db = SessionLocal()
    try:
        config = db.query(StrategyConfig).first()
        assert config is not None
        config.buy_low = 0.0
        db.commit()
    finally:
        db.close()

    class Engine:
        last_price = 0.0
        class State:
            value = "flat"
        state = State()

    runner = SimpleNamespace(
        engine=Engine(),
        broker=object(),
        fresh_market_price=lambda _symbol: None,
    )
    monkeypatch.setattr(llm_api, "get_runner", lambda: runner)

    resp = client.post("/api/strategy/llm-interval/analyze", json={"force": True})

    assert resp.status_code == 400
    data = resp.json()
    assert "current price unavailable" in data["detail"]


def test_llm_analyze_rejects_positive_but_stale_persisted_price(monkeypatch) -> None:
    _clean_strategy()
    with SessionLocal() as db:
        db.add(
            StrategyConfig(
                symbol="AAPL.US",
                market="US",
                buy_low=100.0,
                sell_high=200.0,
            )
        )
        db.commit()

    engine = SimpleNamespace(
        last_price=150.0,
        state=SimpleNamespace(value="flat"),
    )
    runner = SimpleNamespace(
        engine=engine,
        broker=object(),
        fresh_market_price=lambda _symbol: None,
    )
    monkeypatch.setattr(llm_api, "get_runner", lambda: runner)

    response = client.post(
        "/api/strategy/llm-interval/analyze",
        json={"force": True},
    )

    assert response.status_code == 400
    assert "current price unavailable" in response.json()["detail"]
