from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, OrderRecord, TradeEvent
from app.services.daily_pnl_service import DailyPnlService
from app.core.broker import BrokerGateway, BrokerOrder
from app.runner import AppRunner
from app.services.external_order_acknowledgement_service import (
    ACK_EVENT, canonical_group, group_digest, append_ack, replay_exclusions,
)
from app.models import FillSettlement, TrackedEntry


IDENTITY = "a" * 64
BUY_AT = datetime(2026, 10, 8, 17, 29, 8, tzinfo=timezone.utc)
BUY_FILL = BUY_AT + timedelta(seconds=118)
SELL_AT = datetime(2026, 10, 9, 1, 40, 12, tzinfo=timezone.utc)
SELL_FILL = SELL_AT + timedelta(seconds=1)


def _leg(order_id: str, side: str, price: str, created: datetime, filled: datetime) -> BrokerOrder:
    return BrokerOrder(order_id, "NVDL.US", side, Decimal(200), Decimal(price),
                       Decimal(200), Decimal(price), "FILLED", created, filled)


def _facts(row: BrokerOrder) -> dict[str, object]:
    return {"broker_order_id": row.broker_order_id, "symbol": row.symbol,
            "quantity": row.quantity, "price": row.executed_price,
            "submitted_at": row.created_at, "filled_at": row.filled_at}


@pytest.fixture
def case(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    buy = _leg("1292901539857346560", "BUY", "37.16", BUY_AT, BUY_FILL)
    sell = _leg("1293025124420681728", "SELL", "37.60", SELL_AT, SELL_FILL)
    group = canonical_group(IDENTITY, _facts(buy), _facts(sell))
    runner = AppRunner()
    runner._broker_identity_fingerprint = IDENTITY
    runner._credential_parts_complete = True
    clock = [100.0]
    monkeypatch.setattr("app.runner.time.monotonic", lambda: clock[0])

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 9, 7, tzinfo=timezone.utc) + timedelta(seconds=clock[0] - 100)

    monkeypatch.setattr("app.runner.datetime", _FakeDateTime)

    @contextmanager
    def sessions():
        with Session(engine) as db:
            yield db

    runner._db_session = sessions
    issues = [f"broker live or terminal order id={sell.broker_order_id} lacks local submission provenance"]
    runner._unrepresentable_live_order_issues = issues
    runner.risk.pause("ORDER_RECONCILIATION_UNCERTAIN: " + issues[0])

    class _FakeBroker:
        def __init__(self):
            self.orders = [sell]
            self.positions = []

        def get_today_orders(self):
            return self.orders

        def get_positions(self):
            return self.positions

    class _FakeReader:
        def preview(self, **kwargs):
            evidence = []
            for row in (buy, sell):
                evidence.append(SimpleNamespace(order_id=row.broker_order_id, symbol=row.symbol,
                    side=row.side, submitted_quantity=row.quantity, submitted_price=row.price,
                    executed_quantity=row.executed_quantity, executed_price=row.executed_price,
                    submitted_at=row.created_at, first_executed_at=row.filled_at, last_executed_at=row.filled_at))
            return SimpleNamespace(proof=SimpleNamespace(broker_identity_fingerprint=IDENTITY), filled_orders=evidence)

    runner.broker = cast(BrokerGateway, _FakeBroker())
    monkeypatch.setattr("app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env", _FakeReader)
    request = dict(broker_identity_fingerprint=IDENTITY, buy=_facts(buy), sell=_facts(sell),
                   digest=group_digest(group), confirmation_reason="These completed orders were mine", actor_hash="owner")
    yield SimpleNamespace(engine=engine, runner=runner, buy=buy, sell=sell, group=group, clock=clock, request=request)
    engine.dispose()


def _record(db: Session, row: BrokerOrder, **extra) -> OrderRecord:
    record = OrderRecord(broker_order_id=row.broker_order_id, symbol=row.symbol, side=row.side,
                         quantity=float(row.quantity), price=float(row.price), executed_quantity=float(row.executed_quantity),
                         executed_price=float(row.executed_price), status=row.status, created_at=row.created_at,
                         filled_at=row.filled_at, **extra)
    db.add(record)
    return record


def _ack(case):
    first = case.runner.acknowledge_external_round_trip(**case.request)
    assert first["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    return case.runner.acknowledge_external_round_trip(**case.request)


def test_history_buy_today_sell_double_proof_keeps_pause_and_excludes_profit(case):
    with Session(case.engine) as db:
        _record(db, case.sell)
        db.commit()
        assert not DailyPnlService(db).calculate(trade_day=SELL_FILL.date()).is_complete
        _record(db, case.buy)
        db.commit()
        assert DailyPnlService(db).calculate(trade_day=SELL_FILL.date()).realized_pnl > 80
    pause = case.runner.risk.pause_verification_snapshot()
    assert _ack(case)["status"] == "ACKNOWLEDGED"
    assert case.runner.risk.paused
    assert case.runner.risk.pause_verification_snapshot() == pause
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 1
        assert DailyPnlService(db).calculate(trade_day=SELL_FILL.date()).realized_pnl == 0
        assert DailyPnlService(db).pair_round_trips_with_issues(include_excursions=False).trades == []
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ALREADY_ACKNOWLEDGED"


def test_missing_buy_row_is_not_imported_and_derived_replay_is_retained(case, monkeypatch):
    with Session(case.engine) as db:
        _record(db, case.sell, pnl_source="LEDGER_REPLAY", gross_pnl=88, net_pnl=80.524,
                cost_basis_price=37.16, cost_basis_quantity=200, position_quantity_before=200)
        db.commit()
    _ack(case)
    monkeypatch.setattr(case.runner, "_sync_risk_from_order_ledger", lambda: False)
    case.runner.sync_today_orders_from_broker(force=True)
    with Session(case.engine) as db:
        assert db.query(OrderRecord).count() == 1
        row = db.query(OrderRecord).one()
        assert row.pnl_source == "LEDGER_REPLAY" and row.net_pnl == 80.524
        assert DailyPnlService(db).refresh_execution_outcomes() == 0
        assert row.net_pnl == 80.524


@pytest.mark.parametrize("field,value", [("quantity", Decimal(199)), ("executed_price", Decimal("NaN")),
                                          ("status", "PARTIAL_FILLED"), ("symbol", "OTHER.US"),
                                          ("filled_at", SELL_FILL + timedelta(seconds=1)),
                                          ("broker_order_id", "different")])
def test_fresh_terminal_facts_must_match(case, field, value):
    setattr(case.sell, field, value)
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)


@pytest.mark.parametrize("event_type", ["ORDER_SUBMITTED"])
@pytest.mark.parametrize("leg", ["buy", "sell"])
def test_any_submitted_evidence_blocks_even_mismatched(case, event_type, leg):
    with Session(case.engine) as db:
        db.add(TradeEvent(event_type=event_type, broker_order_id=getattr(case, leg).broker_order_id,
                          symbol="WRONG.US", side="WRONG", payload_json="{}"))
        db.commit()
    with pytest.raises(ValueError, match="ORDER_SUBMITTED"):
        case.runner.acknowledge_external_round_trip(**case.request)


def test_manual_bot_inventory_cannot_be_acknowledged(case):
    with Session(case.engine) as db:
        earlier = _leg("bot-buy", "BUY", "40", BUY_AT - timedelta(hours=2), BUY_FILL - timedelta(hours=2))
        _record(db, earlier)
        db.commit()
    with pytest.raises(ValueError, match="overlaps"):
        case.runner.acknowledge_external_round_trip(**case.request)


def test_generation_change_restarts_double_proof(case):
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 6
    case.runner.risk.pause(case.runner.risk.pause_reason)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    with Session(case.engine) as db:
        assert db.query(TradeEvent).count() == 0


def test_fact_drift_fails_closed_after_ack(case):
    with Session(case.engine) as db:
        _record(db, case.buy)
        row = _record(db, case.sell)
        db.commit()
    _ack(case)
    with Session(case.engine) as db:
        row = db.query(OrderRecord).filter(OrderRecord.broker_order_id == case.sell.broker_order_id).one()
        assert row.executed_price is not None
        row.executed_price += 1
        db.commit()
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert not result.is_complete and result.realized_pnl == 0
        with pytest.raises(ValueError):
            DailyPnlService(db).pair_round_trips_with_issues()


def test_owner_profit_excluded_bot_loss_preserved(case):
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        bot_buy = _leg("bot-buy", "BUY", "40", SELL_FILL + timedelta(hours=1), SELL_FILL + timedelta(hours=1))
        bot_sell = _leg("bot-sell", "SELL", "39", SELL_FILL + timedelta(hours=2), SELL_FILL + timedelta(hours=2))
        _record(db, bot_buy)
        _record(db, bot_sell)
        db.commit()
    _ack(case)
    with Session(case.engine) as db:
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert result.is_complete and result.realized_pnl < -200
        assert [trade.broker_order_id for trade in result.trades] == ["bot-sell"]


def test_sync_only_exact_current_terminal(case, monkeypatch):
    _ack(case)
    monkeypatch.setattr(case.runner, "_sync_risk_from_order_ledger", lambda: False)
    case.runner.sync_today_orders_from_broker(force=True)
    assert case.runner._last_order_sync_succeeded
    assert case.runner._unrepresentable_live_order_issues == []
    assert case.runner.risk.paused
    case.sell.status = "PARTIAL_FILLED"
    case.runner.sync_today_orders_from_broker(force=True)
    assert not case.runner._last_order_sync_succeeded
    assert case.runner.risk.paused


@pytest.mark.parametrize("condition", ["kill", "pending", "tracked", "durable_tracked", "reduction",
                                       "postfill", "issue", "identity", "account", "nan_account"])
def test_quiescence_and_identity_required(case, monkeypatch, condition):
    if condition == "kill":
        case.runner.risk.enable_kill_switch("test")
    elif condition == "pending":
        monkeypatch.setattr(case.runner._trade_svc, "pending_order_ids", lambda: ["pending"])
        monkeypatch.setattr(type(case.runner._trade_svc), "has_pending_order", property(lambda _: True))
    elif condition == "tracked":
        case.runner._trade_svc.load_tracked_entries({"OTHER.US": (Decimal(1), Decimal(1))})
    elif condition == "durable_tracked":
        with Session(case.engine) as db:
            db.add(TrackedEntry(symbol="OTHER.US", side="LONG", quantity=1, cost=1))
            db.commit()
    elif condition == "reduction":
        case.runner._reduction_intents["OTHER.US"] = SimpleNamespace()
    elif condition == "postfill":
        case.runner._post_fill_expectations["OTHER.US"] = SimpleNamespace()
    elif condition == "issue":
        case.runner._unrepresentable_live_order_issues.append("unrelated")
    elif condition == "identity":
        case.runner._broker_identity_fingerprint = "b" * 64
    else:
        case.runner.broker.positions = [SimpleNamespace(quantity=Decimal("NaN" if condition == "nan_account" else "1"))]
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)


def test_settlement_receipt_blocks_ack(case):
    with Session(case.engine) as db:
        db.add(FillSettlement(broker_order_id=case.buy.broker_order_id, symbol="NVDL.US", action="BUY",
                              booked_quantity=200, booked_price=37.16, quantity_source="BROKER", price_source="BROKER",
                              tracked_quantity_after=200, tracked_cost_after=7432, first_terminal_status="FILLED"))
        db.commit()
    with pytest.raises(ValueError, match="settlement"):
        case.runner.acknowledge_external_round_trip(**case.request)


def test_database_failure_has_no_memory_exemption_and_requires_new_first_proof(case, monkeypatch):
    case.runner.acknowledge_external_round_trip(**case.request)
    case.clock[0] += 5
    original = Session.commit

    def failing_commit(db):
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(Session, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="database failure"):
        case.runner.acknowledge_external_round_trip(**case.request)
    monkeypatch.setattr(Session, "commit", original)
    with Session(case.engine) as db:
        assert db.query(TradeEvent).count() == 0
        assert replay_exclusions(db) == set()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"


def test_request_digest_nan_and_timestamps_rejected(case):
    case.request["digest"] = "b" * 64
    with pytest.raises(ValueError, match="digest"):
        case.runner.acknowledge_external_round_trip(**case.request)
    for field, value in (("quantity", "NaN"), ("price", "Infinity"), ("filled_at", "2026-10-08T17:31:06")):
        buy = {**case.request["buy"], field: value}
        with pytest.raises(ValueError):
            canonical_group(IDENTITY, buy, case.request["sell"])


def test_shared_external_position_label_manual_bot_loss_retained(case):
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        bot_exit = _leg("manual-close-bot", "SELL", "39", SELL_FILL + timedelta(hours=2), SELL_FILL + timedelta(hours=2))
        _record(db, bot_exit, pnl_source="TRACKED_ENTRY", cost_basis_price=40, cost_basis_quantity=200,
                position_quantity_before=200, gross_pnl=-200, net_pnl=-208, pnl_fee=8, pnl_fee_source="ESTIMATED")
        db.commit()
    _ack(case)
    with Session(case.engine) as db:
        trades = DailyPnlService(db).pair_round_trips_with_issues(include_excursions=False).trades
        assert len(trades) == 1 and trades[0].strategy_source == "EXTERNAL_POSITION"
        assert trades[0].net_pnl < 0


def test_normal_resume_still_requires_two_broker_proofs_and_risk_limits(case, monkeypatch):
    _ack(case)
    original_risk = (case.runner.risk.cumulative_realized_pnl, case.runner.risk.peak_realized_pnl)
    monkeypatch.setattr(case.runner, "_reconcile_tracked_entries_with_broker", lambda *args, **kwargs: [])
    monkeypatch.setattr(case.runner._state_svc, "persist", lambda *args, **kwargs: None)
    case.runner.risk._paused_at = SELL_FILL
    approved, error = case.runner.resume_after_verification()
    assert not approved and "second proof" in error
    case.clock[0] += 5
    approved, error = case.runner.resume_after_verification()
    assert approved, error
    assert (case.runner.risk.cumulative_realized_pnl, case.runner.risk.peak_realized_pnl) == original_risk
    case.runner.risk.pause("manual")
    case.runner.risk.daily_pnl = -1_000_000
    approved, error = case.runner.resume_after_verification()
    assert not approved and "daily loss" in error


def test_authenticated_post_returns_409_then_commits(case, monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import trade
    from app.config import settings

    class _FakeAudit:
        def record(self, *args, **kwargs):
            pass

    monkeypatch.setattr(trade, "get_runner", lambda: case.runner)
    monkeypatch.setattr(settings, "api_key", "owner-secret")
    app = FastAPI()
    app.include_router(trade.router)
    app.dependency_overrides[trade.get_audit_logger] = _FakeAudit
    request = {key: value for key, value in case.request.items() if key != "actor_hash"}
    request = json.loads(json.dumps(request, default=str))
    with TestClient(app) as client:
        assert client.post("/api/control/acknowledge-external-round-trip", json=request).status_code == 401
        headers = {"x-api-key": "owner-secret"}
        first = client.post("/api/control/acknowledge-external-round-trip", json=request, headers=headers)
        assert first.status_code == 409 and first.json()["detail"]["status"] == "PROOF_PENDING"
        case.clock[0] += 5
        second = client.post("/api/control/acknowledge-external-round-trip", json=request, headers=headers)
        assert second.status_code == 200 and second.json()["status"] == "ACKNOWLEDGED"


def test_process_restart_requires_new_first_proof_and_committed_retry_survives(case):
    case.runner.acknowledge_external_round_trip(**case.request)
    case.clock[0] += 5
    case.runner._external_ack_proof = None
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ACKNOWLEDGED"
    case.runner._external_ack_proof = None
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ALREADY_ACKNOWLEDGED"


def test_concurrent_exact_retry_writes_one_event(case):
    from concurrent.futures import ThreadPoolExecutor

    case.runner.acknowledge_external_round_trip(**case.request)
    case.clock[0] += 5
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.runner.acknowledge_external_round_trip(**case.request), range(2)))
    assert {result["status"] for result in results} == {"ACKNOWLEDGED", "ALREADY_ACKNOWLEDGED"}
    with Session(case.engine) as db:
        assert db.query(TradeEvent).count() == 1


def test_pause_generation_race_during_network_rejects(case, monkeypatch):
    original = case.runner.broker.get_positions

    def changed_positions():
        case.runner.risk.pause(case.runner.risk.pause_reason)
        return original()

    monkeypatch.setattr(case.runner.broker, "get_positions", changed_positions)
    with pytest.raises(ValueError, match="changed"):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert case.runner._external_ack_proof is None


def test_live_replay_identity_binding_and_strict_retry(case):
    _ack(case)
    with Session(case.engine) as db:
        assert not DailyPnlService(db).calculate(trade_day=SELL_FILL.date(), external_ack_identity="b" * 64).is_complete
        with pytest.raises(ValueError, match="identity"):
            DailyPnlService(db).pair_round_trips_with_issues(external_ack_identity="b" * 64)
    with pytest.raises(ValueError, match="retry"):
        case.runner.acknowledge_external_round_trip(**{**case.request, "confirmation_reason": "changed"})


def test_corrupt_external_ack_never_credits_owner_profit() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    at = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
    with Session(engine) as db:
        for side, price in (("BUY", 37.16), ("SELL", 37.60)):
            db.add(OrderRecord(broker_order_id=side, symbol="NVDL.US", side=side,
                               quantity=200, price=price, executed_quantity=200,
                               executed_price=price, status="FILLED", created_at=at,
                               filled_at=at))
        db.add(TradeEvent(event_type="EXTERNAL_ROUND_TRIP_ACKNOWLEDGED",
                          broker_order_id="SELL", symbol="NVDL.US",
                          payload_json="broken", source_event_key="a" * 64))
        db.commit()
        result = DailyPnlService(db).calculate(trade_day=at.date())
        assert not result.is_complete
        assert result.realized_pnl == 0
    engine.dispose()
