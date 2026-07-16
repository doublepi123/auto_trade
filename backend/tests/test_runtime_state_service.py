# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

from app import database
from app.config import settings
from app.core.engine import StrategyEngine, EngineState, StrategyParams
from app.core.risk import RiskController
from app.models import RiskEvent, RuntimeState, RuntimeStateSnapshot, StrategyConfig
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService


database.init_db()


class TestRuntimeStateService:
    def _get_db(self):
        return database.SessionLocal()

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.query(RuntimeStateSnapshot).delete()
        db.query(RuntimeState).delete()
        db.query(RiskEvent).delete()
        db.commit()
        db.close()

    def test_load_restores_engine_and_risk(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "auto_resume_minutes": 5,
            "max_daily_loss": 3000.0,
            "max_consecutive_losses": 2,
            "fee_rate_us": 0.001,
            "fee_rate_hk": 0.004,
            "min_repricing_pct": 0.004,
            "llm_action_cooldown_seconds": 120,
        })
        from datetime import datetime, timezone
        paused_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
        svc.update_runtime_state(
            engine_state="long",
            last_price=150.0,
            daily_pnl=-100.0,
            consecutive_losses=1,
            kill_switch=False,
            paused=True,
            pause_reason="429 too many requests",
            paused_at=paused_at,
            pause_auto_resumable=True,
            last_trigger_price=145.0,
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.params.symbol == "AAPL.US"
        assert engine.params.auto_resume_minutes == 5
        assert engine.params.fee_rate_us == 0.001
        assert engine.params.fee_rate_hk == 0.004
        assert engine.params.min_repricing_pct == 0.004
        assert engine.params.llm_action_cooldown_seconds == 120
        assert engine.state == EngineState.LONG
        assert engine.last_price == 150.0
        assert risk.daily_pnl == -100.0
        assert risk.consecutive_losses == 1
        assert risk.paused is True
        assert risk.pause_reason == "429 too many requests"
        assert risk.paused_at == paused_at
        assert risk.pause_auto_resumable is True

    def test_load_invalid_safety_values_uses_deployment_hard_limits(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "stop_loss_pct": float("inf"),
            "max_holding_minutes": 0,
            "entry_cutoff_minutes_before_close": -10,
            "flatten_minutes_before_close": 0,
        })
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        db = self._get_db()
        RuntimeStateService().load(db, engine, risk)
        db.close()

        assert engine.params.stop_loss_pct == settings.hard_stop_loss_pct
        assert engine.params.max_holding_minutes == settings.hard_max_holding_minutes
        assert (
            engine.params.entry_cutoff_minutes_before_close
            == settings.hard_entry_cutoff_minutes_before_close
        )
        assert (
            engine.params.flatten_minutes_before_close
            == settings.hard_flatten_minutes_before_close
        )

    def test_load_defaults_on_invalid_engine_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "TSLA.US", "market": "US", "buy_low": 50.0, "sell_high": 100.0})
        svc.update_runtime_state(engine_state="invalid_state")
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.state == EngineState.FLAT

    def test_persist_saves_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "NVDA.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        db.close()

        engine = StrategyEngine()
        engine.params = StrategyParams(symbol="NVDA.US", market="US")
        engine.state = EngineState.SHORT
        engine.last_price = 180.0
        risk = RiskController()
        risk.daily_pnl = -50.0
        risk.consecutive_losses = 2
        from datetime import datetime, timezone
        paused_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
        risk.pause("429 too many requests", auto_resumable=True, paused_at=paused_at)

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist(db, engine, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state(symbol="NVDA.US")
        db.close()

        assert state.engine_state == "short"
        assert state.last_price == 180.0
        assert state.daily_pnl == -50.0
        assert state.consecutive_losses == 2
        assert state.paused is True
        assert state.pause_reason == "429 too many requests"
        assert state.paused_at is not None
        assert state.paused_at.replace(tzinfo=timezone.utc) == paused_at
        assert state.pause_auto_resumable is True

    def test_stage_keeps_runtime_state_and_snapshot_in_caller_transaction(
        self,
    ) -> None:
        self._cleanup()
        engine = StrategyEngine(StrategyParams(symbol="ATOMIC.US", market="US"))
        risk = RiskController()
        risk.pause("ORDER_EXECUTION_BLOCKED: keep persisted pause")
        state_svc = RuntimeStateService()

        db = self._get_db()
        try:
            state_svc.persist(db, engine, risk)
        finally:
            db.close()

        risk.resume()
        db = self._get_db()
        try:
            state_svc.stage(db, engine, risk)
            db.flush()
            assert (
                db.query(RuntimeState)
                .filter(RuntimeState.symbol == "ATOMIC.US")
                .one()
                .paused
                is False
            )
            assert (
                db.query(RuntimeStateSnapshot)
                .filter(RuntimeStateSnapshot.symbol == "ATOMIC.US")
                .count()
                == 2
            )
            db.rollback()
        finally:
            db.close()

        db = self._get_db()
        try:
            state = (
                db.query(RuntimeState)
                .filter(RuntimeState.symbol == "ATOMIC.US")
                .one()
            )
            assert state.paused is True
            assert state.pause_reason == (
                "ORDER_EXECUTION_BLOCKED: keep persisted pause"
            )
            assert (
                db.query(RuntimeStateSnapshot)
                .filter(RuntimeStateSnapshot.symbol == "ATOMIC.US")
                .count()
                == 1
            )
        finally:
            db.close()

    def test_persist_risk_saves_only_risk(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "META.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(engine_state="flat", last_price=150.0)
        db.close()

        risk = RiskController()
        risk.daily_pnl = -25.0
        risk.consecutive_losses = 1

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist_risk(db, risk, symbol="META.US")
        db.close()

        db = self._get_db()
        state = svc.get_primary_runtime_state()
        db.close()

        assert state.daily_pnl == -25.0
        assert state.consecutive_losses == 1
        assert state.engine_state == "flat"

    def test_persist_records_status_history_snapshot(self) -> None:
        self._cleanup()
        engine = StrategyEngine()
        engine.state = EngineState.LONG
        engine.last_price = 221.5
        risk = RiskController()
        risk.daily_pnl = 12.25
        risk.consecutive_losses = 0

        state_svc = RuntimeStateService()
        db = self._get_db()
        try:
            state_svc.persist(db, engine, risk)
            points = state_svc.query_history(db, limit=10)
        finally:
            db.close()

        assert len(points) == 1
        assert points[0].engine_state == "long"
        assert points[0].last_price == 221.5
        assert points[0].daily_pnl == 12.25

    def test_query_history_returns_points_in_time_order(self) -> None:
        self._cleanup()
        from datetime import datetime, timezone

        db = self._get_db()
        try:
            db.add(RuntimeStateSnapshot(
                engine_state="flat",
                last_price=220.0,
                daily_pnl=0.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            ))
            db.add(RuntimeStateSnapshot(
                engine_state="long",
                last_price=221.0,
                daily_pnl=5.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 2, tzinfo=timezone.utc),
            ))
            db.commit()

            points = RuntimeStateService().query_history(db, limit=10)
        finally:
            db.close()

        assert [point.last_price for point in points] == [220.0, 221.0]


    def test_get_runtime_state_creates_symbol_scoped_rows(self) -> None:
        self._cleanup()
        db = self._get_db()
        try:
            svc = StrategyService(db)
            primary = svc.get_runtime_state()
            nvda = svc.get_runtime_state(symbol="NVDA.US")
            aapl = svc.get_runtime_state(symbol="AAPL.US")
            rows = [
                (primary.id, primary.symbol),
                (nvda.id, nvda.symbol),
                (aapl.id, aapl.symbol),
            ]
        finally:
            db.close()

        assert rows[0][1] == ""
        assert rows[1][1] == "NVDA.US"
        assert rows[2][1] == "AAPL.US"
        assert rows[0][0] != rows[1][0]
        assert rows[1][0] != rows[2][0]

    def test_persist_symbol_saves_independent_engine_state(self) -> None:
        self._cleanup()
        nvda_engine = StrategyEngine(StrategyParams(symbol="NVDA.US", market="US"))
        nvda_engine.state = EngineState.LONG
        nvda_engine.last_price = 222.5
        aapl_engine = StrategyEngine(StrategyParams(symbol="AAPL.US", market="US"))
        aapl_engine.state = EngineState.SHORT
        aapl_engine.last_price = 199.5

        state_svc = RuntimeStateService()
        db = self._get_db()
        try:
            state_svc.persist_symbol(db, nvda_engine)
            state_svc.persist_symbol(db, aapl_engine)
            nvda = StrategyService(db).get_runtime_state(symbol="NVDA.US")
            aapl = StrategyService(db).get_runtime_state(symbol="AAPL.US")
        finally:
            db.close()

        assert nvda.engine_state == "long"
        assert nvda.last_price == 222.5
        assert aapl.engine_state == "short"
        assert aapl.last_price == 199.5

    def test_query_history_filters_by_symbol(self) -> None:
        self._cleanup()
        from datetime import datetime, timezone

        db = self._get_db()
        try:
            db.add(RuntimeStateSnapshot(
                symbol="NVDA.US",
                engine_state="long",
                last_price=222.0,
                daily_pnl=0.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            ))
            db.add(RuntimeStateSnapshot(
                symbol="AAPL.US",
                engine_state="short",
                last_price=199.0,
                daily_pnl=0.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 2, tzinfo=timezone.utc),
            ))
            db.commit()

            points = RuntimeStateService().query_history(db, symbol="NVDA.US", limit=10)
        finally:
            db.close()

        assert [point.symbol for point in points] == ["NVDA.US"]
        assert [point.last_price for point in points] == [222.0]

    def test_record_risk_event(self) -> None:
        self._cleanup()
        db = self._get_db()
        state_svc = RuntimeStateService()
        state_svc.record_risk_event(db, "test risk reason")
        db.close()

        db = self._get_db()
        events = db.query(RiskEvent).all()
        db.close()

        assert len(events) == 1
        assert events[0].event_type == "RISK_REJECTION"
        assert events[0].reason == "test risk reason"

    def test_load_resets_daily_pnl_when_day_changed(self) -> None:
        from datetime import datetime, timedelta, timezone

        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        utc_day = datetime.now(timezone.utc).date()
        svc.update_config({"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(
            engine_state="flat",
            daily_pnl=-500.0,
            consecutive_losses=3,
            daily_pnl_date=utc_day - timedelta(days=1),
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert risk.daily_pnl == 0.0
        assert risk.consecutive_losses == 0

    def test_load_keeps_daily_pnl_when_same_day(self) -> None:
        from datetime import datetime, timezone

        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        utc_day = datetime.now(timezone.utc).date()
        svc.update_config({"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(
            engine_state="flat",
            daily_pnl=-500.0,
            consecutive_losses=3,
            daily_pnl_date=utc_day,
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert risk.daily_pnl == -500.0
        assert risk.consecutive_losses == 3

    def test_persist_saves_daily_pnl_date(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "NVDA.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        db.close()

        engine = StrategyEngine()
        engine.params = StrategyParams(symbol="NVDA.US", market="US")
        risk = RiskController()
        risk.daily_pnl = -25.0

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist(db, engine, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state(symbol="NVDA.US")
        db.close()

        from datetime import datetime, timezone
        assert state.daily_pnl == -25.0
        assert state.daily_pnl_date == datetime.now(timezone.utc).date()

    def test_get_primary_runtime_state_migrates_legacy_row(self) -> None:
        self._cleanup()
        db = self._get_db()
        try:
            svc = StrategyService(db)
            svc.update_config({"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
            legacy = svc.update_runtime_state(engine_state="long", last_price=180.0)
            assert legacy.symbol == ""

            migrated = svc.get_primary_runtime_state()
            assert migrated.symbol == "AAPL.US"
            assert migrated.engine_state == "long"
            assert migrated.last_price == 180.0
        finally:
            db.close()

    def test_persist_writes_primary_symbol_snapshots(self) -> None:
        self._cleanup()
        db = self._get_db()
        try:
            StrategyService(db).update_config(
                {"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0}
            )
            engine = StrategyEngine(StrategyParams(symbol="AAPL.US", market="US"))
            engine.state = EngineState.LONG
            engine.last_price = 190.0
            risk = RiskController()

            state_svc = RuntimeStateService()
            state_svc.persist(db, engine, risk)

            points = state_svc.query_history(db, symbol="AAPL.US", limit=10)
            assert len(points) == 1
            assert points[0].symbol == "AAPL.US"
            assert points[0].last_price == 190.0
        finally:
            db.close()

    def test_query_history_includes_legacy_snapshots_for_primary_symbol(self) -> None:
        self._cleanup()
        from datetime import datetime, timezone

        db = self._get_db()
        try:
            db.add(RuntimeStateSnapshot(
                symbol="",
                engine_state="flat",
                last_price=220.0,
                daily_pnl=0.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 1, tzinfo=timezone.utc),
            ))
            db.add(RuntimeStateSnapshot(
                symbol="AAPL.US",
                engine_state="long",
                last_price=221.0,
                daily_pnl=5.0,
                consecutive_losses=0,
                paused=False,
                kill_switch=False,
                created_at=datetime(2026, 5, 22, 10, 2, tzinfo=timezone.utc),
            ))
            db.commit()

            points = RuntimeStateService().query_history(
                db,
                symbol="AAPL.US",
                include_legacy_empty=True,
                limit=10,
            )
        finally:
            db.close()

        assert [point.last_price for point in points] == [220.0, 221.0]
        assert [point.symbol for point in points] == ["", "AAPL.US"]
