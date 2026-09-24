from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import database, runner
from app.api import deps
from app.models import FillSettlement
from app.services import order_terminal_callback_service as callbacks
from tests.runner_db_isolation_plugin import runner_database_for

_TARGET: Final = Path(__file__).with_name("test_runner.py")
_ORIGINAL_FACTORY: Final = database.SessionLocal


def _insert_receipt() -> None:
    with database.SessionLocal() as db:
        db.add(FillSettlement(
            broker_order_id="isolation-contract-order",
            symbol="AAPL.US",
            action="BUY",
            booked_quantity=1.0,
            booked_price=100.0,
            quantity_source="test",
            price_source="test",
            tracked_quantity_after=1.0,
            tracked_cost_after=100.0,
            first_terminal_status="FILLED",
        ))
        db.commit()


def test_two_tests_reusing_one_order_id_both_see_a_fresh_database() -> None:
    # Given two successive target-test lifetimes, with the same broker order id.
    paths: list[str | None] = []
    for _ in range(2):
        with runner_database_for(_TARGET):
            # When each test books its receipt.
            assert database.SessionLocal is not _ORIGINAL_FACTORY
            paths.append(database.engine.url.database)
            _insert_receipt()
            # Then another session (including a restarted runner) sees that receipt.
            with runner.AppRunner._db_session() as db:
                receipt = db.get(FillSettlement, "isolation-contract-order")
                assert receipt is not None
                assert receipt.booked_quantity == 1.0
    assert paths[0] != paths[1]


def test_delete_trigger_still_rejects_deletion_under_isolation() -> None:
    # Given a committed receipt in a target-test database.
    with runner_database_for(_TARGET):
        assert database.SessionLocal is not _ORIGINAL_FACTORY
        _insert_receipt()
        # When deletion is attempted, then the real production trigger rejects it.
        with database.SessionLocal() as db:
            with pytest.raises(IntegrityError, match="rows cannot be deleted"):
                db.execute(text("DELETE FROM fill_settlements"))


def test_non_target_modules_keep_the_shared_database() -> None:
    # Given this non-target module, including its autouse plugin invocation.
    original_engine = database.engine
    # When the plugin handles a non-target path.
    with runner_database_for(Path(__file__)):
        # Then original objects remain bound.
        assert database.SessionLocal is _ORIGINAL_FACTORY
        assert database.engine is original_engine


def test_audit_singleton_created_during_target_does_not_outlive_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given no process-wide audit logger yet.
    monkeypatch.setattr(deps, "_audit_logger_singleton", None)
    # When a target test lazily creates it against the private database.
    with runner_database_for(_TARGET):
        deps.init_audit_logger()
    # Then later tests get a logger bound to the shared database again.
    assert deps.init_audit_logger()._session_factory is database.SessionLocal


@pytest.mark.parametrize("fail", [False, True])
def test_references_are_restored_after_teardown(fail: bool) -> None:
    # Given the exact original objects, which need not all be identical.
    originals = (database.engine, database.SessionLocal, runner.SessionLocal, callbacks.SessionLocal)
    paths: list[Path] = []

    def run_target() -> None:
        with runner_database_for(_TARGET):
            assert database.engine is not originals[0]
            assert database.SessionLocal is runner.SessionLocal is callbacks.SessionLocal
            filename = database.engine.url.database
            assert filename is not None
            paths.append(Path(filename))
            assert paths[0].is_file()
            if fail:
                raise RuntimeError("simulated test failure")

    # When a target test completes or fails.
    if fail:
        with pytest.raises(RuntimeError, match="simulated test failure"):
            run_target()
    else:
        run_target()
    # Then every reference is restored and all owned SQLite files are removed.
    current = (database.engine, database.SessionLocal, runner.SessionLocal, callbacks.SessionLocal)
    assert all(actual is original for actual, original in zip(current, originals, strict=True))
    assert not paths[0].exists()
    assert not Path(f"{paths[0]}-wal").exists()
    assert not Path(f"{paths[0]}-shm").exists()
