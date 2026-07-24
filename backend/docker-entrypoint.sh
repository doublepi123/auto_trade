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

INITIAL_REVISION = '41e077353669'
LLM_FIELDS_REVISION = '20260602_add_llm_interval_fields'
LLM_INTERVAL_REVISION = '20260520_add_llm_interval_minutes'
MIN_PROFIT_REVISION = '20260522_add_min_profit_amount'
AUTO_RESUME_REVISION = '20260522_auto_resume_pause'
LLM_INTERACTIONS_REVISION = '20260522_add_llm_interactions'
HEAD_REVISION = '20260724_opening_momentum'
# IMPORTANT: 每次新增 alembic 迁移时，必须同步更新 HEAD_REVISION 及 mark_migrated_if_needed 的列检测逻辑


def mark_migrated_if_needed():
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        if 'alembic_version' in tables:
            return
        if 'strategy_config' not in tables:
            return

        strategy_columns = {column['name'] for column in inspector.get_columns('strategy_config')}
        runtime_state_columns = (
            {column['name'] for column in inspector.get_columns('runtime_state')}
            if 'runtime_state' in tables
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
            version_num = HEAD_REVISION

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
