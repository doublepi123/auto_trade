"""Real constructor + complete initialize chain; only external I/O is replaced."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.broker import OrderStatusResult, Position, Quote
from app.core.risk import ResumeBlockedError, RiskController
from app.models import OrderRecord, PassiveMandate, TrackedEntry
from tests.test_runner_passive_recovery_regressions import (
    NOW, _mandate_row, _order_row, _passive_config_snapshot, _submitted_event,
)


class _FakeStartupBroker:
    def __init__(self) -> None:
        self.positions: list[Position] | None = []
        self.status = "SUBMITTED"
        self.qty: Decimal | None = Decimal("0")
        self.price: Decimal | None = None
        self.read_fault = False
        self.calls: list[str] = []
        self.submitted = 0
        self.cancelled = 0

    def get_positions(self) -> list[Position] | None:
        self.calls.append("positions")
        return self.positions

    def get_today_orders(self) -> list[Any]:
        self.calls.append("today_orders")
        return []

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        self.calls.append("order_status:" + order_id)
        if self.read_fault:
            raise OSError("temporary broker read failure")
        return OrderStatusResult(order_id, self.status, cast(Any, self.qty), cast(Any, self.price))

    def get_cash(self, currency: str | None = None) -> Decimal:
        return Decimal("10000")

    def register_disconnect_hook(self, callback: Any) -> None:
        self.calls.append("disconnect_hook")

    def subscribe_quotes_batch(self, symbols: list[str], callback: Any) -> None:
        self.calls.append("subscribe")

    def close(self) -> None:
        pass

    def submit_limit_order(self, *args: Any, **kwargs: Any) -> Any:
        self.submitted += 1
        raise AssertionError("startup must never submit")

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        self.cancelled += 1
        raise AssertionError("startup must never cancel")


@contextmanager
def _startup_environment(path: Path, broker: Any) -> Iterator[tuple[Any, Any]]:
    """Also used by the fresh crash-recovery process, on its existing DB."""
    from app import database, runner
    from app.api import deps
    from app.services import order_terminal_callback_service
    from app.services.credentials_service import PlainCredentials

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(database, "engine", engine)
        for module in (database, runner, order_terminal_callback_service):
            patch.setattr(module, "SessionLocal", sessions)
        patch.setattr(deps, "_audit_logger_singleton", None)
        patch.setattr(runner.AppRunner, "_build_broker", staticmethod(lambda audit: broker))
        patch.setattr(runner.AppRunner, "_load_credentials", lambda self, db=None: PlainCredentials())
        database.init_db()
        try:
            yield runner.AppRunner, sessions
        finally:
            engine.dispose()


def _seed_known(sessions: Any, *, status: str, qty: Decimal | None, holding: bool = False) -> None:
    from app.services.strategy_service import StrategyService

    mandate = _mandate_row(submit_state="ORDER_KNOWN", bound_id="startup-1", bound_status=status)
    mandate.bound_executed_quantity = qty
    mandate.bound_executed_price = Decimal("600") if holding else None
    with sessions() as db:
        db.add(mandate)
        svc = StrategyService(db)
        config = svc.get_config()
        config.symbol = "SPY.US"
        config.market = "US"
        state = svc.get_primary_runtime_state()
        state.paused = True
        state.pause_reason = "MANUAL: owner pause"
        if holding:
            db.add(TrackedEntry(symbol="SPY.US", side="LONG", quantity=8, cost=4800, opened_at=NOW))
        db.commit()
    _order_row(sessions, "startup-1", config_snapshot=_passive_config_snapshot(), status=status)
    _submitted_event(sessions, "startup-1", passive_owner_ref="1:claim-r1:exec-r1")
    if holding:
        with sessions() as db:
            order = db.query(OrderRecord).filter_by(broker_order_id="startup-1").one()
            order.executed_quantity = 8
            order.executed_price = 600
            order.filled_at = NOW
            tracked = db.get(TrackedEntry, "SPY.US")
            assert tracked is not None
            tracked.updated_at = NOW
            db.commit()


def _assert_full_startup(runner: Any, broker: _FakeStartupBroker) -> None:
    assert runner._passive_recovery_inventoried
    assert runner._passive_recovery_complete
    assert "today_orders" in broker.calls
    assert "positions" in broker.calls
    assert "subscribe" in broker.calls
    assert broker.submitted == broker.cancelled == 0


def test_full_initialize_known_holding_and_real_reduction_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from app.services import trade_execution_service
    monkeypatch.setattr(trade_execution_service, "is_trading_hours", lambda market: True)
    broker = _FakeStartupBroker()
    broker.status, broker.qty, broker.price = "FILLED", Decimal("8"), Decimal("600")
    broker.positions = [Position("SPY.US", "LONG", Decimal("8"), Decimal("600"), Decimal("8"))]
    with _startup_environment(tmp_path / "holding.db", broker) as (constructor, sessions):
        _seed_known(sessions, status="FILLED", qty=Decimal("8"), holding=True)
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        assert "pending-ref restoration issues" not in caplog.text
        assert runner.risk.external_block() is None
        assert runner._passive_quarantined_symbols == frozenset({"SPY.US"})
        assert runner.risk.pause_reason == "MANUAL: owner pause"
        assert runner._trade_svc.pending_order_by_broker_id("startup-1") is None
        assert runner._passive_recovery_hard_reasons == ()
        # A separate active real risk controller proves it is the final
        # quarantine, not the preserved owner pause, that refuses reduction.
        result = runner._trade_svc.execute(
            "SELL", "SPY.US", Quote("SPY.US", 620, 619.99, 620.01, "t"),
            broker, RiskController(), runner.notifier, "USD",
            expected_exit_price=Decimal("620"), allow_loss_exit=True,
        )
        assert result is not None and result.status == "SKIPPED"
        assert "quarantin" in result.reason.lower()
        assert broker.submitted == broker.cancelled == 0


def test_full_initialize_live_pending_ref_and_guard(tmp_path: Path) -> None:
    broker = _FakeStartupBroker()
    with _startup_environment(tmp_path / "live.db", broker) as (constructor, sessions):
        _seed_known(sessions, status="SUBMITTED", qty=Decimal("0"))
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        pending = runner._trade_svc.pending_order_by_broker_id("startup-1")
        assert pending is not None and pending.passive_owner_ref == "1:claim-r1:exec-r1"
        assert runner.risk.external_block() is not None
        assert runner._passive_recovery_hard_reasons == ()
        assert not runner.risk.resume_eligibility().approved
        with pytest.raises(ResumeBlockedError):
            runner.risk.resume()
        hooks = runner._trade_svc.passive_submit_hooks
        assert hooks is not None and hooks.current_gate_issue()
        with sessions() as db:
            row = db.get(PassiveMandate, 1)
            assert row is not None and row.submit_state == "ORDER_KNOWN"


@pytest.mark.parametrize("qty,positions,blocked", [(Decimal("0"), [], False), (None, [], True), (Decimal("0"), None, True)])
def test_full_initialize_terminal_no_fill_requires_explicit_facts(
    tmp_path: Path, qty: Decimal | None, positions: Any, blocked: bool,
) -> None:
    broker = _FakeStartupBroker()
    broker.status, broker.qty, broker.positions = "CANCELLED", qty, positions
    with _startup_environment(tmp_path / "terminal.db", broker) as (constructor, sessions):
        _seed_known(sessions, status="CANCELLED", qty=qty)
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        assert (runner.risk.external_block() is not None) == blocked


def test_full_manual_refresh_recovers_transient_but_not_sticky_uncertainty(tmp_path: Path) -> None:
    broker = _FakeStartupBroker()
    broker.status, broker.qty, broker.price = "FILLED", Decimal("8"), Decimal("600")
    broker.positions = [Position("SPY.US", "LONG", Decimal("8"), Decimal("600"), Decimal("8"))]
    broker.read_fault = True
    with _startup_environment(tmp_path / "refresh.db", broker) as (constructor, sessions):
        _seed_known(sessions, status="FILLED", qty=Decimal("8"), holding=True)
        runner = constructor()
        runner._initialize_runner()
        _assert_full_startup(runner, broker)
        assert runner.risk.external_block() is not None
        broker.read_fault = False
        runner._refresh_passive_before_resume_eligibility()
        assert runner.risk.external_block() is None
        assert runner.risk.pause_reason == "MANUAL: owner pause"
        assert runner._passive_quarantined_symbols == frozenset({"SPY.US"})
        with sessions() as db:
            row = db.get(PassiveMandate, 1)
            assert row is not None and row.submit_state == "ORDER_KNOWN"
            row.submit_state = "UNCERTAIN"
            db.commit()
        runner._passive_uncertainty_sink("persisted observation conflict", "startup-1")
        runner._refresh_passive_before_resume_eligibility()
        assert runner.risk.external_block() is not None
        assert runner.risk.pause_reason == "MANUAL: owner pause"
