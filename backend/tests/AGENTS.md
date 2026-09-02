# `backend/tests/` — Test Suite

## OVERVIEW
481 test files, ~170k lines, ~6.7k test functions. 216 flat files here + 264 under `platform/`. pytest 9, `asyncio_mode=auto`, no `unittest.TestCase`.

## STRUCTURE
Flat by design. `test_<module>.py` mirrors `app/<...>/<module>.py`; `tests/platform/` mirrors `app/platform/`; `tests/fixtures/` holds replay data (e.g. `pltr_risk_cap_bypass.py`). No other subdirectories — do not introduce one.

## conftest.py — ENV ONLY, NO FIXTURES
51 lines that run once per process:
1. Installs a `MetaPathFinder` that makes `import longport` / `longbridge` raise `ImportError` (escape hatch: `AUTO_TRADE_ALLOW_BROKER_SDK_IMPORTS=1`). Tests therefore always run against fakes.
2. Points `AUTO_TRADE_DATABASE_URL` at `/tmp/auto_trade_pytest_<pid>.db` (override: `AUTO_TRADE_TEST_DATABASE_URL`) and the credential key at a temp path.
3. Blanks ~10 credential env vars; pins deterministic LLM provider defaults.

Add env defaults here; add fixtures **nowhere** — the file deliberately defines none.

## DB ISOLATION — TWO ACCEPTED PATTERNS
**A. Own engine + dependency override** (preferred for API tests, see `test_trades_export.py`): module-level `TEST_DATABASE_URL` including `os.getpid()`; `setup_class` creates the engine, `drop_all` + `create_all`, installs `app.dependency_overrides[get_db]`, builds `TestClient(app)`; `teardown_class` pops the override and disposes; `setup_method` deletes rows table-by-table.

**B. Shared app DB** (see `test_credentials_api.py`): module-level `database.init_db()`, `SessionLocal`, an `autouse` fixture for monkeypatching, and `_clean_<table>()` helpers.

Clean by `db.query(Model).delete()` — never by deleting the DB file. Use `monkeypatch.delenv(..., raising=False)`, never `os.environ.pop`.

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
```bash
cd backend
python3 -m pytest tests/ -v                 # pytest.ini adds --cov=app --cov-fail-under=80
python3 -m pytest tests/test_engine.py -v
```
`.coveragerc` omits `main.py`, `runner.py`, `database.py`, `config.py`, `api/ws.py` from the 80% gate — coverage there is not required, correctness still is.

## ANTI-PATTERNS (THIS DIR)
- Adding fixtures to `conftest.py`.
- `MagicMock` where a 10-line `_Fake` would do.
- Sharing a DB file between test modules, or cleaning up by unlinking the file.
- Weakening a rule-enforcing test (updating a pinned hash, deleting a compose assertion) instead of fixing the code.
- New subdirectories under `tests/` beyond `platform/` and `fixtures/`.
