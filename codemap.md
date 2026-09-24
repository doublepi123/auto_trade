# Repository Atlas: auto_trade

## Project Responsibility

Full-stack **automated range-trading system** for Longbridge (Longport) HK/US equities,
plus a large **read-only research layer** (universe selection, quant scoring,
Strategy v2 / opening-momentum / portfolio-routing shadows, ~250 platform analytics).

- **Backend**: Python 3.11+ FastAPI, SQLAlchemy 2.0, SQLite (WAL), ~200k lines under `backend/app/`.
- **Frontend**: Vue 3 + Vite + Element Plus + strict TypeScript SPA (hash routes, pure-SVG charts), ~47k lines under `frontend/src/`.
- **Deploy**: Docker Compose (`backend` uvicorn + `frontend` nginx SPA that injects `X-API-Key`).
- **Optional LLM** interval advisor (DeepSeek / MiniMax). It is advisory only.

**P0 live-safety invariants** (enforced in code and pinned by tests): no short entries,
no position add-ons, the LLM never places live orders, and shadow/challenger/research
paths never submit orders or auto-promote. Every live entry passes
`TradeExecutionService.pre_submit_risk_check()`, the single pre-submit boundary in front
of the only broker mutation.

## System Entry Points

| Entry | Role |
|---|---|
| `backend/app/main.py` | FastAPI app + async `lifespan`: `init_db()` → audit logger → `AppRunner.start()` → optional `PlatformRunner` → cron-health registration → 13 background asyncio crons → 93 routers; `/api/health`, `/api/ready` |
| `backend/app/runner.py` | `AppRunner`: live quote hot path (`_on_quote` → `StrategyEngine.update_price` → `TradeExecutionService`) + 5 s reconcile/persist loop + shadow jobs + WS broadcast |
| `backend/docker-entrypoint.sh` | Alembic stamp/upgrade (`HEAD_REVISION`) → `init_db()` runtime `_ensure_*` migrations → uvicorn |
| `frontend/src/main.ts` / `App.vue` | SPA bootstrap and shell (palette, theme, session clock, realtime health, notification stream) |
| `frontend/src/router/index.ts` | Flat hash-route table: 64 lazy routes + catch-all |
| `docker-compose.yaml` / `docker-compose.dockerhub.yaml` | Local build vs published images (`doublepi/auto-trade-*`). `AUTO_TRADE_API_KEY` is mandatory; `./data` is mounted at `/app/data`; the frontend binds to `127.0.0.1:8080` by default. Every `Settings` field must be forwarded in **both** files |
| `.env.example` | Env template, kept in sync with `backend/app/config.py` `Settings` |
| `.github/workflows/dockerhub.yml` | CI: sharded `backend-test` + serial `backend-realtime` → `backend-coverage` (80 % gate), `backend-typecheck` (basedpyright), `frontend-check` (vue-tsc + build), `frontend-e2e` (Cypress) → `dockerhub` push, gated on all of them |
| `AGENTS.md`, `CLAUDE.md`, `README.md` | Agent conventions + gates, the Claude working guide, and product/API/config docs (Chinese) |

## Architecture (layers, AST-verified direction)

```
frontend (Vue SPA) ──HTTP /api/* + WS /ws──▶ backend/app/api (93 routers)  +  platform/api.py (/api/platform/*)
                                                  │
app root: main.py (lifespan/crons) · runner.py (live loop) · config · database · models · schemas
                                                  │
api ──▶ services (Service Layer; ONLY trade_execution_service submits orders)
             │            └─▶ core (broker, engine, risk, fees, calendar, audit, notifiers)
             └─▶ domain (pure computation, no I/O; imports core only)
platform (research, read-only) ──▶ core/domain;  strategies (research plugins) ──▶ platform.sdk
core imports nothing upward.  Known leaks (do not extend): services→api (_active_fee_rates), platform→api (auth/deps), runner→api.ws
```

## Repository Directory Map

### Backend

| Directory | Responsibility Summary | Detailed Map |
|---|---|---|
| `backend/` | Backend project root: multi-stage Dockerfile (ships `app/`, `alembic/`, one script), entrypoint, pytest config (xdist `loadgroup`, 80 % coverage gate), dependency pins | [View Map](backend/codemap.md) |
| `backend/app/` | Application package root: `main.py` lifespan + crons, `runner.py` threaded live loop, `config.py` Settings, `database.py` (SessionLocal/WAL + 59 `_ensure_*` migrations), `models.py` (63 tables), `schemas.py` | [View Map](backend/app/codemap.md) |
| `backend/app/api/` | FastAPI presentation layer: 245 authenticated HTTP routes + `/ws`; trade control, strategy config, shadows, and ~50 read-only analytics routers | [View Map](backend/app/api/codemap.md) |
| `backend/app/services/` | Service Layer (134 files): the live order path (`TradeExecutionService`, the sole broker submitter), reconciliation/ledger, PnL/risk, LLM, universe/quant, shadows/challengers, analytics | [View Map](backend/app/services/codemap.md) |
| `backend/app/core/` | Live trading primitives: `BrokerGateway` (tiered retry), `StrategyEngine` state machine, `RiskController`/`TradingState`, fees/board lots, market calendar, audit, crypto, backtest | [View Map](backend/app/core/codemap.md) |
| `backend/app/core/notifiers/` | Multi-channel notification delivery (ServerChan / Telegram / webhook) with severity routing, dedup, and a retry queue | [View Map](backend/app/core/notifiers/codemap.md) |
| `backend/app/domain/` | Pure computation layer (no I/O): 9 subpackages + opening-momentum modules | [View Map](backend/app/domain/codemap.md) |
| `backend/app/domain/strategy_v2/` | v2 shadow engine, bracket/profit-lock, portfolio routing, signal-edge gate + futility, frozen v5 negative control (`PREREGISTRATION.md`) | [View Map](backend/app/domain/strategy_v2/codemap.md) |
| `backend/app/domain/prompt/` | LLM prompt plugin architecture: `PromptModule` + `PromptBuilder` + `FeatureSelector` | [View Map](backend/app/domain/prompt/codemap.md) |
| `backend/app/domain/universe_selection/` | Candidate catalog, point-in-time selection, rotation walk-forward | [View Map](backend/app/domain/universe_selection/codemap.md) |
| `backend/app/domain/watchlist_quant_v6/` | Quote-only historical evaluation with tamper-evident published artifacts | [View Map](backend/app/domain/watchlist_quant_v6/codemap.md) |
| `backend/app/domain/llm_interval_forward/` | Frozen contract + paired replay for counterfactual evidence on rejected LLM intervals | [View Map](backend/app/domain/llm_interval_forward/codemap.md) |
| `backend/app/domain/analysis/` | Technical indicators + market-state detector | [View Map](backend/app/domain/analysis/codemap.md) |
| `backend/app/domain/sentiment/` | Price-derived sentiment score for the LLM advisor | [View Map](backend/app/domain/sentiment/codemap.md) |
| `backend/app/domain/experiment/` | LLM prompt A/B testing (documented ORM purity exception) | [View Map](backend/app/domain/experiment/codemap.md) |
| `backend/app/domain/performance/` | LLM prediction experiment comparison/recommendations | [View Map](backend/app/domain/performance/codemap.md) |
| `backend/app/platform/` | Read-only research layer: 259 pure analytic modules, paper broker, portfolio, `PlatformRunner`, `/api/platform/*` (202 routes) | [View Map](backend/app/platform/codemap.md) |
| `backend/app/platform/sdk/` | Plugin contract: frozen `OrderIntent` + `Strategy` Protocol | [View Map](backend/app/platform/sdk/codemap.md) |
| `backend/app/strategies/` | Research strategy plugins (interval, breakout, trend, mean reversion); **not** the live strategy | [View Map](backend/app/strategies/codemap.md) |
| `backend/app/cli/` | `python -m app.cli.<module>` ops helpers (config validation, LLM storage maintenance, opening research) | [View Map](backend/app/cli/codemap.md) |
| `backend/alembic/` | Alembic environment; coexists with runtime `_ensure_*` migrations | [View Map](backend/alembic/codemap.md) |
| `backend/alembic/versions/` | 15-revision linear chain → head `20260922_fill_intent` | [View Map](backend/alembic/versions/codemap.md) |
| `backend/scripts/` | Research/ops CLIs (preview by default, `--apply` gated); dev image only, except `import_historical_order_ledger.py` | [View Map](backend/scripts/codemap.md) |
| `backend/tests/` | *Not mapped.* 481 test files; see `backend/tests/AGENTS.md` | — |

### Frontend

| Directory | Responsibility Summary | Detailed Map |
|---|---|---|
| `frontend/` | SPA project root: vue-tsc + Vite build, chunk guards, dev proxy (:3000 → :8000 with injected key), nginx Docker image, Cypress (fully stubbed) | [View Map](frontend/codemap.md) |
| `frontend/scripts/` | Post-build chunk-budget guards | [View Map](frontend/scripts/codemap.md) |
| `frontend/src/` | SPA source tree: bootstrap, shell, feature modules | [View Map](frontend/src/codemap.md) |
| `frontend/src/api/` | One typed axios client per backend domain (shared 3-line `client.ts`, no interceptors) | [View Map](frontend/src/api/codemap.md) |
| `frontend/src/views/` | 13 core operating pages + ~51 read-only analytics pages | [View Map](frontend/src/views/codemap.md) |
| `frontend/src/components/` | Pure-SVG chart primitives + shared panels (`DataState`, `StatisticsQualityAlert`, …) | [View Map](frontend/src/components/codemap.md) |
| `frontend/src/composables/` | Module-level `ref()` singletons (no Pinia); realtime WS + polling fallback | [View Map](frontend/src/composables/codemap.md) |
| `frontend/src/router/` | Flat hash-route table, lazy views, path ≠ filename | [View Map](frontend/src/router/codemap.md) |
| `frontend/src/types/` | Single shared contract file (`index.ts`: 230 interfaces + 32 types) | [View Map](frontend/src/types/codemap.md) |
| `frontend/src/utils/` | Leaf helpers; `labels.ts` is the single source for Chinese enum copy | [View Map](frontend/src/utils/codemap.md) |
| `frontend/cypress/` | *Not mapped.* E2E specs; every API is stubbed in `cypress/support/e2e.ts` | — |

## Key Cross-Cutting Flows

1. **Live quote → order**: broker WS push → `AppRunner._on_quote` → `_evaluate_quote_trigger`
   (under `_state_lock`) → `StrategyEngine.update_price() -> TriggerResult` →
   `_execute_triggered_order` → `TradeExecutionService.pre_submit_risk_check()` →
   frozen `ApprovedOrder` → `broker.submit_limit_order` (the only call site) → terminal
   callback → `trade_events` / `tracked_entries` → `/ws` broadcast.
2. **Operator control**: SPA → `POST /api/control/{pause,resume,kill-switch,…}`
   (`api/trade.py`) → runner / `RiskController`. Resume returns **409** with a `detail`
   when the blocking condition is unresolved.
3. **Research**: `main.py` crons (with the `_opening_research_quiet_window()` yield) →
   record-only services → `domain/*` pure computation → evidence tables → `/api/<metric>`,
   `/api/strategy-shadow/*`, `/api/platform/*` → analytics views. Promotion is always a
   human decision.
4. **Schema**: Alembic chain + the entrypoint stamp logic + `database.py` `_ensure_*`
   runtime migrations. A new column must be added in all three places.

## Where to Start

- Live trading behaviour: `backend/app/services/codemap.md` → `backend/app/core/codemap.md` → `backend/app/codemap.md` (runner).
- A research metric end to end: `frontend/src/views/codemap.md` → `frontend/src/api/codemap.md` → `backend/app/api/codemap.md` → the service → `backend/app/domain/codemap.md`.
- Deep-dive rules: the per-folder `AGENTS.md` files (`services/`, `platform/`, `domain/`, `domain/prompt/`, `tests/`, `frontend/`) take precedence over this atlas.
