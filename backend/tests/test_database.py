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
