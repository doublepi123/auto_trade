from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine
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