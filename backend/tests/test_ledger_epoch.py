# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Pre-handover external orders must not enter the bot ledger.

The accounting period starts at min(ledger_epoch, runner start). Terminal
broker orders created strictly before that cutoff, with no local OrderRecord
and no ORDER_SUBMITTED provenance, belong to the owner and are ignored.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.broker import BrokerOrder
from app.database import SessionLocal
from app.models import OrderRecord, TradeEvent
from app.runner import AppRunner
from app import database

database.init_db()

EPOCH = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
STARTED = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
PRE_EPOCH = datetime(2026, 10, 6, 13, 23, 58, tzinfo=timezone.utc)
PRE_EPOCH_2 = datetime(2026, 10, 6, 13, 31, 31, tzinfo=timezone.utc)


def _clean() -> None:
    db = SessionLocal()
    try:
        db.query(TradeEvent).delete()
        db.query(OrderRecord).delete()
        db.commit()
    finally:
        db.close()


def _order(
    order_id: str,
    *,
    status: str = "FILLED",
    created_at: datetime | None = PRE_EPOCH,
    symbol: str = "TQQQ.US",
) -> BrokerOrder:
    filled = status == "FILLED"
    return BrokerOrder(
        broker_order_id=order_id,
        symbol=symbol,
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("50"),
        executed_quantity=Decimal("10") if filled else Decimal("0"),
        executed_price=Decimal("50") if filled else Decimal("0"),
        status=status,
        created_at=created_at,
        filled_at=created_at if filled else None,
    )


class _Broker:
    def __init__(self, orders: list[BrokerOrder]) -> None:
        self.orders = orders
        self.enriched: list[object] = []

    def get_today_orders(self) -> list[BrokerOrder]:
        return list(self.orders)


def _runner(
    orders: list[BrokerOrder],
    *,
    epoch: datetime | None = EPOCH,
    started_at: datetime | None = STARTED,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> AppRunner:
    runner = AppRunner()
    runner.broker = _Broker(orders)
    runner._started_at = started_at
    if monkeypatch is not None:
        monkeypatch.setattr("app.runner.settings.ledger_epoch", epoch)
    return runner


def _sync(runner: AppRunner) -> int:
    return runner.sync_today_orders_from_broker(force=True)


def _pause_reason(runner: AppRunner) -> str:
    return str(runner.risk.pause_reason or "")


class TestLedgerEpochFilter:
    def setup_method(self) -> None:
        _clean()

    def teardown_method(self) -> None:
        _clean()

    def test_pre_epoch_external_fills_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orders = [
            _order("owner-tqqq-1", created_at=PRE_EPOCH),
            _order("owner-tqqq-2", created_at=PRE_EPOCH_2),
        ]
        runner = _runner(orders, monkeypatch=monkeypatch)
        runner.risk.daily_pnl = -12.5
        runner.risk.consecutive_losses = 1

        changed = _sync(runner)

        assert changed == 0
        assert runner.risk.paused is False
        assert "ORDER_RECONCILIATION_UNCERTAIN" not in _pause_reason(runner)
        assert runner._last_order_sync_succeeded is True
        assert runner.risk.daily_pnl == -12.5
        assert runner.risk.consecutive_losses == 1
        with SessionLocal() as db:
            assert db.query(OrderRecord).count() == 0
            assert (
                db.query(TradeEvent)
                .filter(TradeEvent.event_type == "ORDER_SYNCED")
                .count()
                == 0
            )

    def test_epoch_unset_still_latches_unattributable_fills(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUTO_TRADE_LEDGER_EPOCH", raising=False)
        orders = [_order("owner-tqqq-1"), _order("owner-tqqq-2", created_at=PRE_EPOCH_2)]
        runner = _runner(orders, epoch=None, monkeypatch=monkeypatch)
        _sync(runner)
        assert runner.risk.paused is True
        assert _pause_reason(runner).startswith("ORDER_RECONCILIATION_UNCERTAIN")

    def test_pre_epoch_live_order_still_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner([_order("live-1", status="SUBMITTED")], monkeypatch=monkeypatch)
        _sync(runner)
        assert runner.risk.paused is True
        assert _pause_reason(runner).startswith("ORDER_RECONCILIATION_UNCERTAIN")
        with SessionLocal() as db:
            assert (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == "live-1")
                .count()
                == 1
            )

    def test_terminal_order_after_cutoff_still_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = STARTED + timedelta(minutes=5)
        runner = _runner(
            [_order("after-cutoff", created_at=created)],
            monkeypatch=monkeypatch,
        )
        _sync(runner)
        assert runner.risk.paused is True
        assert "after-cutoff" in _pause_reason(runner)
        with SessionLocal() as db:
            assert (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == "after-cutoff")
                .count()
                == 1
            )

    def test_pre_epoch_terminal_with_order_record_is_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with SessionLocal() as db:
            db.add(
                OrderRecord(
                    broker_order_id="owned-1",
                    symbol="TQQQ.US",
                    side="BUY",
                    quantity=10,
                    price=50,
                    status="SUBMITTED",
                    created_at=PRE_EPOCH,
                )
            )
            db.commit()
        runner = _runner([_order("owned-1")], monkeypatch=monkeypatch)
        changed = _sync(runner)
        assert changed == 1
        with SessionLocal() as db:
            row = db.query(OrderRecord).filter(OrderRecord.broker_order_id == "owned-1").one()
            assert row.status == "FILLED"

    def test_pre_epoch_terminal_with_submitted_event_is_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with SessionLocal() as db:
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="TQQQ.US",
                    broker_order_id="submitted-1",
                    side="BUY",
                    status="SUBMITTED",
                    message="submitted",
                )
            )
            db.commit()
        runner = _runner([_order("submitted-1")], monkeypatch=monkeypatch)
        _sync(runner)
        with SessionLocal() as db:
            assert (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == "submitted-1")
                .count()
                == 1
            )
        assert runner.risk.paused is True
        assert "submitted-1" in _pause_reason(runner)

    def test_missing_created_at_is_not_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner([_order("no-time", created_at=None)], monkeypatch=monkeypatch)
        _sync(runner)
        assert runner.risk.paused is True
        assert "no-time" in _pause_reason(runner)

    def test_cutoff_is_runner_start_when_epoch_is_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        between = STARTED + timedelta(minutes=2)
        later_epoch = STARTED + timedelta(hours=1)
        runner = _runner(
            [_order("between", created_at=between)],
            epoch=later_epoch,
            started_at=STARTED,
            monkeypatch=monkeypatch,
        )
        _sync(runner)
        assert runner.risk.paused is True
        assert "between" in _pause_reason(runner)
        with SessionLocal() as db:
            assert (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == "between")
                .count()
                == 1
            )

    @pytest.mark.parametrize("naive", [False, True])
    def test_naive_and_aware_created_at(
        self, naive: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = PRE_EPOCH.replace(tzinfo=None) if naive else PRE_EPOCH
        runner = _runner([_order("tz-1", created_at=created)], monkeypatch=monkeypatch)
        _sync(runner)
        assert runner.risk.paused is False
        assert runner._last_order_sync_succeeded is True
        with SessionLocal() as db:
            assert db.query(OrderRecord).count() == 0


class TestLedgerEpochSettings:
    def test_empty_epoch_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_LEDGER_EPOCH", "")
        assert Settings().ledger_epoch is None

    def test_iso_z_is_aware_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_LEDGER_EPOCH", "2026-10-06T20:00:00Z")
        value = Settings().ledger_epoch
        assert value == EPOCH
        assert value is not None
        assert value.tzinfo is not None
        assert value.utcoffset() == timedelta(0)

    def test_naive_iso_is_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_LEDGER_EPOCH", "2026-10-06T20:00:00")
        value = Settings().ledger_epoch
        assert value == EPOCH
        assert value is not None
        assert value.utcoffset() == timedelta(0)

    def test_garbage_fails_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_LEDGER_EPOCH", "not-a-timestamp")
        with pytest.raises(ValidationError):
            Settings()
