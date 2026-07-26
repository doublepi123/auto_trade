# auto_trade backend

## Quick start

### 1. Python 3.11+

```bash
python3.11 --version
```

### 2. Virtualenv

```bash
# Prefer setup helper when available
./scripts/setup_venv.sh --reset

# Or manual:
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 3. Verify

```bash
source .venv/bin/activate
python -c "import fastapi, sqlalchemy, pydantic; print('ok')"
pytest tests/test_database.py -q
pytest tests/ -v
```

### 4. Run API

From repo root, ensure `.env` exists (`cp .env.example .env`). Then:

```bash
uvicorn app.main:app --reload --port 8000
```

OpenAPI: `http://localhost:8000/docs`

## Dependencies

| File | Purpose |
|------|---------|
| `requirements.txt` | Runtime (`~=` ranges for day-to-day dev) |
| `requirements-dev.txt` | pytest / basedpyright / tools |
| `requirements.lock.txt` | Optional exact pin file if present for CI/prod |

## SQLite

`app/database.py` enables WAL, busy timeout, foreign keys, and runtime `_ensure_*` column migrations.

Backup: copy `data/auto_trade.db` **and** `-wal` / `-shm` when using WAL.

## Layout (high level)

| Path | Role |
|------|------|
| `app/main.py` | Lifespan, crons, router mounts |
| `app/runner.py` | Live loop + shadow job orchestration |
| `app/core/` | Broker, engine, risk, fees, audit |
| `app/domain/` | Pure logic (prompt, strategy_v2, universe, opening momentum) |
| `app/services/` | Execution, LLM, universe, quant, shadows, review |
| `app/platform/` | Research plugin layer (`/api/platform/*`) |
| `app/api/` | HTTP routers |
| `scripts/` | Read-only research scripts (walk-forward, index membership) |
| `tests/` | pytest suite |

## Docs

User-facing product docs: repo root `README.md`.  
Agent conventions: root `AGENTS.md` / `CLAUDE.md`.  
Prompt plugins: `app/domain/prompt/AGENTS.md`.
