#!/bin/bash
set -e

cd /app
python -c "from app.database import init_db; init_db()"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
