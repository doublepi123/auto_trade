from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    from app.models import Base, CredentialConfig, StrategyConfig

    Base.metadata.create_all(bind=engine)
    _ensure_order_execution_columns(engine)
    _ensure_order_raw_response_column(engine)
    _ensure_strategy_config_llm_columns(engine)
    _ensure_strategy_config_trade_safety_columns(engine)
    _ensure_strategy_config_session_columns(engine)
    _ensure_runtime_state_daily_pnl_date_column(engine)
    _ensure_tracked_entries_table(engine)
    _ensure_audit_log_table(engine)
    _ensure_credential_config_notification_channels_column(engine)
    _ensure_watchlist_items_table(engine)
    _ensure_prompt_versions_table(engine)
    _ensure_experiment_results_table(engine)
    _ensure_strategy_experiments_table(engine)
    _ensure_strategy_experiment_runs_table(engine)
    _ensure_strategy_config_margin_safety_factor(engine)
    _ensure_llm_interaction_variant_column(engine)

    db = SessionLocal()
    try:
        _bootstrap_credentials(db, CredentialConfig, StrategyConfig)
    finally:
        db.close()


def _ensure_order_execution_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "orders" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("orders")}
    missing_columns = {
        "executed_quantity": "FLOAT",
        "executed_price": "FLOAT",
    }.items()

    with db_engine.begin() as connection:
        for name, column_type in missing_columns:
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE orders ADD COLUMN {name} {column_type}")


def _ensure_order_raw_response_column(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "orders" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("orders")}
    with db_engine.begin() as connection:
        if "raw_response" not in columns:
            connection.exec_driver_sql("ALTER TABLE orders ADD COLUMN raw_response TEXT")


def _ensure_strategy_config_llm_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    missing_columns = {
        "min_profit_amount": "FLOAT DEFAULT 0 NOT NULL",
        "auto_resume_minutes": "INTEGER DEFAULT 3 NOT NULL",
        "auto_interval_enabled": "BOOLEAN DEFAULT 0 NOT NULL",
        "llm_interval_minutes": "INTEGER DEFAULT 2 NOT NULL",
        "llm_suggested_buy_low": "FLOAT",
        "llm_suggested_sell_high": "FLOAT",
        "llm_confidence_score": "FLOAT",
        "llm_analysis": "TEXT",
        "llm_last_analysis_at": "DATETIME",
        "llm_next_analysis_at": "DATETIME",
        "llm_applied_buy_low": "FLOAT",
        "llm_applied_sell_high": "FLOAT",
        "llm_applied_at": "DATETIME",
        "llm_reject_reason": "TEXT",
    }.items()

    with db_engine.begin() as connection:
        for name, column_type in missing_columns:
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE strategy_config ADD COLUMN {name} {column_type}")


def _ensure_strategy_config_trade_safety_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    missing_columns = {
        "fee_rate_us": "FLOAT DEFAULT 0.0005 NOT NULL",
        "fee_rate_hk": "FLOAT DEFAULT 0.003 NOT NULL",
        "min_repricing_pct": "FLOAT DEFAULT 0.003 NOT NULL",
        "llm_action_cooldown_seconds": "INTEGER DEFAULT 60 NOT NULL",
    }.items()

    with db_engine.begin() as connection:
        for name, column_type in missing_columns:
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE strategy_config ADD COLUMN {name} {column_type}"
                )


def _ensure_strategy_config_session_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    with db_engine.begin() as connection:
        if "trading_session_mode" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN trading_session_mode VARCHAR(16) DEFAULT 'ANY' NOT NULL"
            )


def _ensure_runtime_state_daily_pnl_date_column(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "runtime_state" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    with db_engine.begin() as connection:
        if "daily_pnl_date" not in columns:
            connection.exec_driver_sql("ALTER TABLE runtime_state ADD COLUMN daily_pnl_date DATE")
        if "pause_reason" not in columns:
            connection.exec_driver_sql("ALTER TABLE runtime_state ADD COLUMN pause_reason TEXT DEFAULT '' NOT NULL")
        if "paused_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE runtime_state ADD COLUMN paused_at DATETIME")
        if "pause_auto_resumable" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE runtime_state ADD COLUMN pause_auto_resumable BOOLEAN DEFAULT 0 NOT NULL"
            )
        connection.exec_driver_sql(
            "UPDATE runtime_state SET daily_pnl = 0, consecutive_losses = 0, daily_pnl_date = DATE('now') WHERE daily_pnl_date IS NULL"
        )


def _ensure_audit_log_table(db_engine: Engine) -> None:
    from app.models import Base

    insp = inspect(db_engine)
    if "audit_logs" in insp.get_table_names():
        return
    Base.metadata.tables["audit_logs"].create(db_engine, checkfirst=True)


DEFAULT_NOTIFICATION_CHANNELS_JSON = '[{"type":"serverchan","severity_floor":"INFO"}]'


def _ensure_credential_config_notification_channels_column(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "credential_config" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("credential_config")}
    with db_engine.begin() as connection:
        if "notification_channels" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE credential_config ADD COLUMN notification_channels TEXT "
                f"DEFAULT '{DEFAULT_NOTIFICATION_CHANNELS_JSON}' NOT NULL"
            )
            connection.exec_driver_sql(
                "UPDATE credential_config SET notification_channels = "
                f"'{DEFAULT_NOTIFICATION_CHANNELS_JSON}' "
                "WHERE notification_channels IS NULL OR notification_channels = ''"
            )


def _ensure_tracked_entries_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "tracked_entries" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS tracked_entries (
                symbol VARCHAR(50) PRIMARY KEY,
                quantity FLOAT NOT NULL DEFAULT 0,
                cost FLOAT NOT NULL DEFAULT 0,
                updated_at DATETIME
            )
            """
        )


def _ensure_watchlist_items_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "watchlist_items" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL UNIQUE,
                market VARCHAR(10) DEFAULT 'US' NOT NULL,
                alias VARCHAR(100) DEFAULT '' NOT NULL,
                is_active BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME
            )
            """
        )


def _ensure_prompt_versions_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "prompt_versions" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                version VARCHAR(20) NOT NULL,
                description TEXT DEFAULT '' NOT NULL,
                template TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME
            )
            """
        )


def _ensure_experiment_results_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "experiment_results" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS experiment_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_name VARCHAR(100) NOT NULL,
                variant_name VARCHAR(100) NOT NULL,
                interaction_id INTEGER,
                order_action VARCHAR(32) DEFAULT 'NONE' NOT NULL,
                predicted_direction VARCHAR(32) DEFAULT '' NOT NULL,
                actual_pnl REAL DEFAULT 0.0 NOT NULL,
                was_profitable BOOLEAN,
                created_at DATETIME
            )
            """
        )



def _ensure_strategy_experiments_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_experiments" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS strategy_experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                base_params_json TEXT NOT NULL,
                parameter_grid_json TEXT NOT NULL,
                status VARCHAR(16) DEFAULT 'PENDING' NOT NULL,
                estimated_runs INTEGER DEFAULT 0 NOT NULL,
                completed_runs INTEGER DEFAULT 0 NOT NULL,
                failed_runs INTEGER DEFAULT 0 NOT NULL,
                error TEXT DEFAULT '' NOT NULL,
                created_at DATETIME,
                completed_at DATETIME
            )
            """
        )


def _ensure_strategy_experiment_runs_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_experiment_runs" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS strategy_experiment_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL,
                parameters_json TEXT NOT NULL,
                status VARCHAR(16) DEFAULT 'COMPLETED' NOT NULL,
                total_pnl REAL DEFAULT 0.0 NOT NULL,
                total_return_pct REAL DEFAULT 0.0 NOT NULL,
                max_drawdown_pct REAL DEFAULT 0.0 NOT NULL,
                win_rate REAL DEFAULT 0.0 NOT NULL,
                trade_count INTEGER DEFAULT 0 NOT NULL,
                closed_trade_count INTEGER DEFAULT 0 NOT NULL,
                result_summary_json TEXT DEFAULT '{}' NOT NULL,
                error TEXT DEFAULT '' NOT NULL,
                created_at DATETIME
            )
            """
        )

def _ensure_strategy_config_margin_safety_factor(db_engine: Engine) -> None:
    """Add margin_safety_factor column to strategy_config if missing."""
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    with db_engine.begin() as connection:
        if "margin_safety_factor" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN margin_safety_factor FLOAT DEFAULT 0.9"
            )

def _ensure_llm_interaction_variant_column(db_engine: Engine) -> None:
    """Add prompt_variant column to llm_interactions if missing."""
    inspector = inspect(db_engine)
    if "llm_interactions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("llm_interactions")}
    with db_engine.begin() as connection:
        if "prompt_variant" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE llm_interactions ADD COLUMN prompt_variant VARCHAR(100)"
            )

def _bootstrap_credentials(db: Session, credential_model: type, strategy_model: type) -> None:
    from app.core.credential_crypto import encrypt_secret

    credential = db.query(credential_model).order_by(credential_model.id.desc()).first()
    legacy = db.query(strategy_model).order_by(strategy_model.id.desc()).first()

    if credential is None:
        credential = credential_model()
        if legacy is not None and legacy.sct_key:
            credential.sct_key = encrypt_secret(legacy.sct_key)
            legacy.sct_key = ""
        db.add(credential)
        db.commit()
        return

    if not credential.sct_key and legacy is not None and legacy.sct_key:
        credential.sct_key = encrypt_secret(legacy.sct_key)
        legacy.sct_key = ""
        db.add(credential)
        db.commit()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
