"""Safety-lane RED tests for the external-order-acknowledgement release blockers.

Scope (oracle53, tests-only lane): these tests pin the *expected good
rejection* behaviour of the current acknowledgement patch.  Several are
intentionally RED against the current implementation and must stay in the tree
until the production fix (fix52) lands.  Never delete or weaken a failing test
in this module to green the lane -- fix the service instead.

Blockers covered
----------------
1. ``assert_isolated`` raw-FIFO accounting silently drops negative executed
   quantities and clamps unmatched SELL / BUY_TO_COVER inventory at zero
   (``max(Decimal(0), ...)``), so a poisoned ledger still acknowledges.
2. ``assert_unowned`` only recognises ORDER_SUBMITTED / FillSettlement /
   ``pnl_source`` / ``config_version`` provenance and misses the local-only
   submission provenance actually persisted by the trade-execution/runner
   path: ``OrderRecord.submit_started_at`` (TES submit start),
   ``OrderRecord.decision_at`` (runner ledger metadata) and
   ``OrderRecord.raw_response`` (serialized submission/execution context).
   Broker-synced timestamps (``broker_submitted_at`` / ``broker_updated_at``,
   both copied from the broker ``OrderResult``) and derived fee /
   ``LEDGER_REPLAY`` facts must keep being allowed.
3. ``AppRunner.acknowledge_external_round_trip`` reads positions/today before
   the historical reader, never re-verifies the broker snapshot after a slow
   history read, and the first proof has no TTL: a first proof of any age is
   accepted and a broker-state change during the history read is invisible to
   the final commit.  Durations below are chosen far beyond the oracle's 60s /
   300s bounds so the assertions never depend on the exact constant a future
   fix picks.
4. The runner must prove the pair through the *real*
   ``LongportHistoricalCompletenessReader`` over a fake official HTTP
   transport (``completed_round_trip=True`` window), never a stubbed reader.

Every broker/history interaction here runs on the fake monotonic clock and a
fake transport: no real sleeping (no lock-held sleeps), no network and no
broker SDK import (conftest blocks ``import longport``).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, OrderRecord, TradeEvent
from app.runner import AppRunner
from app.core.broker import BrokerGateway, BrokerOrder
from app.services.daily_pnl_service import DailyPnlService
from app.services.external_order_acknowledgement_service import (
    ACK_EVENT,
    assert_isolated,
    assert_unowned,
    canonical_group,
    group_digest,
)
from app.services import historical_order_completeness_reader as reader_module
from app.services.historical_order_completeness_reader import (
    LongportHistoricalCompletenessReader,
)

# Reuse the sibling module's exact fixture contract (no edits there).
from tests.test_external_order_acknowledgement import (
    BUY_AT,
    BUY_FILL,
    IDENTITY,
    SELL_AT,
    SELL_FILL,
    _facts,
    _leg,
    _record,
)


# ---------------------------------------------------------------------------
# Blocker 1: assert_isolated must not clamp or drop raw FIFO inventory.
# ---------------------------------------------------------------------------

_POISON_BLOCKERS = (
    "negative_quantity",
    "orphan_sell",
    "oversized_sell",
    "oversized_buy_to_cover",
)


def _add_poison_rows(db: Session, blocker: str) -> None:
    """Raw-FIFO ledgers that a clamping / negative-blind implementation accepts."""
    def row(order_id: str, side: str, quantity: float, executed: float, filled: datetime) -> OrderRecord:
        record = OrderRecord(broker_order_id=order_id, symbol="NVDL.US", side=side,
                             quantity=quantity, price=30.0, executed_quantity=executed,
                             executed_price=30.0, status="FILLED",
                             created_at=filled - timedelta(hours=1), filled_at=filled)
        db.add(record)
        return record

    if blocker == "negative_quantity":
        # Negative executed quantity is silently skipped (quantity > 0 gate).
        row("neg-sell", "SELL", 200.0, -200.0, BUY_AT - timedelta(hours=3))
    elif blocker == "orphan_sell":
        # Unmatched SELL before the external BUY is clamped to zero inventory.
        row("orphan-sell", "SELL", 200.0, 200.0, BUY_AT - timedelta(hours=3))
    elif blocker == "oversized_sell":
        # SELL 300 against a prior BUY 200 is clamped: the -100 remainder vanishes.
        row("ovs-bot-buy", "BUY", 200.0, 200.0, BUY_AT - timedelta(hours=3))
        row("ovs-bot-sell", "SELL", 300.0, 300.0, BUY_AT - timedelta(hours=2))
    elif blocker == "oversized_buy_to_cover":
        # BUY_TO_COVER 200 against a prior SELL_SHORT 100 is clamped: the
        # residual long 100 vanishes before the external pair.
        row("ovc-bot-short", "SELL_SHORT", 100.0, 100.0, BUY_AT - timedelta(hours=3))
        row("ovc-bot-cover", "BUY_TO_COVER", 200.0, 200.0, BUY_AT - timedelta(hours=2))
    else:  # pragma: no cover - guard for typos in parametrization
        raise AssertionError(f"unknown blocker {blocker}")


@pytest.fixture
def ledger():
    """Service-level fixture: an isolated engine holding only the exact pair."""
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    buy = _leg("1292901539857346560", "BUY", "37.16", BUY_AT, BUY_FILL)
    sell = _leg("1293025124420681728", "SELL", "37.60", SELL_AT, SELL_FILL)
    group = canonical_group(IDENTITY, _facts(buy), _facts(sell))
    with Session(engine) as db:
        _record(db, buy)
        _record(db, sell)
        db.commit()
        yield SimpleNamespace(engine=engine, db=db, buy=buy, sell=sell, group=group)
    engine.dispose()


@pytest.mark.parametrize("blocker", _POISON_BLOCKERS)
def test_assert_isolated_rejects_poisoned_raw_fifo_ledger(ledger, blocker):
    _add_poison_rows(ledger.db, blocker)
    ledger.db.commit()
    with pytest.raises(ValueError):
        assert_isolated(ledger.db, ledger.group)


def test_assert_isolated_clean_pair_still_allowed(ledger):
    """Control (GREEN, must stay): the exact pair alone stays acknowledgeable."""
    assert assert_isolated(ledger.db, ledger.group) is None


# ---------------------------------------------------------------------------
# Blocker 2: assert_unowned must see local-only submission provenance.
# ---------------------------------------------------------------------------

_LOCAL_ONLY_EVIDENCE = (
    ("submit_started_at", BUY_AT),
    ("decision_at", BUY_AT - timedelta(seconds=30)),
    ("raw_response", '{"broker_response": {"order_id": "1292901539857346560"}}'),
)


@pytest.mark.parametrize("field,value", _LOCAL_ONLY_EVIDENCE)
@pytest.mark.parametrize("leg", ["buy", "sell"])
def test_assert_unowned_rejects_local_only_submission_provenance(ledger, field, value, leg):
    row = ledger.db.query(OrderRecord).filter(
        OrderRecord.broker_order_id == getattr(ledger, leg).broker_order_id).one()
    setattr(row, field, value)
    ledger.db.commit()
    with pytest.raises(ValueError):
        assert_unowned(ledger.db, ledger.group)


@pytest.mark.parametrize("field,value", [
    ("broker_submitted_at", BUY_AT),
    ("broker_updated_at", SELL_FILL),
    ("estimated_fee", 1.25),
    ("actual_fee", 1.25),
    ("fee_source", "ACTUAL"),
    ("pnl_source", "LEDGER_REPLAY"),
])
def test_assert_unowned_allows_broker_synced_and_derived_facts(ledger, field, value):
    """Control (GREEN, must stay): broker-synced / fee / LEDGER_REPLAY evidence
    is not local submission provenance and must not block the acknowledgement."""
    row = ledger.db.query(OrderRecord).filter(
        OrderRecord.broker_order_id == ledger.sell.broker_order_id).one()
    setattr(row, field, value)
    ledger.db.commit()
    assert assert_unowned(ledger.db, ledger.group) is None


@pytest.mark.parametrize("field,value", _LOCAL_ONLY_EVIDENCE)
def test_runner_acknowledge_rejects_local_only_submission_provenance(case, field, value):
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        db.commit()
        row = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == case.sell.broker_order_id).one()
        setattr(row, field, value)
        db.commit()
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert case.runner._external_ack_proof is None
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 0


# ---------------------------------------------------------------------------
# Runner fixture: fake clock + uncached fake broker + scripted reader hooks.
# ---------------------------------------------------------------------------

@pytest.fixture
def case(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
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
        """Uncached hook: every call returns a fresh copy of current state."""

        def __init__(self):
            self.orders = [sell]
            self.positions = []

        def get_today_orders(self):
            return list(self.orders)

        def get_positions(self):
            return list(self.positions)

    broker = _FakeBroker()
    runner.broker = cast(BrokerGateway, broker)

    reader_calls: list[dict] = []
    reader_hooks: list = []

    def _reader_factory():
        def preview(**kwargs):
            reader_calls.append(dict(kwargs))
            if reader_hooks:
                reader_hooks.pop(0)()
            evidence = []
            for row in (buy, sell):
                evidence.append(SimpleNamespace(order_id=row.broker_order_id, symbol=row.symbol,
                    side=row.side, submitted_quantity=row.quantity, submitted_price=row.price,
                    executed_quantity=row.executed_quantity, executed_price=row.executed_price,
                    submitted_at=row.created_at, first_executed_at=row.filled_at,
                    last_executed_at=row.filled_at))
            return SimpleNamespace(proof=SimpleNamespace(broker_identity_fingerprint=IDENTITY),
                                    filled_orders=evidence)
        return SimpleNamespace(preview=preview)

    monkeypatch.setattr(
        "app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env",
        _reader_factory)
    request = dict(broker_identity_fingerprint=IDENTITY, buy=_facts(buy), sell=_facts(sell),
                   digest=group_digest(group), confirmation_reason="These completed orders were mine",
                   actor_hash="owner")
    yield SimpleNamespace(engine=engine, runner=runner, broker=broker, buy=buy, sell=sell,
                          group=group, clock=clock, request=request, reader_calls=reader_calls,
                          reader_hooks=reader_hooks)
    engine.dispose()


def _pair_rows(case) -> None:
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        db.commit()


def _attempt(case) -> dict:
    """Call the acknowledgement; a clean rejection maps to a non-ACK status."""
    try:
        return case.runner.acknowledge_external_round_trip(**case.request)
    except (ValueError, RuntimeError) as exc:
        return {"status": "REJECTED", "error": repr(exc)}


def _safety_state(case) -> tuple:
    with Session(case.engine) as db:
        events = db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count()
    return (events, case.runner.risk.paused,
            case.runner.risk.cumulative_realized_pnl, case.runner.risk.peak_realized_pnl)


# ---------------------------------------------------------------------------
# Blocker 1 (runner path): poisoned raw FIFO must reject with no event.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blocker", _POISON_BLOCKERS)
def test_runner_acknowledge_rejects_poisoned_raw_fifo_ledger(case, blocker):
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        _add_poison_rows(db, blocker)
        db.commit()
    with pytest.raises(ValueError):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert case.runner._external_ack_proof is None
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 0


@pytest.mark.parametrize("sell_source", ["UNKNOWN", "LEDGER_REPLAY"])
def test_runner_acknowledge_existing_ledger_replay_flow_stays_allowed(case, sell_source):
    """Control (GREEN, must stay): the existing LEDGER_REPLAY acknowledgement
    flow (sibling test_missing_buy_row...) keeps working after the fix."""
    extra = {} if sell_source == "UNKNOWN" else dict(
        pnl_source="LEDGER_REPLAY", gross_pnl=88, net_pnl=80.524,
        cost_basis_price=37.16, cost_basis_quantity=200, position_quantity_before=200)
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell, **extra)
        db.commit()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ACKNOWLEDGED"
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 1


def test_runner_acknowledge_manual_bot_loss_is_not_excluded(case):
    """Control (GREEN, must stay): a manual bot SELL loss recorded after the
    external pair neither blocks the ack nor gets excluded from owner PnL."""
    with Session(case.engine) as db:
        _record(db, case.buy)
        _record(db, case.sell)
        bot_buy = _leg("bot-buy", "BUY", "40", SELL_FILL + timedelta(hours=1),
                       SELL_FILL + timedelta(hours=1))
        bot_sell = _leg("bot-sell", "SELL", "39", SELL_FILL + timedelta(hours=2),
                        SELL_FILL + timedelta(hours=2))
        _record(db, bot_buy)
        _record(db, bot_sell)
        db.commit()
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ACKNOWLEDGED"
    with Session(case.engine) as db:
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert result.is_complete and result.realized_pnl < -200
        assert [trade.broker_order_id for trade in result.trades] == ["bot-sell"]


# ---------------------------------------------------------------------------
# Blocker 3: stale proofs, slow history reads and mid-read state changes.
# ---------------------------------------------------------------------------

def test_control_fast_double_proof_still_acknowledges(case):
    """Control (GREEN, must stay): the normal 5s double proof still acks and
    the runner asks the reader for a completed_round_trip window."""
    _pair_rows(case)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ACKNOWLEDGED"
    assert case.reader_calls[0]["completed_round_trip"] is True
    assert case.reader_calls[0]["symbol"] == "NVDL.US"
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 1


def test_first_proof_expires_and_cannot_acknowledge_stale_proof(case):
    """A first proof that is a full day old (far beyond the oracle's 300s bound)
    must not be accepted; a fresh separated observation is required again."""
    _pair_rows(case)
    before = _safety_state(case)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 86_400
    outcome = _attempt(case)
    assert outcome.get("status") != "ACKNOWLEDGED", outcome
    assert _safety_state(case) == before
    # Either proof-restart or hard-reject designs must force a new first proof.
    assert _attempt(case).get("status") != "ACKNOWLEDGED"
    case.clock[0] += 5
    assert _attempt(case)["status"] == "ACKNOWLEDGED"


def test_slow_history_read_rejects_acknowledgement_and_restarts_proof(case):
    """A history read taking an hour (far beyond the oracle's 60s bound) must
    trigger a refreshed final broker snapshot and a restarted first proof."""
    _pair_rows(case)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    before = _safety_state(case)

    def slow_history() -> None:
        case.clock[0] += 3_600

    case.reader_hooks.append(slow_history)
    outcome = _attempt(case)
    assert outcome.get("status") != "ACKNOWLEDGED", outcome
    assert _safety_state(case) == before
    # The proof restarts: at least one fresh separated observation is required.
    assert _attempt(case).get("status") != "ACKNOWLEDGED"
    case.clock[0] += 5
    assert _attempt(case)["status"] == "ACKNOWLEDGED"


@pytest.mark.parametrize("mutation", ["live_order", "negative_position"])
def test_broker_state_change_during_history_rejects_second_proof(case, mutation):
    """A live order or negative position appearing while history is being read
    must reject the second proof: the final snapshot is stale otherwise."""
    _pair_rows(case)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    before = _safety_state(case)

    def mutate() -> None:
        if mutation == "live_order":
            case.broker.orders.append(BrokerOrder("live-during-history", "NVDL.US", "BUY",
                                                  Decimal(200), Decimal("37.50"), Decimal(0),
                                                  Decimal(0), "SUBMITTED", SELL_AT, None))
        else:
            case.broker.positions.append(SimpleNamespace(quantity=Decimal(-5)))

    case.reader_hooks.append(mutate)
    outcome = _attempt(case)
    assert outcome.get("status") != "ACKNOWLEDGED", outcome
    assert _safety_state(case) == before
    # While the dangerous broker state persists, no later attempt acks either.
    case.clock[0] += 5
    assert _attempt(case).get("status") != "ACKNOWLEDGED"
    assert _safety_state(case) == before


# ---------------------------------------------------------------------------
# Blocker 4: the real LongportHistoricalCompletenessReader over fake HTTP.
# ---------------------------------------------------------------------------

class _ReplayTransport:
    """Fake official authenticated HTTP transport (no network, no SDK)."""

    def __init__(self, orders_payload: dict, executions_payload: dict) -> None:
        self._responses = [orders_payload, executions_payload]
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str) -> object:
        self.calls.append((method, path))
        response = self._responses.pop(0)
        self._responses.append(response)
        return response


def _http_order(order_id: str, side: str, quantity: str, price: str, submitted_at: str) -> dict:
    return {"order_id": order_id, "symbol": "NVDL.US", "side": side, "status": "FilledStatus",
            "quantity": quantity, "price": price, "executed_quantity": quantity,
            "executed_price": price, "submitted_at": submitted_at, "updated_at": submitted_at,
            "currency": "USD"}


def _http_execution(order_id: str, trade_id: str, quantity: str, price: str,
                    trade_done_at: str) -> dict:
    return {"order_id": order_id, "trade_id": trade_id, "symbol": "NVDL.US",
            "quantity": quantity, "price": price, "trade_done_at": trade_done_at}


def _pair_payloads() -> tuple[dict, dict]:
    orders = {"has_more": False, "orders": [
        _http_order("1292901539857346560", "Buy", "200", "37.16", str(int(BUY_AT.timestamp()))),
        _http_order("1293025124420681728", "Sell", "200", "37.60", str(int(SELL_AT.timestamp()))),
    ]}
    executions = {"has_more": False, "trades": [
        _http_execution("1292901539857346560", "buy-trade-1", "200", "37.16",
                        str(int(BUY_FILL.timestamp()))),
        _http_execution("1293025124420681728", "sell-trade-1", "200", "37.60",
                        str(int(SELL_FILL.timestamp()))),
    ]}
    return orders, executions


def _wire_real_reader(case, monkeypatch, orders: dict, executions: dict, *,
                      identity: str = IDENTITY) -> _ReplayTransport:
    transport = _ReplayTransport(orders, executions)
    monkeypatch.setattr(
        "app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env",
        lambda: LongportHistoricalCompletenessReader(transport,
                                                     broker_identity_fingerprint=identity))
    return transport


def _reject(case) -> None:
    with pytest.raises((ValueError, RuntimeError)):
        case.runner.acknowledge_external_round_trip(**case.request)
    assert case.runner._external_ack_proof is None
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 0


def test_real_reader_proves_completed_round_trip_pair(case, monkeypatch):
    """The real reader path: today's completed pair returns facts and the HTTP
    window/query matches the requested round trip exactly."""
    _pair_rows(case)
    orders, executions = _pair_payloads()
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert case.runner.acknowledge_external_round_trip(**case.request)["status"] == "ACKNOWLEDGED"
    query = urlencode({"symbol": "NVDL.US", "start_at": int(BUY_AT.timestamp()),
                       "end_at": int(SELL_FILL.timestamp())})
    assert ("get", f"/v1/trade/order/history?{query}") in transport.calls
    assert ("get", f"/v1/trade/execution/history?{query}") in transport.calls
    with Session(case.engine) as db:
        assert db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).count() == 1


def test_real_reader_third_filled_order_rejects(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _pair_payloads()
    extra_at = str(int((BUY_AT + timedelta(minutes=30)).timestamp()))
    orders["orders"].append(_http_order("other-777", "Buy", "200", "37.00", extra_at))
    executions["trades"].append(_http_execution("other-777", "other-trade-1", "200", "37.00", extra_at))
    _wire_real_reader(case, monkeypatch, orders, executions)
    _reject(case)


def test_real_reader_truncated_page_rejects(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _pair_payloads()
    orders["has_more"] = True
    _wire_real_reader(case, monkeypatch, orders, executions)
    _reject(case)


def test_real_reader_multi_timestamp_executions_reject(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _pair_payloads()
    split_at = str(int((BUY_FILL + timedelta(seconds=1)).timestamp()))
    executions["trades"] = [
        _http_execution("1292901539857346560", "buy-trade-1a", "100", "37.16",
                        str(int(BUY_FILL.timestamp()))),
        _http_execution("1292901539857346560", "buy-trade-1b", "100", "37.16", split_at),
        _http_execution("1293025124420681728", "sell-trade-1", "200", "37.60",
                        str(int(SELL_FILL.timestamp()))),
    ]
    _wire_real_reader(case, monkeypatch, orders, executions)
    _reject(case)


def test_real_reader_quantity_mismatch_rejects(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _pair_payloads()
    orders["orders"][0] = _http_order("1292901539857346560", "Buy", "199", "37.16",
                                      str(int(BUY_AT.timestamp())))
    executions["trades"][0] = _http_execution("1292901539857346560", "buy-trade-1", "199",
                                              "37.16", str(int(BUY_FILL.timestamp())))
    _wire_real_reader(case, monkeypatch, orders, executions)
    _reject(case)


def test_real_reader_wrong_identity_rejects(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _pair_payloads()
    _wire_real_reader(case, monkeypatch, orders, executions, identity="b" * 64)
    _reject(case)


def test_real_reader_is_used_not_a_fake_reader(case, monkeypatch):
    """The acknowledgement must fail closed when the official transport fails,
    proving the real reader (not an equity-skipping stub) is on the path."""
    _pair_rows(case)

    class _FailingTransport:
        def request(self, method: str, path: str) -> object:
            raise reader_module.HistoricalTransportError("injected transport failure")

    monkeypatch.setattr(
        "app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env",
        lambda: LongportHistoricalCompletenessReader(_FailingTransport(),
                                                     broker_identity_fingerprint=IDENTITY))
    _reject(case)
