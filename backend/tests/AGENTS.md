# `backend/tests/` — Test Suite

## OVERVIEW
481 test files, ~170k lines, ~6.7k test functions. 216 flat files here + 264 under `platform/`. pytest 9, `asyncio_mode=auto`, no `unittest.TestCase`.

## STRUCTURE
Flat by design. `test_<module>.py` mirrors `app/<...>/<module>.py`; `tests/platform/` mirrors `app/platform/`; `tests/fixtures/` holds replay data (e.g. `pltr_risk_cap_bypass.py`). No other subdirectories — do not introduce one.

## conftest.py — ENV, SCHEDULING, PLUGIN REGISTRATION; NO FIXTURES
Process-level setup:
1. Installs a `MetaPathFinder` that makes `import longport` / `longbridge` raise `ImportError` (escape hatch: `AUTO_TRADE_ALLOW_BROKER_SDK_IMPORTS=1`). Tests therefore always run against fakes.
2. Points `AUTO_TRADE_DATABASE_URL` at `/tmp/auto_trade_pytest_<pid>.db` (override: `AUTO_TRADE_TEST_DATABASE_URL`) and the credential key at a temp path.
3. Blanks ~10 credential env vars; pins deterministic LLM provider defaults.

Add env defaults here; do not define fixtures in `conftest.py`. Module-local
fixtures remain valid. `conftest.py` also owns collection scheduling and registers
`tests.runner_db_isolation_plugin`, whose function-scoped autouse fixture owns
the per-test database lifecycle exclusively for legacy `tests/test_runner.py`,
which must not be edited for this isolation change. It rebinds the explicit live
engine/session-factory references, calls the real `database.init_db()`, restores
the original objects, disposes the engine, and removes its private temporary
database after each test. Multiple runners within one test share that database.
The existing xdist grouping is unchanged; the recorded cross-module leak has not
been resolved by this lifecycle change.

## DB ISOLATION — TWO ACCEPTED PATTERNS
**A. Own engine + dependency override** (preferred for API tests, see `test_trades_export.py`): module-level `TEST_DATABASE_URL` including `os.getpid()`; `setup_class` creates the engine, `drop_all` + `create_all`, installs `app.dependency_overrides[get_db]`, builds `TestClient(app)`; `teardown_class` pops the override and disposes; `setup_method` deletes rows table-by-table.

**B. Shared app DB** (see `test_credentials_api.py`): module-level `database.init_db()`, `SessionLocal`, an `autouse` fixture for monkeypatching, and `_clean_<table>()` helpers.

For mutable tables in patterns A/B, clean by `db.query(Model).delete()` — never
unlink a shared DB file. Append-only tables must instead be isolated with a new
database per test, never by deleting rows or disabling triggers. The named runner
plugin removes only its own private database after disposal. Use
`monkeypatch.delenv(..., raising=False)`, never `os.environ.pop`.

## FAKES
Inline classes, `_Fake` prefix, named for the collaborator, hand-written and minimal — MagicMock is not the house style. Real examples: `_FakeBroker` (records calls into instance attributes for assertions), `_FakeRunner`, `_FakeDb` / `_FakeQuery`, `_FakeClock` / `_FakeMonotonicClock`, `_FakeCandles`. Inject via `monkeypatch.setattr(<api module>, "get_runner", lambda: _FakeRunner(broker))`.

## API TESTS
`TestClient(app)` imported from `app.main`, in three shapes: module-level singleton, `with TestClient(app) as c:` when lifespan/WS must run, or `cls.client` in `setup_class`. Plain `Test*` classes with `setup_class` / `setup_method` — no `unittest.TestCase`.

## RULE-ENFORCING TESTS (these exist to block a class of change)
| Test | Enforces |
|---|---|
| `test_deploy_config.py` | Every `Settings` field appears in BOTH compose files and `.env.example`; P0 controls present; `AUTO_TRADE_ALLOW_SHORT_ENTRIES` **absent** from `.env.example` (a bypass must not look operator-supported). Docstrings record the incident where a flag reached `.env` but not compose and silently never took effect |
| `test_pre_submit_risk_boundary_topology.py` | Every order path crosses `pre_submit_risk_check()` exactly once; exactly one broker mutation |
| `test_strategy_v2_preregistration.py` | SHA-256 over frozen v5 parameters. Comment: "Never update the hash to silence the test" |
| `test_config.py` | P0 defaults fail-closed; 14 env overrides cannot loosen hard limits |
| `test_ws.py` | prod + empty API key must reject, not silently allow |
| `test_strategy_schemas.py`, `test_api.py`, `test_trade_execution_service.py` | Schema/API/service layers each reject shorts and add-ons independently |

Adding a `Settings` field is a four-part change: `config.py` + both compose files + `.env.example` + an assertion here.

## COMMANDS

### Parallel Scheduling

`pytest.ini` supplies `-n 4 --dist loadgroup`. `conftest.py` is the source of
truth for module groups, audited test-level parallelism, and realtime exclusions.
Keep `RELIABILITY_DB_GROUP` together. Only modules in `TEST_LEVEL_SAFE_MODULES`
may distribute individual tests; expanding that set requires an isolation audit
and repeated validation alongside other modules.

For a split full-suite run, execute all four numbered shards (indices 1..4 with
`AUTO_TRADE_TEST_SHARD_COUNT=4`) and the realtime lane. Keep the total local worker
budget at 4 initially, rather than launching four 4-worker jobs. Each invocation
needs a distinct `COVERAGE_FILE`, log, and captured exit code. Finish ordinary
shards before the realtime lane on a shared machine. Follow the existing CI
coverage-combine step and enforce 80% only on the complete combined result.

The realtime lane is intentionally serial: these tests spawn compute processes
and assert wall-clock deadlines. Run from `backend/`:

```bash
AUTO_TRADE_ENV=test AUTO_TRADE_TEST_REALTIME_ONLY=1 \
  COVERAGE_FILE=.coverage.shard-realtime \
  python3 -m pytest tests/ -o addopts= -p no:xdist -v --durations=20 \
  --cov=app --cov-config=.coveragerc --cov-report=
```

Clear shard/realtime selection variables before an unsplit full-suite run.
Use `-v --durations=20` to expose the current slow test. A busy single core can
be a serial test or a spin loop; profile it before changing scheduling. Do not
remove deadline assertions or isolation groups just to improve throughput.

```bash
cd backend
python3 -m pytest tests/ -v                 # pytest.ini adds --cov=app --cov-fail-under=80
python3 -m pytest tests/test_engine.py -v
```
`.coveragerc` omits `main.py`, `runner.py`, `database.py`, `config.py`, `api/ws.py` from the 80% gate — coverage there is not required, correctness still is.

## ANTI-PATTERNS (THIS DIR)
- Adding fixtures to `conftest.py`.
- `MagicMock` where a 10-line `_Fake` would do.
- Sharing a DB file between test modules, or unlinking a shared database for cleanup (the runner plugin's disposed private database is the explicit exception).
- Weakening a rule-enforcing test (updating a pinned hash, deleting a compose assertion) instead of fixing the code.
- New subdirectories under `tests/` beyond `platform/` and `fixtures/`.
