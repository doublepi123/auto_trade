from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.models import Base, OrderRecord


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
