from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger("auto_trade.database")

_connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
engine = create_engine(settings.database_url, connect_args=_connect_args)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """Enable SQLite pragmas for concurrent writes from runner thread + FastAPI handlers.

        - journal_mode=WAL: allows concurrent readers + a single writer
        - synchronous=NORMAL: WAL mode default; durable enough for our workload
          (we are not a financial exchange; one fsync per checkpoint is fine)
        - busy_timeout=5000: wait up to 5s for the writer lock instead of raising
          "database is locked" immediately
        - foreign_keys=ON: SQLite ships with FK enforcement disabled by default
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
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
    _ensure_runtime_state_symbol_columns(engine)
    _backfill_primary_runtime_state_symbols(engine)
    _ensure_tracked_entries_table(engine)
    _ensure_audit_log_table(engine)
    _ensure_credential_config_notification_channels_column(engine)
    _ensure_watchlist_items_table(engine)
    _ensure_watchlist_scores_table(engine)
    _ensure_prompt_versions_table(engine)
    _ensure_experiment_results_table(engine)
    _ensure_strategy_experiments_table(engine)
    _ensure_strategy_experiment_runs_table(engine)
    _ensure_strategy_experiment_runs_extra_metrics(engine)
    _ensure_strategy_config_margin_safety_factor(engine)
    _ensure_llm_interaction_variant_column(engine)
    _ensure_report_query_indexes(engine)
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
        # Backfill any NULL daily_pnl_date rows regardless of whether the
        # column was just added (a partial migration may have left NULLs).
        # NOTE: only set the date — do NOT reset daily_pnl / consecutive_losses,
        # which would silently wipe accumulated P&L and the consecutive-loss
        # counter (possibly resuming a strategy that should stay paused).
        connection.exec_driver_sql(
            "UPDATE runtime_state SET daily_pnl_date = DATE('now') WHERE daily_pnl_date IS NULL"
        )
        if "pause_reason" not in columns:
            connection.exec_driver_sql("ALTER TABLE runtime_state ADD COLUMN pause_reason TEXT DEFAULT '' NOT NULL")
        if "paused_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE runtime_state ADD COLUMN paused_at DATETIME")
        if "pause_auto_resumable" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE runtime_state ADD COLUMN pause_auto_resumable BOOLEAN DEFAULT 0 NOT NULL"
            )


def _ensure_runtime_state_symbol_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())

    with db_engine.begin() as connection:
        if "runtime_state" in table_names:
            columns = {column["name"] for column in inspector.get_columns("runtime_state")}
            if "symbol" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state ADD COLUMN symbol VARCHAR(50) DEFAULT '' NOT NULL"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_runtime_state_symbol ON runtime_state (symbol)"
                )
        if "runtime_state_snapshots" in table_names:
            columns = {column["name"] for column in inspector.get_columns("runtime_state_snapshots")}
            if "symbol" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state_snapshots ADD COLUMN symbol VARCHAR(50) DEFAULT '' NOT NULL"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_runtime_state_snapshots_symbol ON runtime_state_snapshots (symbol)"
                )


def _backfill_primary_runtime_state_symbols(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "runtime_state" not in inspector.get_table_names():
        return
    if "strategy_config" not in inspector.get_table_names():
        return

    with db_engine.begin() as connection:
        row = connection.exec_driver_sql(
            "SELECT symbol FROM strategy_config ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None or not row[0]:
            return
        primary_symbol = str(row[0]).strip().upper()
        if not primary_symbol:
            return
        connection.exec_driver_sql(
            """
            UPDATE runtime_state
            SET symbol = ?
            WHERE symbol = ''
              AND NOT EXISTS (
                SELECT 1 FROM runtime_state WHERE symbol = ?
              )
            """,
            (primary_symbol, primary_symbol),
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
                "ALTER TABLE credential_config ADD COLUMN notification_channels TEXT NOT NULL DEFAULT ''"
            )
        connection.exec_driver_sql(
            "UPDATE credential_config SET notification_channels = ? "
            "WHERE notification_channels IS NULL OR notification_channels = ''",
            (DEFAULT_NOTIFICATION_CHANNELS_JSON,),
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


def _ensure_watchlist_scores_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "watchlist_scores" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS watchlist_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) DEFAULT 'US' NOT NULL,
                score FLOAT DEFAULT 0.0 NOT NULL,
                rationale TEXT DEFAULT '' NOT NULL,
                confidence FLOAT DEFAULT 0.0 NOT NULL,
                recommended_action VARCHAR(16) DEFAULT 'HOLD' NOT NULL,
                source VARCHAR(32) DEFAULT 'llm' NOT NULL,
                created_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_watchlist_scores_symbol_created_at "
            "ON watchlist_scores (symbol, created_at)"
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

def _ensure_strategy_experiment_runs_extra_metrics(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "strategy_experiment_runs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("strategy_experiment_runs")}
    missing = {
        "sharpe_ratio": "FLOAT",
        "profit_factor": "FLOAT",
        "profit_loss_ratio": "FLOAT",
    }
    with db_engine.begin() as connection:
        for name, column_type in missing.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE strategy_experiment_runs ADD COLUMN {name} {column_type}"
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


def _ensure_report_query_indexes(db_engine: Engine) -> None:
    """Create indexes for report and query performance if they do not exist."""
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    with db_engine.begin() as connection:
        if "orders" in table_names:
            existing_indexes = {i["name"] for i in inspector.get_indexes("orders")}
            if "ix_orders_symbol_filled_at" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_orders_symbol_filled_at ON orders (symbol, filled_at)"
                )
            if "ix_orders_symbol_created_at" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_orders_symbol_created_at ON orders (symbol, created_at)"
                )
            if "ix_orders_status" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status)"
                )
        if "trade_events" in table_names:
            existing_indexes = {i["name"] for i in inspector.get_indexes("trade_events")}
            if "ix_trade_events_symbol_created_at" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_trade_events_symbol_created_at ON trade_events (symbol, created_at)"
                )
            if "ix_trade_events_event_type" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_trade_events_event_type ON trade_events (event_type)"
                )
        if "llm_interactions" in table_names:
            existing_indexes = {i["name"] for i in inspector.get_indexes("llm_interactions")}
            if "ix_llm_interactions_symbol_created_at" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_llm_interactions_symbol_created_at ON llm_interactions (symbol, created_at)"
                )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
