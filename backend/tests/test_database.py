from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import database
from app.models import Base, LLMInteraction, OrderRecord, StrategyConfig


def test_init_db_adds_missing_order_execution_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_order_id VARCHAR(100) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                quantity FLOAT NOT NULL,
                price FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL,
                created_at DATETIME NOT NULL,
                filled_at DATETIME,
                raw_response TEXT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(orders)")}
    assert "executed_quantity" in columns
    assert "executed_price" in columns
    assert {
        "cost_basis_price",
        "cost_basis_quantity",
        "cost_basis_opened_at",
        "position_quantity_before",
        "gross_pnl",
        "pnl_fee",
        "pnl_fee_rate",
        "pnl_fee_source",
        "net_pnl",
        "pnl_source",
    } <= columns

    session = testing_session()
    try:
        session.query(OrderRecord).all()
    finally:
        session.close()

    Base.metadata.drop_all(bind=engine)


def test_init_db_adds_missing_order_raw_response_column(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_orders.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_order_id VARCHAR(100) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                quantity FLOAT NOT NULL,
                price FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL,
                created_at DATETIME NOT NULL,
                filled_at DATETIME,
                executed_quantity FLOAT,
                executed_price FLOAT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(orders)")}
    assert "raw_response" in columns

    Base.metadata.drop_all(bind=engine)


def test_init_db_creates_llm_interactions_table(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "llm_interactions.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        tables = {row[0] for row in db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_interactions" in tables

    session = testing_session()
    try:
        session.add(LLMInteraction(symbol="NVDA.US", market="US", prompt="p", success=True))
        session.commit()
        assert session.query(LLMInteraction).count() == 1
    finally:
        session.close()

    Base.metadata.drop_all(bind=engine)


def test_strategy_v2_shadow_table_migration_is_complete_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "strategy_v2_shadow.db"
    engine = create_engine(f"sqlite:///{db_path}")

    database._ensure_strategy_v2_shadow_tables(engine)
    database._ensure_strategy_v2_shadow_tables(engine)

    inspector = inspect(engine)
    expected_tables = {
        "strategy_v2_shadow_config",
        "strategy_v2_shadow_state",
        "strategy_v2_shadow_decisions",
        "strategy_v2_shadow_trades",
        "strategy_v2_forward_registrations",
        "strategy_v2_forward_evidence",
    }
    assert expected_tables <= set(inspector.get_table_names())
    assert {
        "symbol",
        "enabled",
        "breach_zscore",
        "reclaim_zscore",
        "estimated_fee_rate_us",
        "estimated_fee_rate_hk",
        "updated_at",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_v2_shadow_config")
    }
    assert {
        "idempotency_key",
        "config_version",
        "bar_at",
        "action",
        "features_json",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_v2_shadow_decisions")
    }
    assert {constraint["name"] for constraint in inspector.get_unique_constraints(
        "strategy_v2_shadow_decisions"
    )} == {"uq_strategy_v2_shadow_decision_key"}
    assert "ux_strategy_v2_shadow_trade_open_symbol" in {
        index["name"] for index in inspector.get_indexes("strategy_v2_shadow_trades")
    }
    assert {"holding_deadline", "estimated_fee_rate"} <= {
        column["name"]
        for column in inspector.get_columns("strategy_v2_shadow_trades")
    }
    assert {
        "candidate_algorithm_version",
        "source_config_version",
        "evaluator_digest",
        "candidate_spec_json",
        "registered_at",
        "eligible_after",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_v2_forward_registrations")
    }
    assert {
        "registration_id",
        "target_session_date",
        "disposition",
        "target_bars_sha256",
        "seed_bars_sha256",
        "baseline_input_sha256",
        "candidate_input_sha256",
        "baseline_result_sha256",
        "candidate_result_sha256",
        "evidence_digest_sha256",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_v2_forward_evidence")
    }
    assert "uq_strategy_v2_forward_registration_candidate" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "strategy_v2_forward_registrations"
        )
    }
    assert "uq_strategy_v2_forward_evidence_target" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "strategy_v2_forward_evidence"
        )
    }

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_init_db_adds_missing_strategy_llm_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_strategy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) NOT NULL,
                buy_low FLOAT NOT NULL,
                sell_high FLOAT NOT NULL,
                short_selling BOOLEAN NOT NULL,
                max_daily_loss FLOAT NOT NULL,
                max_consecutive_losses INTEGER NOT NULL,
                sct_key VARCHAR(200) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(strategy_config)")}
    assert "auto_interval_enabled" in columns
    assert "min_profit_amount" in columns
    assert "auto_resume_minutes" in columns
    assert "llm_interval_minutes" in columns
    assert "llm_last_analysis_at" in columns
    assert "llm_reject_reason" in columns

    session = testing_session()
    try:
        session.query(StrategyConfig).all()
    finally:
        session.close()

    Base.metadata.drop_all(bind=engine)


def test_init_db_adds_runtime_symbol_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_runtime_symbol.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE runtime_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine_state VARCHAR(20),
                paused BOOLEAN,
                pause_reason TEXT,
                paused_at DATETIME,
                pause_auto_resumable BOOLEAN,
                kill_switch BOOLEAN,
                daily_pnl FLOAT,
                daily_pnl_date DATE,
                consecutive_losses INTEGER,
                last_price FLOAT,
                last_trigger_price FLOAT,
                last_trigger_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE runtime_state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine_state VARCHAR(20),
                paused BOOLEAN,
                kill_switch BOOLEAN,
                daily_pnl FLOAT,
                consecutive_losses INTEGER,
                last_price FLOAT,
                last_trigger_price FLOAT,
                created_at DATETIME
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        runtime_columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(runtime_state)")}
        snapshot_columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(runtime_state_snapshots)")}
    assert "symbol" in runtime_columns
    assert "symbol" in snapshot_columns

    Base.metadata.drop_all(bind=engine)


def test_runtime_state_entry_rearm_migration_backfills_flat_once(tmp_path) -> None:
    db_path = tmp_path / "legacy_runtime_rearm.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE runtime_state ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "engine_state VARCHAR(20), consecutive_losses INTEGER)"
        )
        connection.execute(
            "INSERT INTO runtime_state (engine_state, consecutive_losses) "
            "VALUES ('flat', 0)"
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}")
    database._ensure_runtime_state_entry_rearm_column(engine)
    database._ensure_runtime_state_entry_rearm_column(engine)

    with engine.connect() as db:
        columns = {
            row[1] for row in db.exec_driver_sql("PRAGMA table_info(runtime_state)")
        }
        migrated_value = db.exec_driver_sql(
            "SELECT long_entry_rearm_required FROM runtime_state WHERE id = 1"
        ).scalar_one()
        db.exec_driver_sql(
            "UPDATE runtime_state SET long_entry_rearm_required = 0 WHERE id = 1"
        )
        db.commit()

    database._ensure_runtime_state_entry_rearm_column(engine)
    with engine.connect() as db:
        value_after_second_run = db.exec_driver_sql(
            "SELECT long_entry_rearm_required FROM runtime_state WHERE id = 1"
        ).scalar_one()

    assert "long_entry_rearm_required" in columns
    assert migrated_value == 1
    assert value_after_second_run == 0
    engine.dispose()


def test_init_db_adds_missing_strategy_trade_safety_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_strategy_trade_safety.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) NOT NULL,
                buy_low FLOAT NOT NULL,
                sell_high FLOAT NOT NULL,
                short_selling BOOLEAN NOT NULL,
                max_daily_loss FLOAT NOT NULL,
                max_consecutive_losses INTEGER NOT NULL,
                sct_key VARCHAR(200) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(strategy_config)")}
    assert {"fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds"} <= columns


def test_init_db_adds_missing_strategy_margin_safety_factor_column(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_strategy_margin.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) NOT NULL,
                buy_low FLOAT NOT NULL,
                sell_high FLOAT NOT NULL,
                short_selling BOOLEAN NOT NULL,
                max_daily_loss FLOAT NOT NULL,
                max_consecutive_losses INTEGER NOT NULL,
                sct_key VARCHAR(200) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)
    database.init_db()
    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(strategy_config)")}
    assert "margin_safety_factor" in columns
    session = testing_session()
    try:
        session.query(StrategyConfig).all()
    finally:
        session.close()
    Base.metadata.drop_all(bind=engine)


def test_init_db_adds_p0_strategy_safety_columns(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_p0_safety.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE strategy_config (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        connection.commit()
    finally:
        connection.close()
    engine = create_engine(f"sqlite:///{db_path}")

    database._ensure_strategy_config_p0_safety_columns(engine)
    database._ensure_strategy_config_p0_safety_columns(engine)

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(strategy_config)")}
    assert {
        "allow_position_addons",
        "max_position_quantity",
        "max_position_notional",
        "max_risk_per_trade",
        "stop_loss_pct",
        "max_holding_minutes",
        "entry_cutoff_minutes_before_close",
        "flatten_minutes_before_close",
        "llm_order_execution_enabled",
    } <= columns


def test_init_db_adds_tracked_entry_and_reduction_metadata(tmp_path) -> None:
    db_path = tmp_path / "legacy_reduction.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE tracked_entries ("
            "symbol VARCHAR(50) PRIMARY KEY, quantity FLOAT, cost FLOAT, updated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE runtime_state (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE runtime_state_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )

    database._ensure_tracked_entry_metadata_columns(engine)
    database._ensure_runtime_reduction_columns(engine)

    with engine.connect() as db:
        tracked_columns = {
            row[1] for row in db.exec_driver_sql("PRAGMA table_info(tracked_entries)")
        }
        runtime_columns = {
            row[1] for row in db.exec_driver_sql("PRAGMA table_info(runtime_state)")
        }
    assert {"side", "opened_at"} <= tracked_columns
    assert {
        "execution_state",
        "reduction_action",
        "reduction_cause",
        "reduction_reason",
        "reduction_started_at",
        "reduction_trigger_price",
    } <= runtime_columns


def test_runtime_state_duplicate_migration_merges_safety_state_and_enforces_uniqueness(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy_duplicate_runtime_state.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE runtime_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                engine_state VARCHAR(20) NOT NULL DEFAULT 'flat',
                paused BOOLEAN NOT NULL DEFAULT 0,
                pause_reason TEXT NOT NULL DEFAULT '',
                paused_at DATETIME,
                pause_auto_resumable BOOLEAN NOT NULL DEFAULT 0,
                kill_switch BOOLEAN NOT NULL DEFAULT 0,
                daily_pnl FLOAT NOT NULL DEFAULT 0,
                daily_pnl_date DATE,
                consecutive_losses INTEGER NOT NULL DEFAULT 0,
                last_price FLOAT NOT NULL DEFAULT 0,
                last_trigger_price FLOAT NOT NULL DEFAULT 0,
                last_trigger_at DATETIME,
                execution_state VARCHAR(20) NOT NULL DEFAULT 'IDLE',
                reduction_action VARCHAR(20) NOT NULL DEFAULT '',
                reduction_cause VARCHAR(30) NOT NULL DEFAULT '',
                reduction_reason TEXT NOT NULL DEFAULT '',
                reduction_started_at DATETIME,
                reduction_trigger_price FLOAT,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO runtime_state (
                symbol, engine_state, paused, pause_reason, paused_at,
                pause_auto_resumable, kill_switch, daily_pnl, daily_pnl_date,
                consecutive_losses, last_price, last_trigger_price,
                last_trigger_at, execution_state, reduction_action,
                reduction_cause, reduction_reason, reduction_started_at,
                reduction_trigger_price, updated_at
            ) VALUES (
                'NVDA.US', 'long', 1, 'protective exit in progress',
                '2026-07-10 15:00:00', 0, 0, -120.0, '2026-07-10',
                2, 200.0, 199.0, '2026-07-10 14:59:00', 'REDUCING',
                'SELL', 'STOP_LOSS', 'hard stop reached',
                '2026-07-10 15:00:00', 198.0, '2026-07-10 15:01:00'
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO runtime_state (
                symbol, engine_state, paused, pause_reason, paused_at,
                pause_auto_resumable, kill_switch, daily_pnl, daily_pnl_date,
                consecutive_losses, last_price, last_trigger_price,
                last_trigger_at, execution_state, reduction_action,
                reduction_cause, reduction_reason, reduction_started_at,
                reduction_trigger_price, updated_at
            ) VALUES (
                'NVDA.US', 'long', 0, '', NULL, 0, 1, -20.0,
                '2026-07-10', 4, 201.0, 200.0,
                '2026-07-10 15:02:00', 'IDLE', '', '', '', NULL, NULL,
                '2026-07-10 15:02:00'
            )
            """
        )

    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()
    database.init_db()

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            """
            SELECT symbol, engine_state, paused, pause_reason, kill_switch,
                   daily_pnl, consecutive_losses, last_price,
                   execution_state, reduction_action, reduction_cause,
                   reduction_reason, reduction_started_at,
                   reduction_trigger_price, updated_at
            FROM runtime_state WHERE symbol = 'NVDA.US'
            """
        ).fetchall()
        indexes = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list(runtime_state)"
            ).fetchall()
        }

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "NVDA.US"
    assert row[1] == "long"
    assert row[2] == 1
    assert row[3] == "protective exit in progress"
    assert row[4] == 1
    assert row[5] == -120.0
    assert row[6] == 4
    assert row[7] == 201.0
    assert row[8] == "REDUCING"
    assert row[9] == "SELL"
    assert row[10] == "STOP_LOSS"
    assert row[11] == "hard stop reached"
    assert str(row[12]).startswith("2026-07-10 15:00:00")
    assert row[13] == 198.0
    assert str(row[14]).startswith("2026-07-10 15:02:00")
    assert "ux_runtime_state_symbol" in indexes

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO runtime_state (symbol, updated_at)
                VALUES ('NVDA.US', '2026-07-10 15:03:00')
                """
            )


def test_order_duplicate_migration_preserves_terminal_fill_and_allows_empty_ids(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "legacy_duplicate_orders.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_order_id VARCHAR(100) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                quantity FLOAT NOT NULL,
                price FLOAT NOT NULL,
                executed_quantity FLOAT,
                executed_price FLOAT,
                status VARCHAR(20) NOT NULL,
                created_at DATETIME,
                filled_at DATETIME,
                raw_response TEXT
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO orders (
                broker_order_id, symbol, side, quantity, price,
                executed_quantity, executed_price, status, created_at,
                filled_at, raw_response
            ) VALUES
                ('order-duplicate', 'NVDA.US', 'BUY', 10, 100, NULL, NULL,
                 'SUBMITTED', '2026-07-10 10:00:00', NULL, 'submitted'),
                ('order-duplicate', 'NVDA.US', 'BUY', 10, 100, 4, 101.5,
                 'PARTIAL_FILLED', '2026-07-10 10:01:00',
                 '2026-07-10 10:02:00', 'partial'),
                ('order-duplicate', 'NVDA.US', 'BUY', 10, 100, 0, NULL,
                 'CANCELLED', '2026-07-10 10:02:00',
                 '2026-07-10 10:03:00', 'cancelled'),
                ('', 'AAPL.US', 'BUY', 1, 200, NULL, NULL, 'SUBMITTED',
                 '2026-07-10 11:00:00', NULL, 'local-one'),
                ('', 'MSFT.US', 'SELL', 2, 300, NULL, NULL, 'REJECTED',
                 '2026-07-10 11:01:00', NULL, 'local-two')
            """
        )

    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()
    database.init_db()

    with engine.connect() as connection:
        merged = connection.exec_driver_sql(
            """
            SELECT status, quantity, executed_quantity, executed_price,
                   created_at, filled_at, raw_response
            FROM orders WHERE broker_order_id = 'order-duplicate'
            """
        ).fetchall()
        empty_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM orders WHERE broker_order_id = ''"
        ).scalar_one()
        indexes = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA index_list(orders)").fetchall()
        }

    assert len(merged) == 1
    row = merged[0]
    assert row[0] == "CANCELLED"
    assert row[1] == 10.0
    assert row[2] == 4.0
    assert row[3] == 101.5
    assert str(row[4]).startswith("2026-07-10 10:00:00")
    assert str(row[5]).startswith("2026-07-10 10:03:00")
    assert row[6] == "cancelled"
    assert empty_count == 2
    assert "ux_orders_broker_order_id_nonempty" in indexes

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO orders (
                broker_order_id, symbol, side, quantity, price, status, created_at
            ) VALUES ('', 'TSLA.US', 'BUY', 1, 250, 'SUBMITTED',
                      '2026-07-10 11:02:00')
            """
        )
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM orders WHERE broker_order_id = ''"
        ).scalar_one() == 3

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO orders (
                    broker_order_id, symbol, side, quantity, price, status,
                    created_at
                ) VALUES ('order-duplicate', 'NVDA.US', 'BUY', 1, 100,
                          'SUBMITTED', '2026-07-10 11:03:00')
                """
            )


def test_init_db_adds_missing_runtime_state_daily_pnl_date_column(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy_runtime_state.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE runtime_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine_state VARCHAR(20) NOT NULL,
                paused BOOLEAN NOT NULL,
                kill_switch BOOLEAN NOT NULL,
                daily_pnl FLOAT NOT NULL,
                consecutive_losses INTEGER NOT NULL,
                last_price FLOAT NOT NULL,
                last_trigger_price FLOAT NOT NULL,
                last_trigger_at DATETIME,
                updated_at DATETIME NOT NULL
            )
            """
        )
        # Seed a pre-existing row with non-zero P&L and losses to verify the
        # migration does NOT reset them when backfilling daily_pnl_date.
        connection.execute(
            "INSERT INTO runtime_state "
            "(engine_state, paused, kill_switch, daily_pnl, consecutive_losses, "
            "last_price, last_trigger_price, last_trigger_at, updated_at) "
            "VALUES ('LONG', 0, 0, 1234.56, 5, 100.0, 99.5, NULL, '2026-01-01 00:00:00')"
        )
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    database.init_db()

    with engine.connect() as db:
        columns = {row[1] for row in db.exec_driver_sql("PRAGMA table_info(runtime_state)")}
    assert "daily_pnl_date" in columns
    assert "pause_reason" in columns
    assert "paused_at" in columns
    assert "pause_auto_resumable" in columns

    # The migration must backfill daily_pnl_date WITHOUT resetting P&L/losses.
    with engine.connect() as db:
        row = db.exec_driver_sql(
            "SELECT daily_pnl, consecutive_losses, daily_pnl_date FROM runtime_state WHERE id = 1"
        ).fetchone()
    assert row is not None
    assert row[0] == 1234.56  # daily_pnl preserved
    assert row[1] == 5  # consecutive_losses preserved
    assert row[2] is not None  # daily_pnl_date backfilled

    Base.metadata.drop_all(bind=engine)


def test_ensure_credential_config_notification_channels_column_adds_and_backfills(
    tmp_path, monkeypatch
) -> None:
    import importlib
    import json

    db_path = tmp_path / "creds.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import app.database as database

    importlib.reload(database)
    database.init_db()
    with database.engine.connect() as conn:
        cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(credential_config);").fetchall()
        }
        assert "notification_channels" in cols

    from app.models import CredentialConfig

    with database.SessionLocal() as db:
        cc = CredentialConfig()
        db.add(cc)
        db.commit()
        db.refresh(cc)
        assert json.loads(cc.notification_channels) == [
            {"type": "serverchan", "severity_floor": "INFO"}
        ]


def test_ensure_audit_log_table_creates_schema(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "audit.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib

    from app import database

    importlib.reload(database)
    database.init_db()

    with database.engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(audit_logs);").fetchall()}
    assert cols == {
        "id",
        "action",
        "severity",
        "actor_hash",
        "source_ip",
        "request_summary",
        "result",
        "created_at",
    }


def test_ensure_audit_log_table_is_idempotent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "audit2.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib

    from app import database

    importlib.reload(database)
    database.init_db()
    database.init_db()



def test_init_db_creates_strategy_experiment_tables(tmp_path, monkeypatch) -> None:
    """Verify init_db creates strategy_experiments and strategy_experiment_runs with expected columns."""
    db_path = tmp_path / "strategy_exp.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib

    from app import database

    importlib.reload(database)
    database.init_db()

    with database.engine.connect() as conn:
        cols_exp = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(strategy_experiments);").fetchall()}
    assert cols_exp == {
        "id", "name", "symbol", "base_params_json", "parameter_grid_json",
        "status", "estimated_runs", "completed_runs", "failed_runs", "error",
        "created_at", "completed_at",
    }

    with database.engine.connect() as conn:
        cols_run = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(strategy_experiment_runs);").fetchall()}
    assert cols_run == {
        "id", "experiment_id", "parameters_json", "status",
        "total_pnl", "total_return_pct", "max_drawdown_pct", "win_rate",
        "trade_count", "closed_trade_count",
        "sharpe_ratio", "profit_factor", "profit_loss_ratio",
        "result_summary_json", "error", "created_at",
    }


def test_init_db_creates_report_query_indexes(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "indexes.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib

    from app import database

    importlib.reload(database)
    database.init_db()

    with database.engine.connect() as conn:
        orders_indexes = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='orders'")}
        trade_events_indexes = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trade_events'")}
        llm_indexes = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='llm_interactions'")}

    assert "ix_orders_symbol_filled_at" in orders_indexes
    assert "ix_orders_symbol_created_at" in orders_indexes
    assert "ix_orders_status" in orders_indexes
    assert "ix_trade_events_symbol_created_at" in trade_events_indexes
    assert "ix_trade_events_event_type" in trade_events_indexes
    assert "ix_llm_interactions_symbol_created_at" in llm_indexes
    assert "ix_llm_interactions_created_at_id" in llm_indexes


def test_report_index_migration_adds_llm_created_at_id_index(tmp_path) -> None:
    from app import database

    db_path = tmp_path / "legacy_llm_index.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE llm_interactions ("
            "id INTEGER PRIMARY KEY, symbol VARCHAR(50), created_at DATETIME)"
        )

    database._ensure_report_query_indexes(legacy_engine)

    with legacy_engine.connect() as connection:
        indexes = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='llm_interactions'"
            )
        }
    assert "ix_llm_interactions_created_at_id" in indexes


def test_execution_ledger_migration_freezes_legacy_fill_fee(tmp_path) -> None:
    db_path = tmp_path / "legacy_orders.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE strategy_config (id INTEGER PRIMARY KEY, "
            "fee_rate_us FLOAT, fee_rate_hk FLOAT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO strategy_config VALUES (1, 0.001, 0.004)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, broker_order_id TEXT, "
            "symbol TEXT, side TEXT, quantity FLOAT, price FLOAT, "
            "executed_quantity FLOAT, executed_price FLOAT, status TEXT, "
            "created_at DATETIME, filled_at DATETIME, raw_response TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO orders (id, symbol, side, quantity, price, "
            "executed_quantity, executed_price, status) VALUES "
            "(1, 'AAPL.US', 'BUY', 10, 100, 10, 101, 'FILLED')"
        )

    database._ensure_order_execution_ledger_columns(legacy_engine)

    with legacy_engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT estimated_fee, fee_source FROM orders WHERE id = 1"
        ).one()
        columns = {
            value[1]
            for value in connection.exec_driver_sql("PRAGMA table_info(orders)")
        }
    assert row[0] == pytest.approx(1.01)
    assert row[1] == "ESTIMATED"
    assert {"decision_bid", "actual_fee", "slippage_bps", "mfe_pct"} <= columns



def test_sqlite_wal_and_busy_timeout_enabled(tmp_path, monkeypatch) -> None:
    """P0-2: SQLite connection must enable WAL, busy_timeout, and FK enforcement.

    The runner thread, FastAPI handlers, and asyncio.to_thread tasks all write
    concurrently. Without WAL + busy_timeout, any sustained write load surfaces
    as OperationalError: database is locked and tracked_entries silently drifts
    because _persist_entry_safe swallows the error.
    """
    import importlib
    from sqlalchemy import text

    db_file = tmp_path / "wal.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_file}")
    # database.engine is bound at import time to settings.database_url; reload
    # the module so the WAL PRAGMA listener attaches to our test engine.
    from app import database as _db_module
    importlib.reload(_db_module)
    database = _db_module

    engine = database.engine
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
        fk_enabled = conn.execute(text("PRAGMA foreign_keys")).scalar()

    assert str(journal_mode).lower() == "wal", f"journal_mode was {journal_mode!r}"
    assert busy_timeout_ms is not None
    assert busy_timeout_ms >= 5000, f"busy_timeout was {busy_timeout_ms}"
    assert fk_enabled is not None
    assert int(fk_enabled) == 1, f"foreign_keys was {fk_enabled}"
