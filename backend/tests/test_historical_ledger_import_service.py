from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import database
from app.models import Base, OrderRecord, TradeEvent
from app.services.daily_pnl_service import DailyPnlService, PnlReplayIssueCode
from app.services.historical_ledger_import_service import (
    HistoricalLedgerAuthorizationError,
    HistoricalLedgerConflictError,
    HistoricalLedgerImportError,
    HistoricalLedgerImportService,
    HistoricalLedgerReplayError,
)
from app.services.historical_order_completeness_reader import (
    HistoricalCompletenessError,
    HistoricalOrderPreview,
    LongportHistoricalCompletenessReader,
)


_FINGERPRINT_A = "a" * 64
_FINGERPRINT_B = "b" * 64
_START = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
_END = datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc)
_OBSERVED = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


class _FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)

    def request(self, method: str, path: str) -> object:
        del method, path
        if not self.responses:
            raise AssertionError("unexpected historical transport request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _SequenceReader:
    def __init__(self, *snapshots: HistoricalOrderPreview) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    def preview(self, **_kwargs: object) -> HistoricalOrderPreview:
        self.calls += 1
        if not self.snapshots:
            raise AssertionError("fresh historical preview was not supplied")
        return self.snapshots.pop(0)


def _epoch(hour: int, minute: int, second: int = 0, *, day: int = 21) -> str:
    return str(int(datetime(
        2026,
        5,
        day,
        hour,
        minute,
        second,
        tzinfo=timezone.utc,
    ).timestamp()))


def _order(
    order_id: str,
    side: str,
    quantity: str,
    price: str,
    submitted_at: str,
    updated_at: str,
    *,
    charge: tuple[str, str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "order_id": order_id,
        "symbol": "NVDA.US",
        "side": side,
        "status": "FilledStatus",
        "quantity": quantity,
        "price": price,
        "executed_quantity": quantity,
        "executed_price": price,
        "submitted_at": submitted_at,
        "updated_at": updated_at,
        "currency": "USD",
    }
    if charge is not None:
        value["charge_detail"] = {
            "total_amount": charge[0],
            "currency": charge[1],
        }
    return value


def _execution(
    order_id: str,
    trade_id: str,
    quantity: str,
    price: str,
    done_at: str,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "symbol": "NVDA.US",
        "quantity": quantity,
        "price": price,
        "trade_done_at": done_at,
    }


def _target_payloads() -> tuple[dict[str, object], dict[str, object]]:
    orders = [
        _order(
            "buy-113",
            "Buy",
            "113",
            "220.80",
            _epoch(14, 0),
            _epoch(14, 0, 9),
            charge=("1.25", "usd"),
        ),
        _order(
            "buy-11-first",
            "Buy",
            "11",
            "220.61",
            _epoch(14, 5),
            _epoch(14, 5, 1),
        ),
        _order(
            "buy-1",
            "Buy",
            "1",
            "220.66",
            _epoch(14, 6),
            _epoch(14, 6, 1),
        ),
        _order(
            "sell-125",
            "Sell",
            "125",
            "220.09",
            _epoch(15, 0),
            _epoch(15, 0, 8),
        ),
        _order(
            "buy-111",
            "Buy",
            "111",
            "219.94",
            _epoch(18, 22),
            _epoch(18, 22, 1),
        ),
        _order(
            "buy-11-second",
            "Buy",
            "11",
            "219.90",
            _epoch(18, 34, 12),
            _epoch(18, 34, 13),
        ),
        _order(
            "sell-104",
            "Sell",
            "104",
            "219.613",
            _epoch(19, 45, 40),
            _epoch(19, 45, 41),
        ),
    ]
    executions = [
        _execution("buy-113", "trade-buy-100", "100", "220.80", _epoch(14, 0, 1)),
        _execution("buy-113", "trade-buy-13", "13", "220.80", _epoch(14, 0, 9)),
        _execution("buy-11-first", "trade-buy-11-first", "11", "220.61", _epoch(14, 5, 1)),
        _execution("buy-1", "trade-buy-1", "1", "220.66", _epoch(14, 6, 1)),
        _execution("sell-125", "trade-sell-100", "100", "220.09", _epoch(15, 0, 1)),
        _execution("sell-125", "trade-sell-25", "25", "220.09", _epoch(15, 0, 8)),
        _execution("buy-111", "trade-buy-111", "111", "219.94", _epoch(18, 22, 1)),
        _execution("buy-11-second", "trade-buy-11-second", "11", "219.90", _epoch(18, 34, 13)),
        _execution("sell-104", "trade-sell-104", "104", "219.613", _epoch(19, 45, 41)),
    ]
    return (
        {"has_more": False, "orders": orders},
        {"has_more": False, "trades": executions},
    )


def _snapshot(
    *,
    fingerprint: str = _FINGERPRINT_A,
    payloads: tuple[dict[str, object], dict[str, object]] | None = None,
) -> HistoricalOrderPreview:
    orders_payload, executions_payload = payloads or _target_payloads()
    return LongportHistoricalCompletenessReader(
        _FakeTransport(orders_payload, executions_payload),
        broker_identity_fingerprint=fingerprint,
    ).preview(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )


def _factory(tmp_path) -> tuple[object, sessionmaker]:
    engine = create_engine(f"sqlite:///{tmp_path / 'historical-ledger.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )


def _seed_final_exit(factory: sessionmaker) -> None:
    with factory.begin() as db:
        db.add(OrderRecord(
            broker_order_id="sell-final-18",
            symbol="NVDA.US",
            side="SELL",
            quantity=18,
            price=221.11,
            executed_quantity=18,
            executed_price=221.11,
            status="FILLED",
            created_at=datetime(2026, 5, 22, 13, 25, 11, tzinfo=timezone.utc),
            filled_at=datetime(2026, 5, 22, 13, 25, 13, tzinfo=timezone.utc),
        ))


def _issues(factory: sessionmaker):
    with factory() as db:
        return DailyPnlService(db).pair_round_trips_with_issues(
            symbol="NVDA.US",
            include_excursions=False,
        ).issues


def test_target_chain_apply_resolves_full_unmatched_and_reapply_is_zero_write(
    tmp_path,
) -> None:
    _engine, factory = _factory(tmp_path)
    _seed_final_exit(factory)
    before = _issues(factory)
    assert len(before) == 1
    assert before[0].issue_code is PnlReplayIssueCode.FULL_UNMATCHED_EXIT

    preview_snapshot = _snapshot()
    apply_snapshot = _snapshot()
    replay_snapshot = _snapshot()
    reader = _SequenceReader(
        preview_snapshot,
        apply_snapshot,
        replay_snapshot,
    )
    service = HistoricalLedgerImportService(factory, reader)

    plan = service.preview(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )

    assert plan.can_apply is True
    assert len(plan.pending_order_ids) == 7
    assert len(plan.pending_execution_trade_ids) == 9
    with factory() as db:
        assert db.query(OrderRecord).count() == 1
        assert db.query(TradeEvent).count() == 0

    applied = service.apply(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        expected_preview_digest=plan.proof.preview_digest,
        expected_broker_identity_fingerprint=(
            plan.proof.broker_identity_fingerprint
        ),
        observed_at=_OBSERVED,
    )

    assert len(applied.inserted_order_ids) == 7
    assert len(applied.inserted_execution_trade_ids) == 9
    assert applied.replay_issue_count_before == 1
    assert applied.replay_issue_count_after == 0
    assert _issues(factory) == []
    with factory() as db:
        assert db.query(OrderRecord).count() == 8
        assert db.query(TradeEvent).filter(
            TradeEvent.event_type == "HISTORICAL_EXECUTION_IMPORTED"
        ).count() == 9
        charged = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "buy-113"
        ).one()
        assert charged.actual_fee == pytest.approx(1.25)
        assert charged.fee_currency == "USD"
        assert charged.fee_source == "ACTUAL"
        # The aggregate FILLED row uses the last of its two executions.
        assert charged.filled_at == datetime(2026, 5, 21, 14, 0, 9)
        assert charged.cost_basis_price is None
        assert charged.cost_basis_quantity is None
        assert charged.position_quantity_before is None
        assert '"broker_executions"' in str(charged.raw_response)
        uncharged = db.query(OrderRecord).filter(
            OrderRecord.broker_order_id == "buy-111"
        ).one()
        assert uncharged.actual_fee is None
        assert uncharged.fee_source == "UNKNOWN"
        assert uncharged.fee_currency == ""
        event = db.query(TradeEvent).filter(
            TradeEvent.broker_order_id == "buy-113"
        ).order_by(TradeEvent.created_at).first()
        assert event is not None
        assert len(event.source_event_key) == 64
        assert '"broker_execution"' in event.payload_json
        assert _FINGERPRINT_A in event.payload_json

    repeated = service.apply(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        expected_preview_digest=plan.proof.preview_digest,
        expected_broker_identity_fingerprint=_FINGERPRINT_A,
        observed_at=_OBSERVED,
    )

    assert repeated.inserted_order_ids == ()
    assert repeated.inserted_execution_trade_ids == ()
    assert len(repeated.skipped_order_ids) == 7
    assert len(repeated.skipped_execution_trade_ids) == 9
    with factory() as db:
        assert db.query(OrderRecord).count() == 8
        assert db.query(TradeEvent).count() == 9


def test_apply_refetches_and_fails_closed_when_account_changes(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    first = _snapshot(fingerprint=_FINGERPRINT_A)
    second = _snapshot(fingerprint=_FINGERPRINT_B)
    reader = _SequenceReader(first, second)
    service = HistoricalLedgerImportService(factory, reader)
    plan = service.preview(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )

    with pytest.raises(
        HistoricalLedgerAuthorizationError,
        match="fingerprint",
    ):
        service.apply(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            expected_preview_digest=plan.proof.preview_digest,
            expected_broker_identity_fingerprint=_FINGERPRINT_A,
            observed_at=_OBSERVED,
        )

    assert reader.calls == 2
    with factory() as db:
        assert db.query(OrderRecord).count() == 0
        assert db.query(TradeEvent).count() == 0


def test_apply_refetches_and_rejects_stale_digest(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    old_snapshot = _snapshot()
    changed_orders, changed_executions = _target_payloads()
    changed_orders = copy.deepcopy(changed_orders)
    raw_orders = changed_orders["orders"]
    assert isinstance(raw_orders, list)
    first_order = raw_orders[0]
    assert isinstance(first_order, dict)
    first_order["currency"] = "US_DOLLAR"
    new_snapshot = _snapshot(payloads=(changed_orders, changed_executions))
    service = HistoricalLedgerImportService(
        factory,
        _SequenceReader(old_snapshot, new_snapshot),
    )
    plan = service.preview(
        symbol="NVDA.US",
        start_at=_START,
        end_at=_END,
        observed_at=_OBSERVED,
    )

    with pytest.raises(
        HistoricalLedgerAuthorizationError,
        match="preview digest",
    ):
        service.apply(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            expected_preview_digest=plan.proof.preview_digest,
            expected_broker_identity_fingerprint=_FINGERPRINT_A,
            observed_at=_OBSERVED,
        )

    with factory() as db:
        assert db.query(OrderRecord).count() == 0


def test_existing_broker_order_conflict_rolls_back_whole_import(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    snapshot = _snapshot()
    first = snapshot.filled_orders[0]
    with factory.begin() as db:
        db.add(OrderRecord(
            broker_order_id=first.order_id,
            symbol=first.symbol,
            side=first.side,
            quantity=float(first.submitted_quantity),
            price=999.0,
            executed_quantity=float(first.executed_quantity),
            executed_price=float(first.executed_price),
            status="FILLED",
            created_at=first.submitted_at,
            filled_at=first.last_executed_at,
            broker_submitted_at=first.submitted_at,
            broker_updated_at=first.updated_at,
        ))
    service = HistoricalLedgerImportService(factory, _SequenceReader(snapshot))

    with pytest.raises(HistoricalLedgerConflictError, match="price"):
        service.apply(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            expected_preview_digest=snapshot.proof.preview_digest,
            expected_broker_identity_fingerprint=_FINGERPRINT_A,
            observed_at=_OBSERVED,
        )

    with factory() as db:
        assert db.query(OrderRecord).count() == 1
        assert db.query(TradeEvent).count() == 0


def test_existing_order_missing_broker_timestamps_is_not_identical(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    snapshot = _snapshot()
    first = snapshot.filled_orders[0]
    with factory.begin() as db:
        db.add(OrderRecord(
            broker_order_id=first.order_id,
            symbol=first.symbol,
            side=first.side,
            quantity=float(first.submitted_quantity),
            price=float(first.submitted_price or first.executed_price),
            executed_quantity=float(first.executed_quantity),
            executed_price=float(first.executed_price),
            status="FILLED",
            created_at=first.submitted_at,
            filled_at=first.last_executed_at,
        ))

    service = HistoricalLedgerImportService(factory, _SequenceReader(snapshot))
    with pytest.raises(
        HistoricalLedgerConflictError,
        match="broker_submitted_at.*broker_updated_at",
    ):
        service.apply(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            expected_preview_digest=snapshot.proof.preview_digest,
            expected_broker_identity_fingerprint=_FINGERPRINT_A,
            observed_at=_OBSERVED,
        )


def test_new_fifo_issue_fails_replay_and_rolls_back(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    sell_order = _order(
        "unmatched-sell",
        "Sell",
        "5",
        "100",
        _epoch(16, 0),
        _epoch(16, 0, 1),
    )
    sell_execution = _execution(
        "unmatched-sell",
        "unmatched-trade",
        "5",
        "100",
        _epoch(16, 0, 1),
    )
    snapshot = _snapshot(payloads=(
        {"has_more": False, "orders": [sell_order]},
        {"has_more": False, "trades": [sell_execution]},
    ))
    service = HistoricalLedgerImportService(factory, _SequenceReader(snapshot))

    with pytest.raises(HistoricalLedgerReplayError, match="introduce"):
        service.apply(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            expected_preview_digest=snapshot.proof.preview_digest,
            expected_broker_identity_fingerprint=_FINGERPRINT_A,
            observed_at=_OBSERVED,
        )

    with factory() as db:
        assert db.query(OrderRecord).count() == 0
        assert db.query(TradeEvent).count() == 0


def test_forged_order_execution_evidence_fails_before_database_access(
    tmp_path,
) -> None:
    _engine, factory = _factory(tmp_path)
    snapshot = _snapshot()
    first = snapshot.filled_orders[0]
    forged_execution = replace(first.executions[0], quantity=first.executed_quantity)
    forged_order = replace(
        first,
        executions=(forged_execution, *first.executions[1:]),
    )
    forged = replace(
        snapshot,
        filled_orders=(forged_order, *snapshot.filled_orders[1:]),
    )
    service = HistoricalLedgerImportService(factory, _SequenceReader(forged))

    with pytest.raises(HistoricalLedgerImportError, match="raw quantity mismatch"):
        service.preview(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            observed_at=_OBSERVED,
        )

    with factory() as db:
        assert db.query(OrderRecord).count() == 0


def test_truncated_broker_page_fails_before_database_access(tmp_path) -> None:
    _engine, factory = _factory(tmp_path)
    reader = LongportHistoricalCompletenessReader(
        _FakeTransport({"has_more": True, "orders": []}),
        broker_identity_fingerprint=_FINGERPRINT_A,
    )
    service = HistoricalLedgerImportService(factory, reader)

    with pytest.raises(HistoricalCompletenessError, match="truncated"):
        service.preview(
            symbol="NVDA.US",
            start_at=_START,
            end_at=_END,
            observed_at=_OBSERVED,
        )

    with factory() as db:
        assert db.query(OrderRecord).count() == 0


def test_trade_event_source_key_migration_is_minimal_and_unique(tmp_path) -> None:
    db_path = tmp_path / "legacy-events.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE trade_events ("
            "id INTEGER PRIMARY KEY, event_type TEXT, symbol TEXT, "
            "broker_order_id TEXT, side TEXT, status TEXT, message TEXT, "
            "payload_json TEXT, created_at DATETIME)"
        )

    database._ensure_trade_event_source_event_key(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("trade_events")
    }
    indexes = {
        index["name"]
        for index in inspect(engine).get_indexes("trade_events")
    }
    assert "source_event_key" in columns
    assert "ux_trade_events_source_event_key_nonempty" in indexes
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO trade_events (id, source_event_key) VALUES (1, 'key')"
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO trade_events (id, source_event_key) "
                "VALUES (2, 'key')"
            )
