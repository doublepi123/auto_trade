# backend/

## Responsibility

Python 3.11+ FastAPI + SQLAlchemy 2.0 + SQLite backend for the automated
range-trading system (Longbridge HK/US). This folder owns the packaging,
dependency, migration, and test configuration that turn `app/` into a
deployable container and a verifiable codebase. Application code itself is
mapped in [`app/codemap.md`](app/codemap.md).

## Design

### Build & deploy (multi-stage `Dockerfile`)

- **builder stage** (`python:3.11-slim` + `build-essential`): builds an
  isolated venv at `/opt/venv` from `requirements.lock.txt` (exact pins,
  `--generate-hashes` source of truth), then strips the toolchain.
- **runtime stage**: `python:3.11-slim` + `tini` (PID 1 zombie reaping) and
  `curl` (healthcheck probes); runs as non-root `appuser`. It ships **only**
  `app/`, `alembic/`, `alembic.ini`,
  `scripts/import_historical_order_ledger.py`, and `docker-entrypoint.sh` —
  no tests, no dev scripts, no `.env` (enforced by `.dockerignore`).
- Local deploy does not wait on CI: the image is built from the working tree
  (see root `AGENTS.md` "Autonomous Commit & Deploy" for the gate sequence).

### Entrypoint (`docker-entrypoint.sh`)

`stamp → upgrade → converge → serve`:

1. `mark_migrated_if_needed()` inspects the live SQLite schema and stamps
   `alembic_version` to the highest revision the data actually matches
   (constants headed by `HEAD_REVISION = '20260922_fill_intent'`); partial
   or out-of-lineage schemas abort with `RuntimeRefusal` rather than guess.
   **Every new alembic revision must update this constant and the column
   detection logic** (comment in the script says so explicitly).
2. Rewrites `sqlalchemy.url` in `alembic.ini` to `AUTO_TRADE_DATABASE_URL`
   (default `sqlite:///data/auto_trade.db`).
3. `alembic upgrade head`, then `init_db()` so the runtime `_ensure_*`
   migrations in `app/database.py` finish convergence.
4. `exec uvicorn app.main:app --host 0.0.0.0 --port 8000`.

### Test config (`pytest.ini`, `.coveragerc`)

- `asyncio_mode = auto`, `testpaths = tests`, `pythonpath = .`.
- Hardwired `-n 4 --dist loadgroup` — **not** `-n auto`: quant-v6 tests spawn
  compute processes and assert wall-clock deadlines, so extra workers
  oversubscribe the CPU. `loadgroup` schedules by the groups `conftest.py`
  assigns (one per module by default; shared groups for shared-DB modules;
  no group for audited modules so they spread). Do not regroup or shard on
  top of this (see root `AGENTS.md`).
- Coverage gate: `--cov=app --cov-fail-under=80 --no-cov-on-fail`.
  `.coveragerc` omits `app/main.py`, `app/runner.py`, `app/database.py`,
  `app/config.py`, `app/api/ws.py`, and `app/__init__.py`.
- Full-suite comparisons require a pristine worktree baseline — the local
  `.env` leaks prod settings into `Settings` otherwise.

### Dependencies

| File | Role |
|---|---|
| `requirements.in` | pip-compile source; regenerate lock with `pip-compile requirements.in --generate-hashes --output-file=requirements.lock.txt` |
| `requirements.txt` | Runtime `~=` ranges for day-to-day dev |
| `requirements.lock.txt` | Exact hashed pins; what the Docker builder installs |
| `requirements-dev.txt` | pytest>=8, pytest-asyncio, pytest-xdist, pytest-cov, coverage, freezegun, PyYAML, basedpyright |

`pyrightconfig.json` (tracked): includes `app`, `tests`, `scripts`; excludes
`alembic`, venvs, caches; Python 3.11, `typeCheckingMode: basic`. The
repo-root pyrightconfig is gitignored per-developer — this is the one CI uses.

## Flow

Dev loop: `scripts/setup_venv.sh --reset` → edit `app/` →
`python3 -m pytest tests/ -v` + `python3 -m basedpyright` → commit/push →
`docker compose up --build -d` rebuilds from the working tree. At container
start the entrypoint converges the database before uvicorn binds :8000.
SQLite lives in `data/auto_trade.db` (WAL mode — back up `-wal`/`-shm` too).

## Integration

| Path | Map |
|---|---|
| `app/` | [`app/codemap.md`](app/codemap.md) — FastAPI app, services, domain, platform |
| `alembic/` | [`alembic/codemap.md`](alembic/codemap.md) — offline schema migrations |
| `scripts/` | [`scripts/codemap.md`](scripts/codemap.md) — research/ops CLIs (dev image only, one exception) |
| `tests/` | Intentionally **not** mapped here — see [`tests/AGENTS.md`](tests/AGENTS.md) for the 481-file suite, DB isolation, and lane rules |
| `data/` | Runtime SQLite storage; created by the container, never committed |
