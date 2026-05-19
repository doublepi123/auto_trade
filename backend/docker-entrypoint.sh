#!/bin/bash
set -e

cd /app

# 确保 Alembic 使用与后端一致的数据库路径
AUTO_TRADE_DATABASE_URL="${AUTO_TRADE_DATABASE_URL:-sqlite:///data/auto_trade.db}"

# 老数据库缺少 alembic_version 表会导致 initial migration 从头建表报错
python -c "
from sqlalchemy import create_engine, inspect, text
from app.config import settings
def mark_migrated_if_needed():
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        if tables and 'alembic_version' not in tables:
            conn.execute(text(\"CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)\"))
            conn.execute(text(\"INSERT INTO alembic_version (version_num) VALUES ('20260602_add_llm_interval_fields')\"))
            conn.commit()
            print('stamped alembic_version to 20260602_add_llm_interval_fields')
mark_migrated_if_needed()
"

# 覆盖 alembic.ini 中的 sqlalchemy.url，确保指向正确数据库
sed -i "s|^sqlalchemy.url = .*|sqlalchemy.url = ${AUTO_TRADE_DATABASE_URL}|" alembic.ini

alembic upgrade head

python -c "from app.database import init_db; init_db()"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
