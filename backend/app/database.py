from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger("auto_trade.database")

# Ensure the data directory exists before the SQLite engine opens the DB file.
# This was previously done at ``app.config`` import time, which made config
# validation (and any config-only import) create directories as a side effect.
# It now lives here, in the narrowest normal startup module the validation CLI
# does not import, so importing ``app.config`` alone is side-effect-free while
# normal backend startup (which imports ``app.database``) still ensures it.
settings.ensure_data_dir()

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
        - recursive_triggers=ON: conflict-replace deletes must hit immutable-table
          DELETE triggers instead of bypassing append-only protection
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


WATCHLIST_QUANT_V6_TABLE_NAMES = (
    "watchlist_quant_v6_registrations",
    "watchlist_quant_v6_artifacts",
    "watchlist_quant_v6_publications",
    "watchlist_quant_v6_publication_artifacts",
)


_WATCHLIST_QUANT_V6_DUPLICATE_PREDICATES = {
    "watchlist_quant_v6_registrations": (
        "(NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.id)) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE identity_sha256 = NEW.identity_sha256) "
        "OR (NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.id "
        "AND identity_sha256 = NEW.identity_sha256))"
    ),
    "watchlist_quant_v6_artifacts": (
        "EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.digest_sha256) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.digest_sha256 "
        "AND kind = NEW.kind)"
    ),
    "watchlist_quant_v6_publications": (
        "(NEW.id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE id = NEW.id)) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE registration_id = NEW.registration_id) "
        "OR EXISTS (SELECT 1 FROM watchlist_quant_v6_publications "
        "WHERE identity_sha256 = NEW.identity_sha256)"
    ),
    "watchlist_quant_v6_publication_artifacts": (
        "EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publication_artifacts "
        "WHERE publication_id = NEW.publication_id "
        "AND member_ordinal = NEW.member_ordinal "
        "AND role = NEW.role "
        "AND artifact_ordinal = NEW.artifact_ordinal) "
        "OR EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publication_artifacts "
        "WHERE binding_sha256 = NEW.binding_sha256)"
    ),
}


_WATCHLIST_QUANT_V6_REFERENCE_PREDICATES = {
    "watchlist_quant_v6_publications": (
        "NOT EXISTS (SELECT 1 FROM watchlist_quant_v6_registrations "
        "WHERE id = NEW.registration_id "
        "AND identity_sha256 = NEW.registration_identity_sha256 "
        "AND cohort_member_count = NEW.registered_member_count "
        "AND json_type(registration_json, '$.cohort.member_count') = 'integer' "
        "AND json_extract(registration_json, '$.cohort.member_count') "
        "= NEW.registered_member_count "
        "AND json_type(registration_json, '$.cohort.members') = 'array' "
        "AND json_array_length(registration_json, '$.cohort.members') "
        "= NEW.registered_member_count)"
    ),
    "watchlist_quant_v6_publication_artifacts": (
        "NOT EXISTS (SELECT 1 "
        "FROM watchlist_quant_v6_publications AS publication "
        "JOIN watchlist_quant_v6_registrations AS registration "
        "ON registration.id = publication.registration_id "
        "AND registration.identity_sha256 "
        "= publication.registration_identity_sha256 "
        "AND registration.cohort_member_count "
        "= publication.registered_member_count "
        "WHERE publication.id = NEW.publication_id "
        "AND NEW.member_ordinal >= 0 "
        "AND NEW.member_ordinal < publication.registered_member_count "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || ']') = 'object' "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].ordinal') = 'integer' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].ordinal') "
        "= NEW.member_ordinal "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].symbol') = 'text' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].symbol') = NEW.symbol "
        "AND json_type(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].market') = 'text' "
        "AND json_extract(registration.registration_json, "
        "'$.cohort.members[' || NEW.member_ordinal || '].market') = NEW.market) "
        "OR NOT EXISTS (SELECT 1 FROM watchlist_quant_v6_artifacts "
        "WHERE digest_sha256 = NEW.artifact_sha256 "
        "AND kind = NEW.artifact_kind)"
    ),
}


def _normalize_sqlite_ddl(value: str) -> str:
    return " ".join(value.strip().removesuffix(";").split())


def _sqlite_type_signature(column_type: object, db_engine: Engine) -> str:
    compile_type = getattr(column_type, "compile", None)
    if not callable(compile_type):
        return str(column_type).upper()
    return " ".join(
        str(compile_type(dialect=db_engine.dialect)).upper().split()
    )


def _sqlite_default_signature(value: object | None) -> str | None:
    if value is None:
        return None
    return _normalize_sqlite_ddl(str(value)).upper()


def _watchlist_quant_v6_trigger_definitions() -> dict[str, tuple[str, str]]:
    definitions: dict[str, tuple[str, str]] = {}
    for table_name in WATCHLIST_QUANT_V6_TABLE_NAMES:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_no_{operation.lower()}"
            definitions[trigger_name] = (
                table_name,
                f"CREATE TRIGGER {trigger_name} "
                f"BEFORE {operation} ON {table_name} "
                "BEGIN "
                f"SELECT RAISE(ABORT, '{table_name} is append-only'); "
                "END",
            )
        duplicate_trigger = f"trg_{table_name}_no_duplicate_key"
        definitions[duplicate_trigger] = (
            table_name,
            f"CREATE TRIGGER {duplicate_trigger} "
            f"BEFORE INSERT ON {table_name} "
            f"WHEN {_WATCHLIST_QUANT_V6_DUPLICATE_PREDICATES[table_name]} "
            "BEGIN "
            f"SELECT RAISE(ABORT, '{table_name} duplicate key'); "
            "END",
        )
        reference_predicate = _WATCHLIST_QUANT_V6_REFERENCE_PREDICATES.get(
            table_name
        )
        if reference_predicate is not None:
            reference_trigger = f"trg_{table_name}_validate_reference"
            definitions[reference_trigger] = (
                table_name,
                f"CREATE TRIGGER {reference_trigger} "
                f"BEFORE INSERT ON {table_name} "
                f"WHEN {reference_predicate} "
                "BEGIN "
                f"SELECT RAISE(ABORT, '{table_name} invalid reference'); "
                "END",
            )
    return definitions


WATCHLIST_QUANT_V6_TRIGGER_NAMES = tuple(
    _watchlist_quant_v6_trigger_definitions()
)


def init_db() -> None:
    from app.models import Base, CredentialConfig, StrategyConfig

    Base.metadata.create_all(bind=engine)
    _ensure_order_execution_columns(engine)
    _ensure_order_raw_response_column(engine)
    _ensure_order_execution_ledger_columns(engine)
    _ensure_strategy_config_llm_columns(engine)
    _ensure_strategy_config_trade_safety_columns(engine)
    _ensure_strategy_config_session_columns(engine)
    _ensure_drawdown_columns(engine)
    _ensure_runtime_state_daily_pnl_date_column(engine)
    _ensure_runtime_state_symbol_columns(engine)
    _ensure_runtime_reduction_columns(engine)
    _ensure_reconciliation_gate(engine)
    _ensure_reconciliation_evidence(engine)
    _backfill_primary_runtime_state_symbols(engine)
    _ensure_runtime_state_symbol_uniqueness(engine)
    _ensure_runtime_state_entry_rearm_column(engine)
    _ensure_order_broker_id_uniqueness(engine)
    _ensure_order_terminal_callbacks_table(engine)
    _ensure_reconciliation_incidents_table(engine)
    _ensure_trade_event_source_event_key(engine)
    _ensure_tracked_entries_table(engine)
    _ensure_tracked_entry_metadata_columns(engine)
    _ensure_audit_log_table(engine)
    _ensure_credential_config_notification_channels_column(engine)
    _ensure_watchlist_items_table(engine)
    _ensure_watchlist_item_source_column(engine)
    _ensure_universe_selection_tables(engine)
    _normalize_universe_selection_run_timestamps(engine)
    _ensure_watchlist_scores_table(engine)
    _ensure_watchlist_quant_v6_tables(engine)
    _ensure_prompt_versions_table(engine)
    _ensure_experiment_results_table(engine)
    _ensure_strategy_experiments_table(engine)
    _ensure_strategy_experiment_runs_table(engine)
    _ensure_strategy_experiment_runs_extra_metrics(engine)
    _ensure_strategy_config_margin_safety_factor(engine)
    _ensure_strategy_config_p0_safety_columns(engine)
    _ensure_strategy_config_report_schedule_columns(engine)
    _ensure_strategy_v2_shadow_tables(engine)
    _ensure_opening_momentum_shadow_table(engine)
    _ensure_opening_activity_observation_table(engine)
    _ensure_opening_momentum_execution_table(engine)
    _ensure_llm_interaction_variant_column(engine)
    _ensure_llm_interaction_token_columns(engine)
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
    _ensure_decision_funnel_session_summaries_table(engine)
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


def _ensure_order_execution_ledger_columns(db_engine: Engine) -> None:
    """Add the immutable execution-context and cost fields used by P1."""
    inspector = inspect(db_engine)
    if "orders" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("orders")}
    missing_columns = {
        "decision_at": "DATETIME",
        "decision_bid": "FLOAT",
        "decision_ask": "FLOAT",
        "decision_spread": "FLOAT",
        "decision_spread_bps": "FLOAT",
        "quote_age_ms": "FLOAT",
        "config_version": "VARCHAR(64) DEFAULT '' NOT NULL",
        "config_snapshot": "TEXT DEFAULT '{}' NOT NULL",
        "submit_started_at": "DATETIME",
        "acknowledged_at": "DATETIME",
        "broker_submitted_at": "DATETIME",
        "broker_updated_at": "DATETIME",
        "submit_latency_ms": "FLOAT",
        "ack_latency_ms": "FLOAT",
        "fill_latency_ms": "FLOAT",
        "estimated_fee": "FLOAT",
        "actual_fee": "FLOAT",
        "fee_currency": "VARCHAR(10) DEFAULT '' NOT NULL",
        "fee_source": "VARCHAR(20) DEFAULT 'UNKNOWN' NOT NULL",
        "slippage_amount": "FLOAT",
        "slippage_bps": "FLOAT",
        "exit_cause": "VARCHAR(50) DEFAULT '' NOT NULL",
        "exit_reason": "TEXT DEFAULT '' NOT NULL",
        "gross_pnl": "FLOAT",
        "net_pnl": "FLOAT",
        "pnl_source": "VARCHAR(30) DEFAULT 'UNKNOWN' NOT NULL",
        "cost_basis_price": "FLOAT",
        "cost_basis_quantity": "FLOAT",
        "cost_basis_opened_at": "DATETIME",
        "position_quantity_before": "FLOAT",
        "pnl_fee": "FLOAT",
        "pnl_fee_source": "VARCHAR(20) DEFAULT 'UNKNOWN' NOT NULL",
        "pnl_fee_rate": "FLOAT",
        "mfe_amount": "FLOAT",
        "mae_amount": "FLOAT",
        "mfe_pct": "FLOAT",
        "mae_pct": "FLOAT",
    }
    with db_engine.begin() as connection:
        for name, column_type in missing_columns.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE orders ADD COLUMN {name} {column_type}"
                )

        # Freeze a best-effort cost for legacy fills. Future reads must not
        # rewrite history merely because the active strategy fee rate changed.
        strategy_columns = (
            {column["name"] for column in inspector.get_columns("strategy_config")}
            if "strategy_config" in inspector.get_table_names()
            else set()
        )
        fee_us = (
            connection.execute(
                text("SELECT fee_rate_us FROM strategy_config ORDER BY id DESC LIMIT 1")
            ).scalar()
            if "fee_rate_us" in strategy_columns
            else None
        )
        fee_hk = (
            connection.execute(
                text("SELECT fee_rate_hk FROM strategy_config ORDER BY id DESC LIMIT 1")
            ).scalar()
            if "fee_rate_hk" in strategy_columns
            else None
        )
        us_rate = float(fee_us) if fee_us is not None else 0.0005
        hk_rate = float(fee_hk) if fee_hk is not None else 0.003
        connection.execute(
            text(
                "UPDATE orders SET estimated_fee = "
                "ABS(COALESCE(executed_price, price) * "
                "COALESCE(executed_quantity, quantity) * "
                "CASE WHEN UPPER(symbol) LIKE '%.HK' THEN :hk ELSE :us END), "
                "fee_source = 'ESTIMATED' "
                "WHERE estimated_fee IS NULL AND actual_fee IS NULL "
                "AND UPPER(status) IN ('FILLED', 'PARTIAL_FILLED')"
            ),
            {"us": us_rate, "hk": hk_rate},
        )


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


def _ensure_drawdown_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    with db_engine.begin() as connection:
        if "strategy_config" in table_names:
            strategy_columns = {
                column["name"]
                for column in inspector.get_columns("strategy_config")
            }
            if "max_drawdown_amount" not in strategy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE strategy_config ADD COLUMN max_drawdown_amount FLOAT"
                )
        if "runtime_state" in table_names:
            runtime_columns = {
                column["name"]
                for column in inspector.get_columns("runtime_state")
            }
            if "cumulative_realized_pnl" not in runtime_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state ADD COLUMN "
                    "cumulative_realized_pnl FLOAT DEFAULT 0 NOT NULL"
                )
            if "peak_realized_pnl" not in runtime_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state ADD COLUMN "
                    "peak_realized_pnl FLOAT DEFAULT 0 NOT NULL"
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


def _ensure_runtime_state_entry_rearm_column(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "runtime_state" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    if "long_entry_rearm_required" not in columns:
        with db_engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE runtime_state ADD COLUMN "
                "long_entry_rearm_required BOOLEAN DEFAULT 0 NOT NULL"
            )
            predicates: list[str] = []
            if "engine_state" in columns:
                predicates.append(
                    "LOWER(COALESCE(engine_state, 'flat')) IN ('flat', 'long')"
                )
            if predicates:
                connection.exec_driver_sql(
                    "UPDATE runtime_state SET long_entry_rearm_required = 1 WHERE "
                    + " OR ".join(predicates)
                )


def _ensure_runtime_reduction_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if "runtime_state" in table_names:
        columns = {column["name"] for column in inspector.get_columns("runtime_state")}
        missing = {
            "execution_state": "VARCHAR(20) NOT NULL DEFAULT 'IDLE'",
            "reduction_action": "VARCHAR(20) NOT NULL DEFAULT ''",
            "reduction_cause": "VARCHAR(30) NOT NULL DEFAULT ''",
            "reduction_reason": "TEXT NOT NULL DEFAULT ''",
            "reduction_started_at": "DATETIME",
            "reduction_trigger_price": "FLOAT",
        }
        with db_engine.begin() as connection:
            for name, column_type in missing.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE runtime_state ADD COLUMN {name} {column_type}"
                    )
    if "runtime_state_snapshots" in table_names:
        columns = {
            column["name"] for column in inspector.get_columns("runtime_state_snapshots")
        }
        with db_engine.begin() as connection:
            if "execution_state" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state_snapshots ADD COLUMN "
                    "execution_state VARCHAR(20) NOT NULL DEFAULT 'IDLE'"
                )
            if "reduction_reason" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE runtime_state_snapshots ADD COLUMN "
                    "reduction_reason TEXT NOT NULL DEFAULT ''"
                )


def _ensure_reconciliation_gate(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "runtime_state" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("runtime_state")}
    with db_engine.begin() as connection:
        if "reconciliation_gate" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE runtime_state ADD COLUMN "
                "reconciliation_gate VARCHAR(16) DEFAULT 'pending' NOT NULL"
            )


def _ensure_reconciliation_evidence(db_engine: Engine) -> None:
    """Defensive explicit create for reconciliation_evidence (P0.2 audit log).

    One row per reconciliation pass (or per drift event).  The ``passed``
    flag records whether the gate allowed the strategy to proceed.
    """
    inspector = inspect(db_engine)
    if "reconciliation_evidence" in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                event_type VARCHAR(32) NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                operator VARCHAR(64),
                snapshot_id VARCHAR(64),
                position_count INTEGER,
                order_count INTEGER,
                drift_summary TEXT,
                passed BOOLEAN NOT NULL DEFAULT 0
            )
            """
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


def _ensure_runtime_state_symbol_uniqueness(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "runtime_state" not in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        duplicate_symbols = connection.execute(
            text(
                "SELECT symbol FROM runtime_state GROUP BY symbol HAVING COUNT(*) > 1"
            )
        ).scalars().all()
        for symbol in duplicate_symbols:
            rows = connection.execute(
                text("SELECT * FROM runtime_state WHERE symbol = :symbol ORDER BY id"),
                {"symbol": symbol},
            ).mappings().all()
            if not rows:
                continue

            def row_key(mapped: Any) -> tuple[int, int, int, str, int]:
                return (
                    int(str(mapped["execution_state"]).upper() == "REDUCING"),
                    int(bool(mapped["kill_switch"])),
                    int(bool(mapped["paused"])),
                    str(mapped["updated_at"] or ""),
                    int(mapped["id"]),
                )

            keeper = max(rows, key=row_key)
            reduction_rows = [
                row for row in rows if str(row["execution_state"]).upper() == "REDUCING"
            ]
            reduction = max(reduction_rows, key=row_key) if reduction_rows else None
            latest = max(rows, key=lambda row: (str(row["updated_at"] or ""), int(row["id"])))
            nonflat_states = {
                str(row["engine_state"]).lower()
                for row in rows
                if str(row["engine_state"]).lower() != "flat"
            }
            paused = any(bool(row["paused"]) for row in rows)
            pause_reason = str(keeper["pause_reason"] or "")
            if len(nonflat_states) > 1:
                paused = True
                pause_reason = (
                    "POSITION_RECONCILIATION_UNCERTAIN: conflicting duplicate "
                    f"runtime states for {symbol}"
                )
            update_values = {
                "id": int(keeper["id"]),
                "engine_state": str(keeper["engine_state"]),
                "paused": int(paused),
                "pause_reason": pause_reason,
                "paused_at": keeper["paused_at"],
                "pause_auto_resumable": int(
                    paused
                    and all(
                        bool(row["pause_auto_resumable"])
                        for row in rows
                        if bool(row["paused"])
                    )
                ),
                "kill_switch": int(any(bool(row["kill_switch"]) for row in rows)),
                "daily_pnl": min(float(row["daily_pnl"] or 0) for row in rows),
                "daily_pnl_date": max(
                    (row["daily_pnl_date"] for row in rows if row["daily_pnl_date"] is not None),
                    default=None,
                ),
                "consecutive_losses": max(int(row["consecutive_losses"] or 0) for row in rows),
                "last_price": float(latest["last_price"] or 0),
                "last_trigger_price": float(latest["last_trigger_price"] or 0),
                "last_trigger_at": latest["last_trigger_at"],
                "execution_state": str(reduction["execution_state"] if reduction else "IDLE"),
                "reduction_action": str(reduction["reduction_action"] if reduction else ""),
                "reduction_cause": str(reduction["reduction_cause"] if reduction else ""),
                "reduction_reason": str(reduction["reduction_reason"] if reduction else ""),
                "reduction_started_at": reduction["reduction_started_at"] if reduction else None,
                "reduction_trigger_price": reduction["reduction_trigger_price"] if reduction else None,
                "updated_at": latest["updated_at"],
            }
            connection.execute(
                text(
                    """
                    UPDATE runtime_state SET
                        engine_state=:engine_state, paused=:paused,
                        pause_reason=:pause_reason, paused_at=:paused_at,
                        pause_auto_resumable=:pause_auto_resumable,
                        kill_switch=:kill_switch, daily_pnl=:daily_pnl,
                        daily_pnl_date=:daily_pnl_date,
                        consecutive_losses=:consecutive_losses,
                        last_price=:last_price,
                        last_trigger_price=:last_trigger_price,
                        last_trigger_at=:last_trigger_at,
                        execution_state=:execution_state,
                        reduction_action=:reduction_action,
                        reduction_cause=:reduction_cause,
                        reduction_reason=:reduction_reason,
                        reduction_started_at=:reduction_started_at,
                        reduction_trigger_price=:reduction_trigger_price,
                        updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                update_values,
            )
            for row in rows:
                if int(row["id"]) != int(keeper["id"]):
                    connection.execute(
                        text("DELETE FROM runtime_state WHERE id = :id"),
                        {"id": int(row["id"])},
                    )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_state_symbol "
            "ON runtime_state (symbol)"
        )


def _ensure_order_broker_id_uniqueness(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "orders" not in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        duplicate_ids = connection.execute(
            text(
                "SELECT broker_order_id FROM orders WHERE broker_order_id <> '' "
                "GROUP BY broker_order_id HAVING COUNT(*) > 1"
            )
        ).scalars().all()
        status_rank = {
            "FILLED": 5,
            "CANCELLED": 4,
            "REJECTED": 4,
            "PARTIAL_FILLED": 3,
            "SUBMITTED": 2,
        }
        for broker_order_id in duplicate_ids:
            rows = connection.execute(
                text("SELECT * FROM orders WHERE broker_order_id = :order_id ORDER BY id"),
                {"order_id": broker_order_id},
            ).mappings().all()
            if not rows:
                continue
            keeper = max(
                rows,
                key=lambda row: (
                    status_rank.get(str(row["status"]).upper(), 1),
                    float(row["executed_quantity"] or 0),
                    int(row["id"]),
                ),
            )
            fill_row = max(
                rows,
                key=lambda row: (float(row["executed_quantity"] or 0), int(row["id"])),
            )
            merged = {
                "id": int(keeper["id"]),
                "symbol": str(keeper["symbol"]),
                "side": str(keeper["side"]),
                "quantity": max(float(row["quantity"] or 0) for row in rows),
                "price": float(keeper["price"] or 0),
                "executed_quantity": max(
                    (float(row["executed_quantity"] or 0) for row in rows),
                    default=0.0,
                ) or None,
                "executed_price": float(fill_row["executed_price"] or 0) or None,
                "status": str(keeper["status"]),
                "created_at": min(
                    (row["created_at"] for row in rows if row["created_at"] is not None),
                    default=None,
                ),
                "filled_at": max(
                    (row["filled_at"] for row in rows if row["filled_at"] is not None),
                    default=None,
                ),
                "raw_response": keeper["raw_response"],
            }
            connection.execute(
                text(
                    """
                    UPDATE orders SET symbol=:symbol, side=:side,
                        quantity=:quantity, price=:price,
                        executed_quantity=:executed_quantity,
                        executed_price=:executed_price, status=:status,
                        created_at=:created_at, filled_at=:filled_at,
                        raw_response=:raw_response
                    WHERE id=:id
                    """
                ),
                merged,
            )
            for row in rows:
                if int(row["id"]) != int(keeper["id"]):
                    connection.execute(
                        text("DELETE FROM orders WHERE id = :id"),
                        {"id": int(row["id"])},
                    )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_broker_order_id_nonempty "
            "ON orders (broker_order_id) WHERE broker_order_id <> ''"
        )


def _ensure_trade_event_source_event_key(db_engine: Engine) -> None:
    """Add a database-enforced idempotency key for external event evidence.

    Most trade events are local observations and leave the key empty. Historical
    broker executions use a provider/account/trade-bound SHA-256 key so two
    concurrent imports cannot persist the same immutable execution twice.
    Existing non-empty duplicates are never guessed or merged.
    """
    inspector = inspect(db_engine)
    if "trade_events" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("trade_events")
    }
    with db_engine.begin() as connection:
        if "source_event_key" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE trade_events ADD COLUMN source_event_key "
                "VARCHAR(64) DEFAULT '' NOT NULL"
            )
        duplicate_keys = connection.execute(
            text(
                "SELECT source_event_key FROM trade_events "
                "WHERE source_event_key <> '' GROUP BY source_event_key "
                "HAVING COUNT(*) > 1"
            )
        ).scalars().all()
        if duplicate_keys:
            raise RuntimeError(
                "trade_events contains duplicate non-empty source_event_key "
                "values; refusing to guess historical execution identity"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_trade_events_source_event_key_nonempty "
            "ON trade_events (source_event_key) "
            "WHERE source_event_key <> ''"
        )


def _ensure_order_terminal_callbacks_table(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS order_terminal_callbacks (
                broker_order_id VARCHAR(100) NOT NULL,
                terminal_status VARCHAR(30) NOT NULL,
                state VARCHAR(20) NOT NULL DEFAULT 'PROCESSING',
                attempt_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                completed_at DATETIME,
                PRIMARY KEY (broker_order_id, terminal_status)
            )
            """
        )


def _ensure_reconciliation_incidents_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_exists = "reconciliation_incidents" in inspector.get_table_names()
    columns = (
        {
            column["name"]
            for column in inspector.get_columns("reconciliation_incidents")
        }
        if table_exists
        else set()
    )
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_incidents (
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
                sdk_error_code VARCHAR(100) NOT NULL DEFAULT '',
                sdk_error_category VARCHAR(100) NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                probe_duration_ms FLOAT,
                exit_code INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0,
                stderr TEXT NOT NULL DEFAULT '',
                CONSTRAINT ux_reconciliation_incident_key
                    UNIQUE (source, failure_category, symbols_json)
            )
            """
        )
        if table_exists:
            missing_columns = {
                "sdk_error_code": "VARCHAR(100) NOT NULL DEFAULT ''",
                "sdk_error_category": "VARCHAR(100) NOT NULL DEFAULT ''",
                "error_message": "TEXT NOT NULL DEFAULT ''",
                "probe_duration_ms": "FLOAT",
                "exit_code": "INTEGER",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "stderr": "TEXT NOT NULL DEFAULT ''",
            }
            for name, column_type in missing_columns.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE reconciliation_incidents ADD COLUMN "
                        f"{name} {column_type}"
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
                side VARCHAR(10) NOT NULL DEFAULT 'LONG',
                quantity FLOAT NOT NULL DEFAULT 0,
                cost FLOAT NOT NULL DEFAULT 0,
                opened_at DATETIME,
                updated_at DATETIME
            )
            """
        )


def _ensure_tracked_entry_metadata_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "tracked_entries" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("tracked_entries")}
    with db_engine.begin() as connection:
        if "side" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE tracked_entries ADD COLUMN side VARCHAR(10) NOT NULL DEFAULT ''"
            )
        if "opened_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE tracked_entries ADD COLUMN opened_at DATETIME"
            )
        connection.exec_driver_sql(
            "UPDATE tracked_entries SET opened_at = updated_at "
            "WHERE opened_at IS NULL AND updated_at IS NOT NULL"
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


def _ensure_decision_funnel_session_summaries_table(db_engine: Engine) -> None:
    """Defensive explicit create for decision_funnel_session_summaries.

    One row per exchange-local trading day per primary symbol with the
    live-path decision-funnel counters; the durable evidence for the
    multi-session zero-order diagnosis. Idempotent via IF NOT EXISTS so an
    existing deployment database gains the table without manual migration.
    """
    inspector = inspect(db_engine)
    if "decision_funnel_session_summaries" in inspector.get_table_names():
        _ensure_decision_funnel_suppression_columns(db_engine)
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS decision_funnel_session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date DATE NOT NULL,
                symbol VARCHAR(50) NOT NULL DEFAULT '',
                market VARCHAR(10) NOT NULL DEFAULT '',
                primary_quotes_seen INTEGER NOT NULL DEFAULT 0,
                quality_rejections INTEGER NOT NULL DEFAULT 0,
                quality_rejections_json TEXT NOT NULL DEFAULT '{}',
                entry_crossing_blocks INTEGER NOT NULL DEFAULT 0,
                fresh_primary_quote INTEGER NOT NULL DEFAULT 0,
                evaluations INTEGER NOT NULL DEFAULT 0,
                threshold_crossings INTEGER NOT NULL DEFAULT 0,
                triggers INTEGER NOT NULL DEFAULT 0,
                sized_quantity_positive INTEGER NOT NULL DEFAULT 0,
                submit_attempts INTEGER NOT NULL DEFAULT 0,
                broker_acks INTEGER NOT NULL DEFAULT 0,
                persisted INTEGER NOT NULL DEFAULT 0,
                pre_submit_risk_check_invocations INTEGER NOT NULL DEFAULT 0,
                skips_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_decision_funnel_session_summaries_session_symbol "
            "ON decision_funnel_session_summaries (session_date, symbol)"
        )


def _ensure_decision_funnel_suppression_columns(db_engine: Engine) -> None:
    """Backfill the suppression counters onto an existing summaries table.

    A deployment created before these counters existed keeps its rows; the new
    columns default to 0/{} so historical sessions read as "not measured"
    rather than "measured zero".
    """
    inspector = inspect(db_engine)
    if "decision_funnel_session_summaries" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("decision_funnel_session_summaries")
    }
    missing = {
        "primary_quotes_seen": "INTEGER NOT NULL DEFAULT 0",
        "quality_rejections": "INTEGER NOT NULL DEFAULT 0",
        "quality_rejections_json": "TEXT NOT NULL DEFAULT '{}'",
        "entry_crossing_blocks": "INTEGER NOT NULL DEFAULT 0",
    }
    with db_engine.begin() as connection:
        for name, column_type in missing.items():
            if name not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE decision_funnel_session_summaries "
                    f"ADD COLUMN {name} {column_type}"
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
    existing = inspector.get_table_names()
    if "factor_snapshots" in existing and "factor_ic_series" in existing:
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
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
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_factor_snapshots_factor_name ON factor_snapshots (factor_name)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_factor_snapshots_symbol ON factor_snapshots (symbol)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_factor_snapshots_as_of ON factor_snapshots (as_of)"
        )
        connection.exec_driver_sql(
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
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_factor_ic_series_factor_name ON factor_ic_series (factor_name)"
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
                source VARCHAR(32) DEFAULT 'manual' NOT NULL,
                is_active BOOLEAN DEFAULT 0 NOT NULL,
                created_at DATETIME
            )
            """
        )


def _ensure_watchlist_item_source_column(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "watchlist_items" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("watchlist_items")
    }
    if "source" in columns:
        return
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE watchlist_items "
            "ADD COLUMN source VARCHAR(32) DEFAULT 'manual' NOT NULL"
        )


def _ensure_universe_selection_tables(db_engine: Engine) -> None:
    """Defensively create the universe-selection audit tables and indexes."""

    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS universe_selection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of_date DATE NOT NULL,
                algorithm_version VARCHAR(100) NOT NULL,
                source_version VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'RUNNING' NOT NULL,
                candidate_count INTEGER DEFAULT 0 NOT NULL,
                evaluable_count INTEGER DEFAULT 0 NOT NULL,
                selected_count INTEGER DEFAULT 0 NOT NULL,
                coverage_ratio FLOAT DEFAULT 0.0 NOT NULL,
                parameters_json TEXT DEFAULT '{}' NOT NULL,
                error TEXT DEFAULT '' NOT NULL,
                started_at DATETIME NOT NULL,
                completed_at DATETIME,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_universe_selection_run_identity UNIQUE (as_of_date, algorithm_version, source_version)
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_universe_selection_runs_created_at "
            "ON universe_selection_runs (created_at)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS universe_selection_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                market VARCHAR(10) DEFAULT 'US' NOT NULL,
                alias VARCHAR(100) DEFAULT '' NOT NULL,
                sector VARCHAR(100) DEFAULT '' NOT NULL,
                memberships_json TEXT DEFAULT '[]' NOT NULL,
                selected BOOLEAN DEFAULT 0 NOT NULL,
                rank INTEGER,
                score FLOAT DEFAULT 0.0 NOT NULL,
                metrics_json TEXT DEFAULT '{}' NOT NULL,
                exclusion_reasons_json TEXT DEFAULT '[]' NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_universe_selection_candidate_symbol UNIQUE (run_id, symbol),
                FOREIGN KEY (run_id) REFERENCES universe_selection_runs (id) ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_universe_selection_candidates_run_id "
            "ON universe_selection_candidates (run_id)"
        )


def _normalize_universe_selection_run_timestamps(
    db_engine: Engine,
) -> None:
    inspector = inspect(db_engine)
    if "universe_selection_runs" not in inspector.get_table_names():
        return
    with db_engine.begin() as connection:
        result = connection.execute(
            text(
                "UPDATE universe_selection_runs "
                "SET completed_at = started_at "
                "WHERE completed_at IS NOT NULL "
                "AND completed_at < started_at"
            )
        )
    if result.rowcount > 0:
        logger.info(
            "normalized %s universe selection run timestamps",
            result.rowcount,
        )


def _ensure_watchlist_scores_table(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_exists = "watchlist_scores" in inspector.get_table_names()
    columns = (
        {
            str(column["name"])
            for column in inspector.get_columns("watchlist_scores")
        }
        if table_exists
        else set()
    )
    with db_engine.begin() as connection:
        if not table_exists:
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
                    estimated_round_trip_cost_bps FLOAT,
                    created_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL
                )
                """
            )
        elif "estimated_round_trip_cost_bps" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE watchlist_scores ADD COLUMN "
                "estimated_round_trip_cost_bps FLOAT"
            )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_watchlist_scores_symbol_created_at "
            "ON watchlist_scores (symbol, created_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS "
            "ix_watchlist_scores_symbol_source_created_at "
            "ON watchlist_scores (symbol, source, created_at)"
        )


def _watchlist_quant_v6_schema_issues(
    db_engine: Engine,
    *,
    require_triggers: bool = True,
) -> tuple[str, ...]:
    """Return fail-closed differences from the frozen B1 SQLite signature."""
    from app.models import Base

    if db_engine.dialect.name != "sqlite":
        return ("watchlist quant-v6 storage requires SQLite",)

    inspector = inspect(db_engine)
    actual_table_names = set(inspector.get_table_names())
    issues: list[str] = []
    for table_name in WATCHLIST_QUANT_V6_TABLE_NAMES:
        if table_name not in actual_table_names:
            issues.append(f"missing table {table_name}")
            continue

        expected_table = Base.metadata.tables[table_name]
        expected_columns = tuple(expected_table.columns.keys())
        actual_column_rows = inspector.get_columns(table_name)
        actual_columns = tuple(
            str(column["name"]) for column in actual_column_rows
        )
        if actual_columns != expected_columns:
            issues.append(
                f"{table_name} columns differ: "
                f"expected {expected_columns}, found {actual_columns}"
            )
        actual_columns_by_name = {
            str(column["name"]): column for column in actual_column_rows
        }
        for expected_column in expected_table.columns:
            actual_column = actual_columns_by_name.get(expected_column.name)
            if actual_column is None:
                continue
            expected_type = _sqlite_type_signature(
                expected_column.type,
                db_engine,
            )
            actual_type = _sqlite_type_signature(
                actual_column["type"],
                db_engine,
            )
            if actual_type != expected_type:
                issues.append(
                    f"{table_name} column {expected_column.name} type "
                    f"differs: expected {expected_type}, found {actual_type}"
                )
            expected_nullable = bool(expected_column.nullable)
            actual_nullable = bool(actual_column.get("nullable"))
            if actual_nullable != expected_nullable:
                issues.append(
                    f"{table_name} column {expected_column.name} "
                    "nullability differs: "
                    f"expected {expected_nullable}, found {actual_nullable}"
                )
            expected_server_default = (
                getattr(expected_column.server_default, "arg", None)
                if expected_column.server_default is not None
                else None
            )
            expected_default = _sqlite_default_signature(
                expected_server_default
            )
            actual_default = _sqlite_default_signature(
                actual_column.get("default")
            )
            if actual_default != expected_default:
                issues.append(
                    f"{table_name} column {expected_column.name} "
                    "server default differs: "
                    f"expected {expected_default}, found {actual_default}"
                )

        expected_primary_key = tuple(
            column.name for column in expected_table.primary_key.columns
        )
        actual_primary_key = tuple(
            inspector.get_pk_constraint(table_name).get(
                "constrained_columns"
            )
            or ()
        )
        if actual_primary_key != expected_primary_key:
            issues.append(
                f"{table_name} primary key differs: "
                f"expected {expected_primary_key}, found {actual_primary_key}"
            )

        expected_checks = {
            str(constraint.name): " ".join(
                str(constraint.sqltext).upper().split()
            )
            for constraint in expected_table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name is not None
        }
        actual_checks = {
            str(constraint["name"]): " ".join(
                str(constraint.get("sqltext") or "").upper().split()
            )
            for constraint in inspector.get_check_constraints(table_name)
            if constraint.get("name") is not None
        }
        for constraint_name, expected_check_sql in expected_checks.items():
            if actual_checks.get(constraint_name) != expected_check_sql:
                issues.append(
                    f"{table_name} check constraint {constraint_name} "
                    "does not match the frozen predicate"
                )
        unexpected_checks = set(actual_checks) - set(expected_checks)
        if unexpected_checks:
            issues.append(
                f"{table_name} has unexpected checks "
                f"{sorted(unexpected_checks)}"
            )

        expected_uniques = {
            str(constraint.name): tuple(
                column.name for column in constraint.columns
            )
            for constraint in expected_table.constraints
            if isinstance(constraint, UniqueConstraint)
            and constraint.name is not None
        }
        actual_uniques = {
            str(constraint["name"]): tuple(
                constraint.get("column_names") or ()
            )
            for constraint in inspector.get_unique_constraints(table_name)
            if constraint.get("name") is not None
        }
        for constraint_name, expected_unique_columns in expected_uniques.items():
            if actual_uniques.get(constraint_name) != expected_unique_columns:
                issues.append(
                    f"{table_name} unique constraint {constraint_name} "
                    f"does not cover {expected_unique_columns}"
                )
        unexpected_uniques = set(actual_uniques) - set(expected_uniques)
        if unexpected_uniques:
            issues.append(
                f"{table_name} has unexpected unique constraints "
                f"{sorted(unexpected_uniques)}"
            )

        expected_indexes = {
            str(index.name): tuple(column.name for column in index.columns)
            for index in expected_table.indexes
            if index.name is not None
        }
        actual_indexes = {
            str(index["name"]): tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
            if index.get("name") is not None
        }
        for index_name, expected_index_columns in expected_indexes.items():
            if actual_indexes.get(index_name) != expected_index_columns:
                issues.append(
                    f"{table_name} index {index_name} "
                    f"does not cover {expected_index_columns}"
                )
        unexpected_indexes = set(actual_indexes) - set(expected_indexes)
        if unexpected_indexes:
            issues.append(
                f"{table_name} has unexpected indexes "
                f"{sorted(unexpected_indexes)}"
            )

        expected_foreign_keys = set()
        for constraint in expected_table.foreign_key_constraints:
            elements = tuple(constraint.elements)
            expected_foreign_keys.add((
                tuple(constraint.column_keys),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
                str(constraint.ondelete or "").upper(),
            ))
        actual_foreign_keys = {
            (
                tuple(constraint.get("constrained_columns") or ()),
                str(constraint.get("referred_table") or ""),
                tuple(constraint.get("referred_columns") or ()),
                str(
                    (constraint.get("options") or {}).get("ondelete")
                    or ""
                ).upper(),
            )
            for constraint in inspector.get_foreign_keys(table_name)
        }
        missing_foreign_keys = expected_foreign_keys - actual_foreign_keys
        if missing_foreign_keys:
            issues.append(
                f"{table_name} missing foreign keys "
                f"{sorted(missing_foreign_keys)}"
            )
        unexpected_foreign_keys = (
            actual_foreign_keys - expected_foreign_keys
        )
        if unexpected_foreign_keys:
            issues.append(
                f"{table_name} has unexpected foreign keys "
                f"{sorted(unexpected_foreign_keys)}"
            )

    if require_triggers:
        expected_triggers = _watchlist_quant_v6_trigger_definitions()
        with db_engine.connect() as connection:
            actual_triggers = {
                str(row[0]): (str(row[1]), str(row[2] or ""))
                for row in connection.exec_driver_sql(
                    "SELECT name, tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE "
                    "'trg_watchlist_quant_v6_%'"
                )
            }
        missing_triggers = set(expected_triggers) - set(actual_triggers)
        if missing_triggers:
            issues.append(
                "missing immutable-table triggers "
                f"{sorted(missing_triggers)}"
            )
        unexpected_triggers = set(actual_triggers) - set(expected_triggers)
        if unexpected_triggers:
            issues.append(
                "unexpected immutable-table triggers "
                f"{sorted(unexpected_triggers)}"
            )
        for trigger_name, (
            expected_table_name,
            expected_ddl,
        ) in expected_triggers.items():
            actual_trigger = actual_triggers.get(trigger_name)
            if actual_trigger is None:
                continue
            actual_table_name, actual_ddl = actual_trigger
            if (
                actual_table_name != expected_table_name
                or _normalize_sqlite_ddl(actual_ddl)
                != _normalize_sqlite_ddl(expected_ddl)
            ):
                issues.append(
                    f"trigger {trigger_name} does not match canonical DDL"
                )

    return tuple(issues)


def _ensure_watchlist_quant_v6_tables(db_engine: Engine) -> None:
    """Create and verify the immutable B1 tables used outside Alembic.

    Production deploys run Alembic first, while tests and legacy ``init_db``
    callers also rely on ``Base.metadata.create_all``.  This parity hook
    installs the fourteen SQLite triggers that metadata alone cannot express and
    rejects a same-name table whose frozen constraints are incomplete.
    """
    from app.models import Base

    if db_engine.dialect.name != "sqlite":
        raise RuntimeError("watchlist quant-v6 storage requires SQLite")

    for table_name in WATCHLIST_QUANT_V6_TABLE_NAMES:
        Base.metadata.tables[table_name].create(db_engine, checkfirst=True)

    schema_issues = _watchlist_quant_v6_schema_issues(
        db_engine,
        require_triggers=False,
    )
    if schema_issues:
        raise RuntimeError(
            "incomplete watchlist quant-v6 schema: "
            + "; ".join(schema_issues)
        )

    with db_engine.begin() as connection:
        for _, trigger_ddl in (
            _watchlist_quant_v6_trigger_definitions().values()
        ):
            connection.exec_driver_sql(
                trigger_ddl.replace(
                    "CREATE TRIGGER ",
                    "CREATE TRIGGER IF NOT EXISTS ",
                    1,
                )
            )

    complete_issues = _watchlist_quant_v6_schema_issues(db_engine)
    if complete_issues:
        raise RuntimeError(
            "incomplete watchlist quant-v6 schema: "
            + "; ".join(complete_issues)
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


def _ensure_strategy_config_p0_safety_columns(db_engine: Engine) -> None:
    """Install the fail-safe live trading controls on existing SQLite databases."""
    inspector = inspect(db_engine)
    if "strategy_config" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("strategy_config")}
    missing = {
        "allow_position_addons": "BOOLEAN NOT NULL DEFAULT 0",
        "max_position_quantity": "INTEGER NOT NULL DEFAULT 100",
        "max_position_notional": "FLOAT NOT NULL DEFAULT 5000",
        "max_risk_per_trade": "FLOAT NOT NULL DEFAULT 250",
        "stop_loss_pct": "FLOAT NOT NULL DEFAULT 1",
        "max_holding_minutes": "INTEGER NOT NULL DEFAULT 60",
        "entry_cutoff_minutes_before_close": "INTEGER NOT NULL DEFAULT 45",
        "flatten_minutes_before_close": "INTEGER NOT NULL DEFAULT 15",
        "llm_order_execution_enabled": "BOOLEAN NOT NULL DEFAULT 0",
    }
    with db_engine.begin() as connection:
        for name, column_type in missing.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE strategy_config ADD COLUMN {name} {column_type}"
                )
        available_columns = columns | set(missing)
        assignments = [
            f"{name} = 0"
            for name in (
                "short_selling",
                "allow_position_addons",
                "llm_order_execution_enabled",
            )
            if name in available_columns
        ]
        if assignments:
            connection.exec_driver_sql(
                "UPDATE strategy_config SET " + ", ".join(assignments)
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


def _ensure_strategy_v2_shadow_tables(db_engine: Engine) -> None:
    """Create the isolated P2 forward-shadow tables when upgrading in place."""
    from app.models import Base

    for table_name in (
        "strategy_v2_shadow_config",
        "strategy_v2_shadow_versions",
        "strategy_v2_shadow_state",
        "strategy_v2_shadow_decisions",
        "strategy_v2_shadow_trades",
        "strategy_v2_forward_registrations",
        "strategy_v2_forward_evidence",
        "strategy_v2_forward_replay_artifacts",
        "strategy_v2_forward_evidence_artifacts",
        "strategy_v2_exit_challenger_registrations",
        "strategy_v2_exit_challenger_trades",
        "strategy_v2_bracket_challenger_registrations",
        "strategy_v2_bracket_challenger_trades",
        "live_exit_challenger_registrations",
        "live_exit_challenger_trades",
        "strategy_v2_portfolio_registrations",
        "strategy_v2_portfolio_observations",
    ):
        Base.metadata.tables[table_name].create(db_engine, checkfirst=True)

    _ensure_strategy_v2_forward_registration_uniqueness(db_engine)
    inspector = inspect(db_engine)
    config_columns = {
        column["name"]
        for column in inspector.get_columns("strategy_v2_shadow_config")
    }
    trade_columns = {
        column["name"]
        for column in inspector.get_columns("strategy_v2_shadow_trades")
    }
    evidence_columns = {
        column["name"]
        for column in inspector.get_columns("strategy_v2_forward_evidence")
    }
    exit_registration_columns = {
        column["name"]
        for column in inspector.get_columns(
            "strategy_v2_exit_challenger_registrations"
        )
    }
    bracket_registration_columns = {
        column["name"]
        for column in inspector.get_columns(
            "strategy_v2_bracket_challenger_registrations"
        )
    }
    live_exit_registration_columns = {
        column["name"]
        for column in inspector.get_columns(
            "live_exit_challenger_registrations"
        )
    }
    with db_engine.begin() as connection:
        if "policy_type" not in live_exit_registration_columns:
            connection.exec_driver_sql(
                "ALTER TABLE live_exit_challenger_registrations "
                "ADD COLUMN policy_type VARCHAR(24) NOT NULL "
                "DEFAULT 'PROFIT_LOCK'"
            )
        if "max_holding_minutes" not in live_exit_registration_columns:
            connection.exec_driver_sql(
                "ALTER TABLE live_exit_challenger_registrations "
                "ADD COLUMN max_holding_minutes INTEGER"
            )
        if "estimated_fee_rate_us" not in config_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_config "
                "ADD COLUMN estimated_fee_rate_us FLOAT NOT NULL DEFAULT 0.0005"
            )
        if "estimated_fee_rate_hk" not in config_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_config "
                "ADD COLUMN estimated_fee_rate_hk FLOAT NOT NULL DEFAULT 0.003"
            )
        if "universe_managed" not in config_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_config "
                "ADD COLUMN universe_managed BOOLEAN NOT NULL DEFAULT 0"
            )
        if "opening_momentum_execution_eligible" not in config_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_config "
                "ADD COLUMN opening_momentum_execution_eligible "
                "BOOLEAN NOT NULL DEFAULT 1"
            )
        if "holding_deadline" not in trade_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_trades ADD COLUMN holding_deadline DATETIME"
            )
        if "estimated_fee_rate" not in trade_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_shadow_trades ADD COLUMN estimated_fee_rate FLOAT"
            )
        if "evidence_digest_sha256" not in evidence_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_forward_evidence ADD COLUMN "
                "evidence_digest_sha256 VARCHAR(64) NOT NULL DEFAULT ''"
            )
        if "policy_type" not in exit_registration_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_exit_challenger_registrations "
                "ADD COLUMN policy_type VARCHAR(24) NOT NULL "
                "DEFAULT 'PROFIT_LOCK'"
            )
        if "max_holding_minutes" not in exit_registration_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_exit_challenger_registrations "
                "ADD COLUMN max_holding_minutes INTEGER"
            )
        if "vwap_target_cap_bps" not in bracket_registration_columns:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_v2_bracket_challenger_registrations "
                "ADD COLUMN vwap_target_cap_bps FLOAT"
            )


def _ensure_strategy_v2_forward_registration_uniqueness(
    db_engine: Engine,
) -> None:
    """Preserve old evidence while allowing one registration per frozen version."""
    from app.models import Base

    table_name = "strategy_v2_forward_registrations"
    desired_columns = {
        "symbol",
        "source_config_version",
        "candidate_algorithm_version",
        "evaluator_digest",
    }
    inspector = inspect(db_engine)
    constraints = inspector.get_unique_constraints(table_name)
    if any(
        set(constraint.get("column_names") or ()) == desired_columns
        for constraint in constraints
    ):
        return
    if db_engine.dialect.name != "sqlite":
        raise RuntimeError(
            "strategy v2 forward registration migration requires SQLite"
        )

    legacy_table = f"{table_name}__symbol_unique_legacy"
    columns = (
        "id",
        "symbol",
        "market",
        "candidate_algorithm_version",
        "source_config_version",
        "evaluator_digest",
        "candidate_spec_json",
        "registered_at",
        "eligible_after",
    )
    column_sql = ", ".join(columns)
    with db_engine.begin() as connection:
        legacy_exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (legacy_table,),
        ).first()
        if legacy_exists is not None:
            raise RuntimeError(
                "unfinished strategy v2 forward registration migration"
            )
        connection.exec_driver_sql(
            f"ALTER TABLE {table_name} RENAME TO {legacy_table}"
        )
        connection.exec_driver_sql(
            "DROP INDEX IF EXISTS "
            "ix_strategy_v2_forward_registration_symbol_eligible"
        )
        Base.metadata.tables[table_name].create(
            connection,
            checkfirst=False,
        )
        connection.exec_driver_sql(
            f"INSERT INTO {table_name} ({column_sql}) "
            f"SELECT {column_sql} FROM {legacy_table}"
        )
        connection.exec_driver_sql(f"DROP TABLE {legacy_table}")


def _ensure_opening_momentum_shadow_table(db_engine: Engine) -> None:
    """Create the prospective cross-sectional shadow table in place."""
    from app.models import Base

    Base.metadata.tables["opening_momentum_shadow_runs"].create(
        db_engine,
        checkfirst=True,
    )
    inspector = inspect(db_engine)
    columns = {
        column["name"]
        for column in inspector.get_columns(
            "opening_momentum_shadow_runs"
        )
    }
    evidence_columns = {
        "candidate_first_five_return_bps": "FLOAT",
        "candidate_last_five_return_bps": "FLOAT",
        "candidate_path_efficiency": "FLOAT",
        "candidate_max_pullback_bps": "FLOAT",
        "candidate_opening_range_bps": "FLOAT",
        "candidate_breakout_depth_bps": "FLOAT",
        "candidate_signal_turnover": "FLOAT",
        "candidate_avg_dollar_volume": "FLOAT",
        "candidate_signal_turnover_ratio": "FLOAT",
        "candidate_opening_activity_ratio": "FLOAT",
        "candidate_overnight_gap_bps": "FLOAT",
        "candidate_prev_close_to_signal_bps": "FLOAT",
        "benchmark_qqq_return_bps": "FLOAT",
        "benchmark_dia_return_bps": "FLOAT",
        "stop_loss_pct": "FLOAT",
        "maximum_adverse_excursion_bps": "FLOAT",
        "maximum_favorable_excursion_bps": "FLOAT",
    }
    with db_engine.begin() as connection:
        for name, column_type in evidence_columns.items():
            if name not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE opening_momentum_shadow_runs "
                    f"ADD COLUMN {name} {column_type}"
                )


def _ensure_opening_activity_observation_table(
    db_engine: Engine,
) -> None:
    """Create the causal opening-activity history table in place."""
    from app.models import Base

    Base.metadata.tables["opening_activity_observations"].create(
        db_engine,
        checkfirst=True,
    )


def _ensure_opening_momentum_execution_table(db_engine: Engine) -> None:
    """Create the idempotent opening-execution journal in place."""
    from app.models import Base

    Base.metadata.tables["opening_momentum_executions"].create(
        db_engine,
        checkfirst=True,
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


def _ensure_llm_interaction_token_columns(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    if "llm_interactions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("llm_interactions")}
    missing_columns = {
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "total_tokens": "INTEGER",
    }
    with db_engine.begin() as connection:
        for name, column_type in missing_columns.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE llm_interactions ADD COLUMN {name} {column_type}"
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
            if "ix_llm_interactions_created_at_id" not in existing_indexes:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_llm_interactions_created_at_id "
                    "ON llm_interactions (created_at, id)"
                )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
