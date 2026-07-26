# Repository Guidelines

> **Last refreshed:** 2026-07-27 / commit `771916c`. Product docs live in `README.md`; this file is for coding agents.

Full-stack automated range-trading system for Longbridge (HK/US equities), plus a large **read-only research layer** (universe selection, quant scoring, Strategy v2 / opening-momentum / portfolio-routing shadows, `/api/platform/*` analytics). Backend: Python 3.11+ FastAPI + SQLAlchemy 2.0 + SQLite. Frontend: Vue 3 + Vite + Element Plus + TypeScript (strict). Optional LLM interval advisor (DeepSeek / MiniMax). Docker Compose deployment (nginx SPA → uvicorn).

**P0 live safety (hard defaults):** no short entries, no position add-ons, LLM never places live orders, shadow / challenger paths never submit orders or auto-promote.

**Distinct subdirectory deep-dives:**

- `backend/app/domain/prompt/AGENTS.md` — LLM prompt plugin architecture (PromptModule subclasses + builder + feature selector). Pure computation, no I/O.

---

## Architecture & Data Flow

```
FastAPI (async lifespan) ─── mount 30+ routers
   │
   ├── api/                   [strategy, trade, credentials, watchlist, universe,
   │                           strategy_shadow, opening_momentum_shadow, review,
   │                           reports, alerts, notifications, backtest, platform, …]
   │
   ├── services/              [trade execution, LLM, universe, watchlist quant,
   │                           shadow / challenger / portfolio routing, review, PnL]
   │
   ├── domain/                [pure computation, no I/O]
   │   ├── prompt/            [PromptModule + PromptBuilder + FeatureSelector]
   │   ├── strategy_v2/       [shadow engine, bracket, profit_lock, portfolio_routing]
   │   ├── universe_selection/[catalog, selector, rotation walk-forward]
   │   ├── opening_momentum*  [opening path / extension pure logic]
   │   └── analysis|sentiment|performance|experiment
   │
   ├── core/                  [broker, engine, risk, fees, backtest, audit, calendar, notifiers]
   ├── platform/              [research/plugin SDK, paper broker, portfolio, 200+ analytics modules]
   ├── strategies/            [platform strategy plugins]
   ├── runner.py              [AppRunner: quotes, interval loop, shadow jobs, WS]
   └── config|database|models|schemas
```

### Frontend

Hash routes (`createWebHashHistory`): Dashboard, Watchlist, Review, Reports, Strategy, History, Events, Backtest, Experiments, Credentials, Alerts, Notifications, Lab. No Pinia — composable module-level `ref()` singletons. Charts are pure SVG.

### Key Architecture Decisions

- **Synchronous trading loop**: `AppRunner` uses `threading.Lock` + `threading.Event`. Bridge async contexts via `asyncio.to_thread()`.
- **P0 live vs research split**: Live path is range engine + risk + execution. Universe / quant / strategy_v2 / opening-momentum / platform APIs are observation or offline research unless a flag explicitly wires a *read* gate (e.g. live regime gate) — still no auto-promotion.
- **Prompt plugins**: `PromptModule` ABC + `FeatureSelector` for dynamic indicator gating.
- **Hybrid credential encryption**: AES-GCM + RSA; plaintext only via `CredentialsService.get_plain_credentials()`.
- **Bilingual prompts**: Chinese instructions in LLM prompts; code/logs/models English.

---

## Key Directories

| Path | Purpose |
|---|---|
| `backend/app/core/` | Broker, engine, risk, fees, backtest, audit, calendar, notifiers, crypto |
| `backend/app/services/` | Business logic (execution, LLM, universe, quant, shadows, review, PnL) |
| `backend/app/domain/` | Pure computation (prompt, strategy_v2, universe_selection, opening momentum) |
| `backend/app/platform/` | Research/plugin layer + `/api/platform/*` |
| `backend/app/api/` | FastAPI routers |
| `frontend/src/views/` | Page components (13 routes) |
| `frontend/src/api/` | Per-domain axios clients |
| `frontend/src/composables/` | Shared reactive state + realtime health |
| `frontend/cypress/e2e/` | Cypress E2E (API fully stubbed) |

---

## Development Commands

```bash
# Backend
cd backend
uvicorn app.main:app --reload --port 8000
python3 -m pytest tests/ -v
python3 -m basedpyright   # needs local pyrightconfig.json

# Frontend
cd frontend
npm run dev               # :3000, proxies /api and /ws
npm run build             # vue-tsc + vite build
npm run cypress:run

# Docker
docker compose up --build -d
docker compose -f docker-compose.dockerhub.yaml up

# Read-only research scripts (no orders)
cd backend
python3 scripts/evaluate_rotation_walk_forward.py --history-bars 1000
python3 scripts/build_index_membership_snapshot.py
```

---

## Code Conventions

### Python

| Aspect | Convention |
|---|---|
| Classes | PascalCase |
| Functions | snake_case; private `_` prefix |
| Imports | `from __future__ import annotations` |
| Types | PEP 604 (`X \| None`) |
| Pydantic | v2 (`BaseModel`, `Field`, validators) |
| SQLAlchemy | sync ORM, `Mapped` + `mapped_column` |
| Route handlers | **sync**; only lifespan/cron/WS async |
| Errors | API → `HTTPException`; audit failures swallowed with warning |

### TypeScript / Vue

| Aspect | Convention |
|---|---|
| Components | `<script setup lang="ts">` |
| Props | `defineProps<{ ... }>()` |
| State | Composables, no Pinia |
| Routing | Hash history |
| Strictness | No `as any` / `@ts-ignore` / `@ts-expect-error` |
| Labels | `src/utils/labels.ts` single source |

### Testing

- pytest 9, `asyncio_mode=auto`; no `unittest.TestCase`
- conftest sets env only (no fixtures); per-module DB cleanup
- Prefer inline fakes (`_FakeBroker`) over MagicMock
- Cypress E2E: `cy.stubApi()` intercepts all APIs; `baseUrl` targets Docker `:8080`

---

## Important Files

| File | Role |
|---|---|
| `backend/app/main.py` | Lifespan, crons, router mounts |
| `backend/app/config.py` | All settings / env aliases |
| `backend/app/runner.py` | Live loop + shadow job orchestration |
| `backend/app/core/broker.py` | Longbridge gateway + retry |
| `backend/app/core/engine.py` | Range state machine |
| `backend/app/core/risk.py` | Daily loss / consecutive loss / kill switch |
| `backend/app/services/trade_execution_service.py` | Orders, tracked entries, ticks |
| `backend/app/services/universe_selection_service.py` | Dynamic candidate pool |
| `backend/app/services/strategy_v2_shadow_service.py` | Strategy v2 forward shadow |
| `frontend/src/router/index.ts` | Hash routes |
| `frontend/src/types/index.ts` | Shared TS interfaces |
| `.env.example` | Env template (keep in sync with Settings) |

---

## Tooling Preferences

| Requirement | Version / Tool |
|---|---|
| Python | 3.11+ |
| Node.js | 20.19+ |
| Python packages | pip (`requirements.txt` + `requirements-dev.txt`) |
| Type check | `basedpyright` / `vue-tsc` |
| Linters | None intentional (no ruff/eslint) |
| DB | SQLite only |
| Deploy | Docker Compose (frontend bind default `127.0.0.1:8080`) |

- `pyrightconfig.json` is gitignored (per-developer).
- `LONGPORT_*` canonical; `LONGBRIDGE_*` accepted via `merge_longbridge_credentials()`.
- Intermediate design/plan/review docs under `docs/` are **not tracked** (see `.gitignore`).

---

## Anti-Patterns

- `as any` / `@ts-ignore` / `@ts-expect-error`
- Commit without explicit user request
- Delete failing tests to green CI
- Empty catch blocks
- `os.environ.pop` in tests — use `monkeypatch.delenv`
- Commit `.env`, API secrets, `credential_private_key.pem`
- Blocking asyncio in lifespan — use `asyncio.to_thread()`
- Wiring shadow/research paths to live order submission without explicit product decision
- Submitting intermediate design docs (`docs/superpowers/**`, agent session JSON, battery reports)

---

## Domain Concepts

- **Skip categories**: `FEE | REPRICING | COOLDOWN | RISK | PENDING | POSITION | SESSION` — UI via `skipCategoryLabel`
- **Tracked entries**: Weighted cost basis in SQLite; loaded on runner start; drift → `TRACKED_ENTRY_DRIFT`
- **Fee guard**: Non-loss exits require fee-adjusted profit ≥ `min_profit_amount`
- **Market calendar**: Exchange-local day for PnL/risk reset; static NYSE/HKEX holidays 2024–2027
- **Universe / quant / shadows**: Default off; evidence-only; promotion-readiness is human review only
- **Prompt modules**: Composable modules assembled by `PromptBuilder`
