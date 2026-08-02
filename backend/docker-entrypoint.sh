#!/bin/bash
set -e

cd /app

# 确保 Alembic 使用与后端一致的数据库路径
AUTO_TRADE_DATABASE_URL="${AUTO_TRADE_DATABASE_URL:-sqlite:///data/auto_trade.db}"

# 老数据库缺少 alembic_version 表会导致 initial migration 从头建表报错。
# 按实际 schema stamp 到“已具备的最高安全版本”，再交给 Alembic 和 init_db() 继续补齐。
python -c "
from sqlalchemy import create_engine, inspect, text
from app.config import settings
from app.database import (
    WATCHLIST_QUANT_V6_TABLE_NAMES,
    _watchlist_quant_v6_schema_issues,
)

INITIAL_REVISION = '41e077353669'
LLM_FIELDS_REVISION = '20260602_add_llm_interval_fields'
LLM_INTERVAL_REVISION = '20260520_add_llm_interval_minutes'
MIN_PROFIT_REVISION = '20260522_add_min_profit_amount'
AUTO_RESUME_REVISION = '20260522_auto_resume_pause'
LLM_INTERACTIONS_REVISION = '20260522_add_llm_interactions'
OPENING_MOMENTUM_REVISION = '20260724_opening_momentum'
OPENING_STOP_REVISION = '20260726_opening_stop'
OPENING_CONTEXT_REVISION = '20260727_opening_context'
OPENING_EXECUTION_REVISION = '20260727_opening_execution'
WATCHLIST_QUANT_V6_REVISION = '20260801_watchlist_quant_v6'
DURABLE_JOB_LEASES_REVISION = '20260801_durable_job_leases'
HEAD_REVISION = DURABLE_JOB_LEASES_REVISION
# IMPORTANT: 每次新增 alembic 迁移时，必须同步更新 HEAD_REVISION 及 mark_migrated_if_needed 的列检测逻辑


def advance_added_columns(
    *,
    current_revision,
    predecessor,
    revision,
    label,
    actual_columns,
    added_columns,
):
    present_columns = actual_columns & added_columns
    if not present_columns:
        return current_revision
    if present_columns != added_columns:
        missing = sorted(added_columns - present_columns)
        raise RuntimeError(
            f'partial {label} schema; missing columns: {missing}'
        )
    if current_revision != predecessor:
        raise RuntimeError(
            f'{label} schema is outside the expected revision lineage: '
            f'{current_revision} != {predecessor}'
        )
    return revision


def opening_execution_schema_issues(inspector):
    table_name = 'opening_momentum_executions'
    expected_columns = (
        'id',
        'session_date',
        'algorithm_version',
        'config_version',
        'universe_source',
        'selection_run_id',
        'status',
        'reason',
        'symbol',
        'signal_at',
        'armed_at',
        'entry_due_at',
        'entry_deadline_at',
        'requested_at',
        'universe_size',
        'market_return_bps',
        'candidate_return_bps',
        'excess_return_bps',
        'reference_entry_price',
        'max_price_deviation_bps',
        'stop_loss_pct',
        'max_holding_minutes',
        'signal_context_json',
        'submit_attempts',
        'entry_order_id',
        'exit_order_id',
        'entry_filled_at',
        'entry_price',
        'quantity',
        'exit_filled_at',
        'exit_price',
        'net_pnl',
        'created_at',
        'updated_at',
    )
    actual_columns = tuple(
        str(column['name'])
        for column in inspector.get_columns(table_name)
    )
    issues = []
    if actual_columns != expected_columns:
        issues.append(
            f'columns differ: expected {expected_columns}, '
            f'found {actual_columns}'
        )
    primary_key = tuple(
        inspector.get_pk_constraint(table_name).get(
            'constrained_columns'
        )
        or ()
    )
    if primary_key != ('id',):
        issues.append(f'primary key differs: {primary_key}')
    unique_constraints = {
        str(constraint.get('name')): tuple(
            constraint.get('column_names') or ()
        )
        for constraint in inspector.get_unique_constraints(table_name)
    }
    if unique_constraints.get(
        'uq_opening_momentum_execution_session'
    ) != ('session_date',):
        issues.append('missing session-date unique constraint')
    indexes = {
        str(index.get('name')): tuple(index.get('column_names') or ())
        for index in inspector.get_indexes(table_name)
    }
    if indexes.get(
        'ix_opening_momentum_execution_status_session'
    ) != ('status', 'session_date'):
        issues.append('missing status/session index')
    return tuple(issues)


def durable_job_lease_schema_issues(inspector, conn):
    table_name = 'durable_job_leases'
    expected_columns = (
        ('lease_key', 'VARCHAR(128)', False),
        ('holder_id', 'VARCHAR(128)', False),
        ('fencing_token', 'INTEGER', False),
        ('acquired_at_epoch_ms', 'INTEGER', False),
        ('renewed_at_epoch_ms', 'INTEGER', False),
        ('expires_at_epoch_ms', 'INTEGER', False),
    )
    actual_columns = tuple(
        (
            str(column['name']),
            str(column['type']).upper(),
            bool(column['nullable']),
        )
        for column in inspector.get_columns(table_name)
    )
    issues = []
    if actual_columns != expected_columns:
        issues.append(
            f'columns differ: expected {expected_columns}, '
            f'found {actual_columns}'
        )
    primary_key = tuple(
        inspector.get_pk_constraint(table_name).get(
            'constrained_columns'
        )
        or ()
    )
    if primary_key != ('lease_key',):
        issues.append(f'primary key differs: {primary_key}')
    expected_checks = {
        'ck_durable_job_lease_key',
        'ck_durable_job_lease_holder',
        'ck_durable_job_lease_fencing_token',
        'ck_durable_job_lease_epoch_ms',
    }
    actual_checks = {
        str(constraint.get('name'))
        for constraint in inspector.get_check_constraints(table_name)
    }
    missing_checks = sorted(expected_checks - actual_checks)
    if missing_checks:
        issues.append(f'missing check constraints: {missing_checks}')
    trigger_sql = conn.execute(
        text(
            'SELECT sql FROM sqlite_master '
            'WHERE type = :object_type AND name = :trigger_name'
        ),
        {
            'object_type': 'trigger',
            'trigger_name': 'trg_durable_job_leases_no_delete',
        },
    ).scalar_one_or_none()
    expected_trigger_sql = (
        'CREATE TRIGGER trg_durable_job_leases_no_delete '
        'BEFORE DELETE ON durable_job_leases '
        'BEGIN SELECT RAISE(ABORT, '
        + chr(39)
        + 'durable_job_leases rows cannot be deleted'
        + chr(39)
        + '); END'
    )
    if trigger_sql is None:
        issues.append('missing no-delete trigger')
    elif ' '.join(str(trigger_sql).split()) != expected_trigger_sql:
        issues.append('no-delete trigger does not match canonical DDL')
    return tuple(issues)


def mark_migrated_if_needed():
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        quant_table_names = set(WATCHLIST_QUANT_V6_TABLE_NAMES)
        present_quant_tables = tables & quant_table_names
        quant_schema_complete = False
        if present_quant_tables:
            quant_issues = _watchlist_quant_v6_schema_issues(engine)
            if quant_issues:
                raise RuntimeError(
                    'partial watchlist quant-v6 schema; refusing to stamp: '
                    + '; '.join(quant_issues)
                )
            quant_schema_complete = True
        lease_schema_complete = False
        if 'durable_job_leases' in tables:
            lease_issues = durable_job_lease_schema_issues(inspector, conn)
            if lease_issues:
                raise RuntimeError(
                    'partial durable-job-lease schema; refusing to stamp: '
                    + '; '.join(lease_issues)
                )
            lease_schema_complete = True
        if lease_schema_complete and not quant_schema_complete:
            raise RuntimeError(
                'durable-job-lease schema exists without its quant-v6 '
                'predecessor schema'
            )
        if 'alembic_version' in tables:
            recorded_revisions = tuple(
                conn.execute(
                    text('SELECT version_num FROM alembic_version')
                ).scalars()
            )
            if len(recorded_revisions) != 1:
                raise RuntimeError(
                    'alembic_version must contain exactly one revision; '
                    f'found {recorded_revisions}'
                )
            recorded_revision = recorded_revisions[0]
            if recorded_revision == DURABLE_JOB_LEASES_REVISION:
                if not quant_schema_complete or not lease_schema_complete:
                    raise RuntimeError(
                        'alembic_version is durable-job-leases but its '
                        'schema or predecessor schema is incomplete'
                    )
                return
            if lease_schema_complete:
                if recorded_revision != WATCHLIST_QUANT_V6_REVISION:
                    raise RuntimeError(
                        'complete durable-job-lease schema is outside the '
                        'expected recorded lineage: '
                        f'{recorded_revision} != '
                        f'{WATCHLIST_QUANT_V6_REVISION}'
                    )
                conn.execute(
                    text(
                        'UPDATE alembic_version SET version_num = '
                        ':version_num'
                    ),
                    {'version_num': DURABLE_JOB_LEASES_REVISION},
                )
                conn.commit()
                print(
                    'advanced alembic_version from '
                    f'{WATCHLIST_QUANT_V6_REVISION} to '
                    f'{DURABLE_JOB_LEASES_REVISION}'
                )
                return
            if recorded_revision == WATCHLIST_QUANT_V6_REVISION:
                if not quant_schema_complete:
                    quant_issues = _watchlist_quant_v6_schema_issues(engine)
                    raise RuntimeError(
                        'alembic_version is quant-v6 but its schema is '
                        'incomplete: ' + '; '.join(quant_issues)
                    )
                return
            if quant_schema_complete:
                if recorded_revision != OPENING_EXECUTION_REVISION:
                    raise RuntimeError(
                        'complete watchlist quant-v6 schema is outside the '
                        'expected recorded lineage: '
                        f'{recorded_revision} != '
                        f'{OPENING_EXECUTION_REVISION}'
                    )
                conn.execute(
                    text(
                        'UPDATE alembic_version SET version_num = '
                        ':version_num'
                    ),
                    {'version_num': WATCHLIST_QUANT_V6_REVISION},
                )
                conn.commit()
                print(
                    'advanced alembic_version from '
                    f'{OPENING_EXECUTION_REVISION} to '
                    f'{WATCHLIST_QUANT_V6_REVISION}'
                )
            return
        if 'strategy_config' not in tables:
            return

        strategy_columns = {column['name'] for column in inspector.get_columns('strategy_config')}
        runtime_state_columns = (
            {column['name'] for column in inspector.get_columns('runtime_state')}
            if 'runtime_state' in tables
            else set()
        )
        opening_columns = (
            {column['name'] for column in inspector.get_columns('opening_momentum_shadow_runs')}
            if 'opening_momentum_shadow_runs' in tables
            else set()
        )
        required_columns = {
            'llm_interval_minutes',
            'min_profit_amount',
            'auto_resume_minutes',
            'auto_interval_enabled',
            'llm_suggested_buy_low',
            'llm_suggested_sell_high',
            'llm_confidence_score',
            'llm_analysis',
            'llm_last_analysis_at',
            'llm_next_analysis_at',
            'llm_applied_buy_low',
            'llm_applied_sell_high',
            'llm_applied_at',
            'llm_reject_reason',
        }

        version_num = INITIAL_REVISION
        llm_field_columns = required_columns - {'llm_interval_minutes', 'min_profit_amount', 'auto_resume_minutes'}
        if llm_field_columns.issubset(strategy_columns):
            version_num = LLM_FIELDS_REVISION
        if version_num == LLM_FIELDS_REVISION and 'llm_interval_minutes' in strategy_columns:
            version_num = LLM_INTERVAL_REVISION
        if version_num == LLM_INTERVAL_REVISION and 'min_profit_amount' in strategy_columns:
            version_num = MIN_PROFIT_REVISION
        auto_resume_columns = {'pause_reason', 'paused_at', 'pause_auto_resumable'}
        if (
            version_num == MIN_PROFIT_REVISION
            and 'auto_resume_minutes' in strategy_columns
            and auto_resume_columns.issubset(runtime_state_columns)
        ):
            version_num = AUTO_RESUME_REVISION
        if version_num == AUTO_RESUME_REVISION and 'llm_interactions' in tables:
            version_num = LLM_INTERACTIONS_REVISION
        if (
            version_num == LLM_INTERACTIONS_REVISION
            and 'opening_momentum_shadow_runs' in tables
        ):
            version_num = OPENING_MOMENTUM_REVISION
        opening_stop_columns = {
            'stop_loss_pct',
            'maximum_adverse_excursion_bps',
            'maximum_favorable_excursion_bps',
        }
        version_num = advance_added_columns(
            current_revision=version_num,
            predecessor=OPENING_MOMENTUM_REVISION,
            revision=OPENING_STOP_REVISION,
            label='opening-stop',
            actual_columns=opening_columns,
            added_columns=opening_stop_columns,
        )

        opening_context_columns = {
            'candidate_overnight_gap_bps',
            'candidate_prev_close_to_signal_bps',
            'benchmark_qqq_return_bps',
            'benchmark_dia_return_bps',
        }
        version_num = advance_added_columns(
            current_revision=version_num,
            predecessor=OPENING_STOP_REVISION,
            revision=OPENING_CONTEXT_REVISION,
            label='opening-context',
            actual_columns=opening_columns,
            added_columns=opening_context_columns,
        )

        if 'opening_momentum_executions' in tables:
            execution_issues = opening_execution_schema_issues(inspector)
            if execution_issues:
                raise RuntimeError(
                    'partial opening-execution schema; refusing to stamp: '
                    + '; '.join(execution_issues)
                )
            if version_num != OPENING_CONTEXT_REVISION:
                raise RuntimeError(
                    'opening-execution schema is outside the expected '
                    f'revision lineage: {version_num} != '
                    f'{OPENING_CONTEXT_REVISION}'
                )
            version_num = OPENING_EXECUTION_REVISION

        if quant_schema_complete:
            if version_num != OPENING_EXECUTION_REVISION:
                raise RuntimeError(
                    'watchlist quant-v6 schema is outside the expected '
                    f'revision lineage: {version_num} != '
                    f'{OPENING_EXECUTION_REVISION}'
                )
            version_num = WATCHLIST_QUANT_V6_REVISION

        if lease_schema_complete:
            if version_num != WATCHLIST_QUANT_V6_REVISION:
                raise RuntimeError(
                    'durable-job-lease schema is outside the expected '
                    f'revision lineage: {version_num} != '
                    f'{WATCHLIST_QUANT_V6_REVISION}'
                )
            version_num = DURABLE_JOB_LEASES_REVISION

        conn.execute(text(\"CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)\"))
        conn.execute(text('INSERT INTO alembic_version (version_num) VALUES (:version_num)'), {'version_num': version_num})
        conn.commit()
        print(f'stamped alembic_version to {version_num}')


mark_migrated_if_needed()
"

# 覆盖 alembic.ini 中的 sqlalchemy.url，确保指向正确数据库
sed -i "s|^sqlalchemy.url = .*|sqlalchemy.url = ${AUTO_TRADE_DATABASE_URL}|" alembic.ini

alembic upgrade head

python -c "from app.database import init_db; init_db()"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
