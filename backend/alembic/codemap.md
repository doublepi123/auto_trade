# backend/alembic/

## Responsibility

Alembic migration environment for the SQLite schema: the declarative revision
chain under `versions/` (see [`versions/codemap.md`](versions/codemap.md)) plus
the `env.py` wiring that runs migrations against `app.models.Base.metadata`.
In production it runs **at container start only** (`docker-entrypoint.sh`:
stamp → `alembic upgrade head`); it is excluded from `pyrightconfig.json`
and ships in the runtime image alongside `app/`.

## Design

- `alembic.ini` (in `backend/`, not here): `script_location = %(here)s/alembic`,
  `prepend_sys_path = .`, default `sqlalchemy.url = sqlite:///data/auto_trade.db`.
  The entrypoint `sed`-rewrites that URL to `AUTO_TRADE_DATABASE_URL` before
  upgrading, so the checked-in value is only a default.
- `script.py.mako` is the standard revision template; `README` is Alembic's.
- `env.py` imports `app.models.Base` (path-injected) as `target_metadata`,
  uses `fileConfig(..., disable_existing_loggers=False)` — programmatic
  callers share a process with the app and must not lose its loggers — and
  `pool.NullPool` for online mode. No autogenerate workflow; revisions are
  hand-written.

### Coexistence with runtime `_ensure_*` migrations

Alembic is **not** the sole schema authority in prod. `app/database.py`
carries ~50 idempotent `_ensure_*_table` / `_ensure_*_columns` functions that
`init_db()` applies on every startup (create-if-missing + additive column
migrations; docstrings call this "runtime migration parity"). Today the
entrypoint runs `alembic upgrade head` first, then `init_db()` converges the
rest. Practical contract for a new table/column:

1. Add the SQLAlchemy model **and** an `_ensure_*` in `database.py`.
2. Add an Alembic revision (chain table in `versions/codemap.md`).
3. Update `HEAD_REVISION` and the column-detection logic in
   `docker-entrypoint.sh` — the stamp routine refuses partial schemas, so a
   revision that is not teachable to the stamp logic blocks deploys.

`_ensure_*` only adds, never drops, and the stamp logic only moves
`alembic_version` forward to what the schema provably matches, so any
historical DB state converges without guessing — mismatches abort the
container instead.

## Flow

Container start: entrypoint inspects schema → stamps `alembic_version` →
`alembic upgrade head` walks the chain in `versions/` → `init_db()` runs the
`_ensure_*` pass → uvicorn starts. Local dev rarely invokes `alembic`
directly — `init_db()` alone keeps a dev DB converged.

## Integration

- `app/models` — `Base.metadata` is the migration target.
- `app/database.py` — runtime `_ensure_*` counterpart; its
  `WATCHLIST_QUANT_V6_TABLE_NAMES` / `_watchlist_quant_v6_schema_issues`
  are imported by the entrypoint's stamp routine.
- `docker-entrypoint.sh` / `Dockerfile` — the only prod driver ([`../codemap.md`](../codemap)).
- `tests/` — migration integrity tests run the chain against scratch SQLite
  files (`tests/AGENTS.md`).
