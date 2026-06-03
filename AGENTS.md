# Repository Guidelines

> **Last refreshed:** 2026-06-03 / commit `c2fad57` (post-P23a'). Knowledge base regenerated via `/init-deep` (update mode). Test baseline: `pytest 730 passed`, `basedpyright 0 errors / 0 warnings / 0 notes`, `vue-tsc` clean. Audit #17 closed.

Full-stack automated range-trading system for Longbridge (HK/US equities). Backend: Python 3.11+ FastAPI + SQLAlchemy 2.0 + SQLite. Frontend: Vue 3.5 + Vite 5 + Element Plus 2.8 + TypeScript (strict). LLM interval advisor via DeepSeek API (optional). Docker Compose deployment (nginx serving built SPA, reverse-proxying to uvicorn backend).

**Distinct subdirectory deep-dives** (root-only docs are insufficient for these — see linked AGENTS.md):

- `backend/app/domain/prompt/AGENTS.md` — LLM prompt plugin architecture (6 PromptModule subclasses + builder + feature selector). Pure computation, no I/O. Not duplicated in this root file.

---

## Architecture & Data Flow

```
FastAPI (async lifespan) ─── mount 11 routers
   │
   ├── api/auth.py            [API key auth via FastAPI Depends]
   ├── api/deps.py            [audit logger singleton + actor extraction]
   │
   ├── services/              [business logic layer]
   │   ├── strategy_service.py, credentials_service.py   [CRUD wrappers over ORM]
   │   ├── data_aggregator.py, llm_advisor_service.py    [market data + DeepSeek LLM]
   │   ├── trade_execution_service.py                      [order lifecycle, position tracking]
   │   ├── interval_application_service.py                 [guardrail-validated suggestion apply]
   │   ├── runtime_state_service.py                        [engine/risk state persistence]
   │   ├── review_service.py, event_list_service.py        [read-side aggregation]
   │   ├── daily_pnl_service.py                            [P&L from ledger fills]
   │   └── llm_interaction_service.py, trade_event_service.py [event recording]
   │
   ├── domain/                [pure computation, no I/O]
   │   ├── prompt/            [PromptModule ABC → 6 modules + PromptBuilder + FeatureSelector]
   │   ├── analysis/          [TechnicalIndicators (11 indicators) + MarketStateDetector]
   │   ├── sentiment/         [MarketSentimentAnalyzer: price→bullish/bearish/neutral]
   │   ├── performance/       [PerformanceTracker: experiment win-rate analytics]
   │   └── experiment/        [ABTestManager: prompt versioning, deterministic variant]
   │
   ├── core/                  [infrastructure / domain primitives]
   │   ├── broker.py          [BrokerGateway: Longbridge SDK wrapper with retry]
   │   ├── engine.py          [StrategyEngine: price trigger + state machine + cooldowns]
   │   ├── risk.py            [RiskController: daily loss + consecutive loss + pause]
   │   ├── fees.py            [fee estimation helpers]
   │   ├── backtest.py        [BacktestEngine: frozen dataclass + CSV parser]
   │   ├── audit.py           [AuditLogger: SQLAlchemy-based audit with silent failure]
   │   ├── market_calendar.py [MarketSession: US/HK trading hours, UTC conversion]
   │   ├── credential_crypto.py [AES-GCM + RSA hybrid secret encryption]
   │   └── notifiers/         [MultiChannelNotifier, ServerChan, Webhook]
   │
   ├── runner.py              [AppRunner: main loop via threading, live order checks, LLM]
   ├── database.py            [sync SQLAlchemy engine, SessionLocal, get_db, manual ALTER migrations]
   ├── models.py              [~15 ORM models: StrategyConfig, OrderRecord, LLMInteraction…]
   ├── schemas.py             [~40 Pydantic v2 models for request/response]
   └── config.py              [Settings via pydantic-settings, env vars, credential aliases]
```

### Frontend Data Flow

```
View (e.g. Dashboard.vue)
  │
  ├─ imports composables (useDashboardData, useStatusStream, useAccountRefresh)
  │     └─ composables call API functions from ../api/*
  │           └─ API functions use shared axios instance (api.get/post/put/delete)
  │                 └─ axios hits /api/* → vite proxy (dev) or nginx (prod) → backend
  │
  ├─ imports typed API functions directly for one-off calls
  ├─ imports label functions from utils/labels.ts for display transforms
  ├─ uses element-plus components (el-table, el-form, el-button, el-menu)
  └─ renders child components (PriceChart, PnLChart, BacktestChart)
       └─ props: typed via defineProps<{ points: StatusHistoryPoint[] }>()

composables/useStatusStream.ts:
  └─ WebSocket → ws://host/ws
       └─ onmessage: parses JSON, mutates shared status ref
            └─ view reacts via watch/computed on status.value
```

### Key Architecture Decisions

- **Synchronous trading loop**: `AppRunner` uses `threading.Lock` + `threading.Event`, not asyncio. Broker SDK and DB operations are synchronous. Bridge from async contexts via `asyncio.to_thread()`.
- **Prompt as plugin architecture**: LLM prompts via `PromptModule` ABC — 6 composable modules + `FeatureSelector` for dynamic indicator gating.
- **Two-pass LLM flow**: Preview (throttled, 60s cooldown) → suggestions applied via `IntervalApplicationService` with guardrails (min width, profit %, no chasing). Analysis (2-min cron) → can produce order actions.
- **Hybrid credential encryption**: AES-GCM + RSA via `credential_crypto.py`. Plaintext only through `CredentialsService.get_plain_credentials()`.
- **Bilingual prompts**: LLM prompts in Chinese (quant role, instructions); code, logs, data models in English. JSON keys in prompts stay English.
- **No charting library**: Frontend charts are pure SVG path rendering (PnLChart, PriceChart, BacktestChart).

---

## Key Directories

| Path | Purpose |
|---|---|
| `backend/app/core/` | Infrastructure primitives: broker wrapper, engine (state machine), risk, fees, backtest, audit, market calendar, notifiers, credential crypto |
| `backend/app/services/` | Business logic: CRUD wrappers, order execution, LLM advisor, data aggregation, review, event timeline, interval application |
| `backend/app/domain/` | Pure computation: prompt modules, technical indicators, market state, sentiment, performance tracker, A/B test manager |
| `backend/app/api/` | FastAPI routers: strategy, trade, control, LLM, backtest, watchlist, review, credentials, indicators, experiments, performance, WebSocket |
| `frontend/src/views/` | 9 page components: Dashboard, Strategy, TradeHistory, DecisionTimeline, Backtest, Credentials, Watchlist, Review, Lab |
| `frontend/src/api/` | Per-domain axios HTTP clients: strategy, trade, events, credentials, llm_advisor, backtest, watchlist, lab |
| `frontend/src/composables/` | Shared reactive state: useDashboardData, useStatusStream (WebSocket), useAccountRefresh, useFormState |
| `frontend/src/components/` | SVG chart components: PnLChart, PriceChart, BacktestChart |
| `frontend/cypress/e2e/` | Cypress E2E specs (17 files), all API stubbed via `cy.intercept` |

---

## Development Commands

### Backend

```bash
cd backend

# Run dev server (hot reload)
uvicorn app.main:app --reload --port 8000

# Run tests
python3 -m pytest tests/ -v

# Type check (requires local pyrightconfig.json; project targets zero errors)
python3 -m basedpyright
```

### Frontend

```bash
cd frontend

# Dev server (port 3000, proxies /api and /ws to localhost:8000)
npm run dev

# Build (type-check then Vite build)
npm run build

# Type check only
npm run type-check

# E2E tests (headless)
npm run cypress:run

# E2E tests (interactive)
npm run cypress:open
```

### Docker

```bash
# Build and run from source (recommended)
docker compose up --build -d

# Run pre-built images from Docker Hub
docker compose -f docker-compose.dockerhub.yaml up
```

---

## Code Conventions & Common Patterns

### Python

| Aspect | Convention |
|---|---|
| **Classes** | PascalCase (`StrategyEngine`, `BrokerGateway`, `TradeExecutionService`) |
| **Functions/methods** | snake_case, private methods prefixed `_` |
| **Constants** | UPPER_SNAKE_CASE |
| **Imports** | `from __future__ import annotations` at top of every file |
| **Type annotations** | PEP 604 (`X \| None`) standard; fall back to `Optional[X]` in generic alias contexts |
| **Dataclasses** | `@dataclass(frozen=True)` for immutable carriers, `@dataclass` for mutable state |
| **Pydantic** | v2 style: `BaseModel`, `Field()`, `model_validator(mode="after")`, `field_validator` |
| **SQLAlchemy** | sync ORM, `Mapped[x]` + `mapped_column()`, `DeclarativeBase`, `sessionmaker(autocommit=False, autoflush=False)` |
| **DI** | Constructor injection for services; FastAPI `Depends(get_db)` / `Depends(require_api_key())` for routes; module-level singletons with `threading.Lock` |
| **Async** | Route handlers are **sync**. Runner loop is **threading**. Only lifespan/cron/WS are async. Bridge via `asyncio.to_thread()`. |
| **Error handling** | API: `try/except` → `raise HTTPException()`. Broker: retryable exception tuple + marker detection. Audit: swallowed with `logger.warning()`. |

### TypeScript / Vue

| Aspect | Convention |
|---|---|
| **Component API** | `<script setup lang="ts">` — Composition API everywhere |
| **Props** | Typed via `defineProps<{ propName: Type }>()` — no runtime prop validation |
| **State management** | **No Pinia/Vuex**. Composables with module-level `ref()` singletons ARE the state layer. |
| **Routing** | Hash-based (`createWebHashHistory`), not history mode |
| **API client** | Single `axios` instance (client.ts), per-domain modules in `api/` |
| **Type strictness** | `strict: true` in tsconfig. No `as any` or `@ts-ignore`/`@ts-expect-error`. |
| **Path alias** | `@/*` → `src/*` |
| **UI framework** | Element Plus 2.8 components throughout |
| **Labels** | All label mappers centralized in `src/utils/labels.ts` — single source for display transforms |
| **Error feedback** | `ElMessage.success`/`ElMessage.error` for user-facing API feedback |

### Testing

| Aspect | Convention |
|---|---|
| **Framework** | pytest 9, pytest-asyncio 0.24+ (asyncio_mode=auto in pytest.ini) |
| **No unittest** | No `unittest.TestCase` anywhere |
| **conftest** | Single root-level — sets env vars at import time. No pytest fixtures. |
| **DB isolation** | Manual cleanup per test module (`_clean()` helpers). No transaction rollback pattern. |
| **Mocking** | **Inline fake classes preferred** (`_FakeBroker`, `_FakeSession`). `monkeypatch` for env vars. Minimal `unittest.mock.patch`/`MagicMock`. |
| **DB setup** | `Base.metadata.create_all(bind=engine)` at module level or in `setup_class` |
| **Async tests** | Auto-detected via `asyncio_mode=auto`. `@pytest.mark.asyncio` redundant but sometimes present. |

---

## Important Files

| File | Role |
|---|---|
| `backend/app/main.py` | FastAPI app entry; lifespan (runner start/stop), LLM analysis cron, router mounts |
| `backend/app/config.py` | pydantic-settings `Settings` — all env vars with defaults/aliases |
| `backend/app/database.py` | SQLAlchemy engine, `init_db()`, runtime `_ensure_*` column migration functions |
| `backend/app/models.py` | All 15 ORM models |
| `backend/app/schemas.py` | All ~40 Pydantic v2 request/response schemas |
| `backend/app/runner.py` | `AppRunner` — main trading loop, threading, LLM orchestration, WS broadcast |
| `backend/app/core/broker.py` | `BrokerGateway` — Longbridge SDK wrapper with retry/backoff |
| `backend/app/core/engine.py` | `StrategyEngine` — FLAT/LONG/SHORT state machine |
| `backend/app/core/risk.py` | `RiskController` — daily loss limit, consecutive loss counter, kill switch |
| `backend/app/services/llm_advisor_service.py` | `LLMAdvisorService` — DeepSeek caller, throttled analysis/preview |
| `backend/app/services/trade_execution_service.py` | `TradeExecutionService` — order lifecycle, tracked entries, HK tick table |
| `frontend/src/router/index.ts` | Hash routes (9 routes + catch-all) |
| `frontend/src/types/index.ts` | All ~45 TypeScript interfaces |
| `frontend/src/composables/useStatusStream.ts` | WebSocket manager + polling fallback |
| `frontend/src/utils/labels.ts` | Centralized label mappers for all display transforms |
| `frontend/cypress/support/e2e.ts` | Shared E2E stubs — `stubApi()` intercepts all endpoints |

---

## Runtime / Tooling Preferences

| Requirement | Version / Tool |
|---|---|
| Python | 3.11+ (3.11-slim in Docker) |
| Node.js | 20.x (20-alpine in Docker) |
| Python package manager | pip (no `pyproject.toml` — `backend/requirements.txt` + `backend/requirements-dev.txt`) |
| Node package manager | npm (uses `npm ci` in Docker, `npm install` local) |
| Python type checker | `basedpyright` (not mypy) |
| TS type checker | `vue-tsc --noEmit` (strict mode) |
| Python linter | **None** (intentional — no ruff/flake8/pylint in dev deps) |
| Frontend linter | **None** (intentional — no eslint/prettier in dev deps; no `.editorconfig`) |
| Database | SQLite (no PostgreSQL/MySQL support) |
| Deployment | Docker Compose (2 containers: backend internal, frontend via nginx) |
| CI | GitHub Actions — backend-test + frontend-check gate dockerhub publish (P21) |

### Notable Constraints

- `pyrightconfig.json` is in `.gitignore` (developer-local). Project targets zero `basedpyright` errors.
- Frontend Docker build includes type-check (`vue-tsc + vite build`); a TS error blocks the production build.
- CI has **no test step** — quality gates are local only. (P21 added CI quality gates via `.github/workflows/dockerhub.yml` — see below.)
- Docker Hub images use `latest` tag promiscuously (pushed on every default-branch commit).
- No multi-arch Docker builds.
- `docker-entrypoint.sh` mutates `alembic.ini` at runtime via `sed`.

### Non-Obvious Config (not covered above)

- **No `pyproject.toml`.** Backend deps split into `backend/requirements.txt` + `backend/requirements-dev.txt`; pytest config in `backend/pytest.ini`. Deliberate minimal-tooling choice.
- **No `.editorconfig` / `.prettierrc` / `.eslintrc`** at any level. Python style: `basedpyright` only. Frontend style: `vue-tsc` only. No auto-formatter.
- **Cypress `baseUrl` defaults to `http://localhost:8080`** (Docker/nginx) not `:3000` (Vite dev) — E2E tests target the deployed path.
- **`docker-compose.dockerhub.yaml` is the prebuilt-image variant** — binds `127.0.0.1:8080`, sets `AUTO_TRADE_API_KEY` as required env, uses `pull_policy: always`. The main `docker-compose.yaml` is build-from-source + `0.0.0.0:8080` (LAN-friendly).
- **`LONGPORT_*` is canonical**, but `LONGBRIDGE_*` is silently accepted via `merge_longbridge_credentials()` in `app/config.py`. Both old + new deploy scripts work.
- **`.worktrees/`** in repo root is leftover from `using-git-worktrees` skill usage; **not part of main**, ignore in file counts / glob patterns.

---

## Testing & QA

### Backend Tests

- **50 test files** in `backend/tests/` (incl. `test_e2e_restart.py` from P23a'), pure pytest 9.0.
- **pytest-asyncio** with `asyncio_mode = auto` — async test functions handled transparently.
- DB isolation: each module sets its own `AUTO_TRADE_DATABASE_URL` to an isolated SQLite file. No parallel write contention (runs serially).
- conftest.py only sets env vars (DB URL per PID, credential key path) — **no fixtures**.
- Mocking: **inline fake classes** preferred over patching. `_FakeBroker`, `_FakeSession`, `_NoopNotifier` defined per test module.
- E2E restart coverage: `test_e2e_restart.py` (P23a') uses `TestClient(app)` + per-PID SQLite + fake broker to exercise full lifespan → runner.start → reconcile cycle. 5 scenarios: tracked_entries+drift, unresolved live order, pending timeout, refresh sync, start/stop state-machine.
- Class-based grouping: `TestXxx` classes with `def test_*` methods. Helper methods prefixed `_`.
- Coverage mix:
  - **Pure unit** (no DB, no IO): ~15 files (indicators, engine, risk, fees, calendar, sentiment, prompt modules)
  - **Unit with fakes**: ~10 files (runner, broker, execution service, data aggregator)
  - **Integration with DB + mocked broker**: ~12 files (API tests, service tests)
  - **DB migration tests**: `test_database.py`
  - **HTTP API tests**: ~8 files via `TestClient(app)`
  - **Notifier HTTP mocking**: 3 files (mock httpx)
  - **Deployment consistency**: `test_deploy_config.py` — asserts env var names across Docker/config files

### Frontend Tests

- **Cypress 15** for E2E, 17 spec files in `frontend/cypress/e2e/`.
- `support/e2e.ts`: `cy.stubApi()` command intercepts **all** API endpoints via `cy.intercept` — no real backend needed.
- `cy.visitApp()` custom command for navigation.
- Viewport: 1280x720 desktop, plus `mobile_smoke.cy.ts` at 390x844.
- All feature specs: dashboard, strategy, LLM, history, events, backtest, controls, credentials, watchlist, navigation, lab, mobile.

### Running Tests

```bash
# Backend tests (run from backend/)
cd backend && python3 -m pytest tests/ -v

# Frontend E2E (run from frontend/)
cd frontend && npm run cypress:run

# Run a single test file
cd backend && python3 -m pytest tests/test_engine.py -v
```

---

## Anti-Patterns (This Project)

- `as any` / `@ts-ignore` / `@ts-expect-error` — never suppress TS errors.
- Commit without explicit user request.
- Delete failing tests to make CI green.
- Empty catch blocks (`catch(e) {}`).
- `os.environ.pop` in tests — use `monkeypatch.delenv`.
- Submit `.env`, API credentials, or `credential_private_key.pem`.
- Blocking `asyncio` in FastAPI lifespan — use `asyncio.to_thread()`.

---

## Unique Domain Concepts

- **Skip categories**: Order rejections use `skip_category ∈ {FEE, REPRICING, COOLDOWN, RISK, PENDING, POSITION}`; frontend `skipCategoryLabel` is the single rendering source.
- **Tracked entries**: Weighted average cost basis persisted in `tracked_entries` table; `AppRunner` loads on startup.
- **Fee guard**: `_profit_guard_for_exit` subtracts estimated round-trip fees from expected profit before allowing non-loss exits.
- **LLM action cooldown**: `_last_llm_action_at[(symbol, broker_side)]` updated only on FILLED/SUBMITTED/PARTIAL_FILLED.
- **Market calendar**: `trade_day_for()` returns the exchange's local calendar day (not holiday-aware); used for daily PnL / risk reset.
- **Prompt modules**: 6 composable modules (System, Context, Strategy, Output, Selection, Sentiment) assembled by `PromptBuilder` — makes prompts testable without string formatting.
