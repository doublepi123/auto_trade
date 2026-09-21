from __future__ import annotations

import os
import tempfile

_DB_PATH = os.path.join(
    tempfile.gettempdir(),
    f"auto_trade_order_lifecycle_schema_{os.getpid()}.db",
)
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from app import database


def _fresh_engine(name: str) -> Engine:
    path = os.path.join(
        tempfile.gettempdir(),
        f"auto_trade_{name}_{os.getpid()}.db",
    )
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        if os.path.exists(candidate):
            os.remove(candidate)
    return create_engine(f"sqlite:///{path}")


def test_reconciliation_incident_upgrade_adds_diagnostic_columns() -> None:
    # Given
    engine = _fresh_engine("reconciliation_incident_upgrade")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE reconciliation_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source VARCHAR(80) NOT NULL,
                failure_category VARCHAR(80) NOT NULL,
                symbols_json TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                alert_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                last_alerted_at DATETIME NOT NULL,
                next_alert_at DATETIME NOT NULL,
                recovered_at DATETIME,
                message TEXT NOT NULL DEFAULT '',
                error_type VARCHAR(100) NOT NULL DEFAULT '',
                UNIQUE (source, failure_category, symbols_json)
            )
            """
        )

    # When
    database._ensure_reconciliation_incidents_table(engine)
    database._ensure_reconciliation_incidents_table(engine)

    # Then
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("reconciliation_incidents")
    }
    assert {
        "sdk_error_code",
        "sdk_error_category",
        "error_message",
        "probe_duration_ms",
        "exit_code",
        "retry_count",
        "stderr",
    } <= columns


def test_terminal_callback_table_materializes_idempotently() -> None:
    # Given
    engine = _fresh_engine("terminal_callback_upgrade")

    # When
    database._ensure_order_terminal_callbacks_table(engine)
    database._ensure_order_terminal_callbacks_table(engine)

    # Then
    inspector = inspect(engine)
    assert "order_terminal_callbacks" in inspector.get_table_names()
    primary_key = inspector.get_pk_constraint("order_terminal_callbacks")
    assert primary_key["constrained_columns"] == [
        "broker_order_id",
        "terminal_status",
    ]


def test_fill_settlements_table_materializes_idempotently() -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    # Given
    engine = _fresh_engine("fill_settlements_upgrade")
    try:
        # When
        database._ensure_fill_settlements_table(engine)
        database._ensure_fill_settlements_table(engine)

        # Then
        inspector = inspect(engine)
        assert "fill_settlements" in inspector.get_table_names()
        assert inspector.get_pk_constraint("fill_settlements")[
            "constrained_columns"
        ] == ["broker_order_id"]
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO fill_settlements (broker_order_id, symbol, action, "
                "booked_quantity, booked_price, quantity_source, price_source, "
                "tracked_quantity_after, tracked_cost_after, first_terminal_status, "
                "created_at) VALUES ('order-1', 'AAPL.US', 'OPEN_LONG', 1, 100, "
                "'BROKER', 'BROKER', 1, 100, 'FILLED', CURRENT_TIMESTAMP)"
            )
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            with engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM fill_settlements")
    finally:
        engine.dispose()
