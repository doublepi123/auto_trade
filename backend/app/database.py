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
    _ensure_strategy_config_report_schedule_columns(engine)
    _ensure_llm_interaction_variant_column(engine)
    _ensure_report_query_indexes(engine)
    _ensure_trade_notes_table(engine)
    _ensure_backtest_runs_table(engine)
    _ensure_alert_rules_table(engine)
    _ensure_alert_firings_table(engine)
    _ensure_strategy_presets_table(engine)
    _ensure_notifications_table(engine)
    _ensure_event_log_table(engine)
    _ensure_portfolio_config_table(engine)
    _ensure_paper_orders_table(engine)
    _ensure_strategy_param_versions_table(engine)
    _ensure_transactions_table(engine)
    _ensure_platform_backtest_runs_table(engine)
    _ensure_factor_snapshots_table(engine)
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


def _ensure_trade_notes_table(db_engine: Engine) -> None:
    """Defensive explicit create for trade_notes.

    ``Base.metadata.create_all`` already creates new tables, but the project
    keeps an explicit ``_ensure_*`` per table/column for runtime migration
    parity (alembic is not used in prod). Idempotent via IF NOT EXISTS.
    """
    inspector = inspect(db_engine)
    if "trade_notes" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS trade_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                rating INTEGER,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_trade_notes_order_id ON trade_notes (order_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_trade_notes_symbol_updated ON trade_notes (symbol, updated_at)"
        )


def _ensure_backtest_runs_table(db_engine: Engine) -> None:
    """Defensive explicit create for backtest_runs (saved runs for comparison)."""
    inspector = inspect(db_engine)
    if "backtest_runs" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_backtest_runs_created_at ON backtest_runs (created_at)"
        )


def _ensure_alert_rules_table(db_engine: Engine) -> None:
    """Defensive explicit create for alert_rules (user-defined alert rules)."""
    inspector = inspect(db_engine)
    if "alert_rules" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                rule_type VARCHAR(24) NOT NULL,
                threshold FLOAT NOT NULL DEFAULT 0,
                severity VARCHAR(16) NOT NULL DEFAULT 'WARNING',
                enabled BOOLEAN NOT NULL DEFAULT 1,
                cooldown_seconds INTEGER NOT NULL DEFAULT 300,
                last_fired_at DATETIME,
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_alert_rules_enabled ON alert_rules (enabled)"
        )


def _ensure_alert_firings_table(db_engine: Engine) -> None:
    """Defensive explicit create for alert_firings (append-only firing log).

    No FK to alert_rules so a deleted rule's firing history survives.
    """
    inspector = inspect(db_engine)
    if "alert_firings" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS alert_firings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                rule_type VARCHAR(24) NOT NULL DEFAULT '',
                threshold FLOAT NOT NULL DEFAULT 0,
                trigger_value FLOAT NOT NULL DEFAULT 0,
                severity VARCHAR(16) NOT NULL DEFAULT 'WARNING',
                message TEXT NOT NULL DEFAULT '',
                fired_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_alert_firings_rule_fired_at ON alert_firings (rule_id, fired_at)"
        )


def _ensure_strategy_presets_table(db_engine: Engine) -> None:
    """Defensive explicit create for strategy_presets."""
    inspector = inspect(db_engine)
    if "strategy_presets" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS strategy_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_strategy_presets_name ON strategy_presets (name)"
        )


def _ensure_notifications_table(db_engine: Engine) -> None:
    """Defensive explicit create for notifications (dispatch-log)."""
    inspector = inspect(db_engine)
    if "notifications" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title VARCHAR(200) NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                severity VARCHAR(16) NOT NULL DEFAULT 'INFO',
                success BOOLEAN NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON notifications (created_at)"
        )


def _ensure_event_log_table(db_engine: Engine) -> None:
    """Defensive explicit create for event_log (platform event store)."""
    inspector = inspect(db_engine)
    if "event_log" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id VARCHAR(36) NOT NULL,
                event_type VARCHAR(32) NOT NULL,
                source VARCHAR(32) NOT NULL,
                symbol VARCHAR(32),
                timestamp DATETIME NOT NULL,
                payload_json TEXT NOT NULL,
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_event_log_event_id ON event_log (event_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_event_log_event_type ON event_log (event_type)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_event_log_symbol ON event_log (symbol)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_event_log_timestamp ON event_log (timestamp)"
        )


def _ensure_portfolio_config_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "portfolio_config" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS portfolio_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                symbols_json TEXT NOT NULL DEFAULT '[]',
                allocations_json TEXT NOT NULL DEFAULT '{}',
                per_symbol_risk_json TEXT NOT NULL DEFAULT '{}',
                rebalance_threshold_pct FLOAT NOT NULL DEFAULT 5.0,
                max_gross_exposure FLOAT NOT NULL DEFAULT 1.0,
                max_net_exposure FLOAT NOT NULL DEFAULT 1.0,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )


def _ensure_paper_orders_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "paper_orders" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_order_id VARCHAR(50) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                quantity INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL DEFAULT 0,
                limit_price FLOAT,
                status VARCHAR(30) NOT NULL DEFAULT 'SUBMITTED',
                intent_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_paper_orders_broker_order_id ON paper_orders (broker_order_id)"
        )


def _ensure_strategy_param_versions_table(db_engine: Engine) -> None:
    """Defensive explicit create for strategy_param_versions.

    Snapshots the tunable strategy params after each successful config save
    so the user can list/rollback. Created explicitly (rather than only via
    metadata.create_all) for parity with the other _ensure_* tables.
    """
    inspector = inspect(db_engine)
    if "strategy_param_versions" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS strategy_param_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                params_json TEXT NOT NULL DEFAULT '{}',
                actor_hash VARCHAR(64),
                created_at DATETIME
            )
            """
        )


def _ensure_transactions_table(db_engine: Engine) -> None:
    """Defensive explicit create for transactions (per-fill ledger).

    Each FillEvent observed by ``TransactionLogger`` becomes one row here;
    the schema mirrors the pyfolio ``transactions`` contract (one row per
    fill with broker id / symbol / side / quantity / price / commission).
    """
    inspector = inspect(db_engine)
    if "transactions" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broker_order_id VARCHAR(50) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                side VARCHAR(20) NOT NULL,
                quantity INTEGER NOT NULL,
                price FLOAT NOT NULL,
                commission FLOAT NOT NULL DEFAULT 0.0,
                source VARCHAR(20) NOT NULL DEFAULT 'paper',
                timestamp DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_symbol ON transactions (symbol)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_broker_order_id ON transactions (broker_order_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_timestamp ON transactions (timestamp)"
        )


def _ensure_platform_backtest_runs_table(db_engine: Engine) -> None:
    """Defensive explicit create for platform_backtest_runs (saved runs)."""
    inspector = inspect(db_engine)
    if "platform_backtest_runs" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS platform_backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL DEFAULT '',
                strategy VARCHAR(50) NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                symbols_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '{}',
                final_nav FLOAT NOT NULL DEFAULT 0.0,
                sharpe FLOAT NOT NULL DEFAULT 0.0,
                created_at DATETIME
            )
            """
        )


def _ensure_factor_snapshots_table(db_engine: Engine) -> None:
    """Defensive explicit create for factor_snapshots + factor_ic_series (P196)."""
    inspector = inspect(db_engine)
    for stmt in (
        """
        CREATE TABLE IF NOT EXISTS factor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factor_name VARCHAR(64) NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            as_of DATETIME NOT NULL,
            factor_value FLOAT NOT NULL,
            forward_return FLOAT,
            horizon_bars INTEGER NOT NULL DEFAULT 1,
            rank INTEGER,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_factor_snapshots_factor_name ON factor_snapshots (factor_name)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_factor_snapshots_symbol ON factor_snapshots (symbol)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_factor_snapshots_as_of ON factor_snapshots (as_of)
        """,
        """
        CREATE TABLE IF NOT EXISTS factor_ic_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factor_name VARCHAR(64) NOT NULL,
            as_of DATETIME NOT NULL,
            mean_ic FLOAT NOT NULL DEFAULT 0.0,
            std_ic FLOAT NOT NULL DEFAULT 0.0,
            ic_ir FLOAT NOT NULL DEFAULT 0.0,
            num_symbols INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_factor_ic_series_factor_name ON factor_ic_series (factor_name)
        """,
    ):
        with db_engine.begin() as connection:
            connection.exec_driver_sql(stmt)


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


def _ensure_strategy_config_report_schedule_columns(db_engine: Engine) -> None:
    """Add the scheduled-report config columns to strategy_config if missing."""
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    with db_engine.begin() as connection:
        if "report_schedule_enabled" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN report_schedule_enabled BOOLEAN NOT NULL DEFAULT 0"
            )
        if "report_schedule_interval_hours" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN report_schedule_interval_hours INTEGER NOT NULL DEFAULT 24"
            )
        if "report_schedule_symbol" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN report_schedule_symbol VARCHAR(50) NOT NULL DEFAULT ''"
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
