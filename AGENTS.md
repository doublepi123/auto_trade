# PROJECT KNOWLEDGE BASE

**Generated:** 2026-05-28
**Commit:** fe6f1db
**Branch:** main

## OVERVIEW

Full-stack automated range-trading system for Longbridge (HK/US equities).  
Backend: FastAPI + SQLAlchemy 2.0 + SQLite. Frontend: Vue 3 + Vite + Element Plus + TS.

## STRUCTURE

```
auto_trade/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI entry, lifespan, LLM cron
│   │   ├── config.py            # pydantic-settings (AUTO_TRADE_* / LONGPORT_*)
│   │   ├── database.py          # engine, init_db, runtime _ensure_* migrations
│   │   ├── models.py            # ORM: StrategyConfig, OrderRecord, TrackedEntry…
│   │   ├── schemas.py           # Pydantic API schemas
│   │   ├── runner.py            # AppRunner: quotes, strategy loop, WS broadcast
│   │   ├── api/                 # REST routers (/api/strategy, /api/trade, /api/control, /ws…)
│   │   ├── core/                # broker, engine, risk, backtest, notify, credential_crypto
│   │   └── services/            # business logic (execution, LLM advisor, data aggregator…)
│   ├── tests/                   # pytest; isolated SQLite per file
│   ├── alembic/                 # historical migrations (not used at runtime)
│   ├── requirements.txt
│   └── requirements-dev.txt     # pytest, basedpyright
├── frontend/
│   ├── src/
│   │   ├── router/index.ts      # Hash routes (createWebHashHistory)
│   │   ├── api/                 # per-domain HTTP clients (axios-based)
│   │   ├── composables/         # useDashboardData, useStatusStream…
│   │   ├── components/          # PriceChart, PnLChart, BacktestChart
│   │   ├── views/               # Dashboard, Strategy, TradeHistory, Backtest…
│   │   └── types/index.ts
│   ├── cypress/e2e/             # E2E stubs
│   └── package.json
├── docker-compose.yaml          # frontend exposed on 0.0.0.0; backend internal only
├── docs/Roadmap.md
└── .env.example
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `backend/app/api/*.py` | Mount in `main.py` |
| Change trading logic | `backend/app/core/engine.py` or `services/trade_execution_service.py` | |
| Change LLM behavior | `backend/app/services/llm_advisor_service.py` | Also `_llm_analysis_cron` in `main.py` |
| Change frontend page | `frontend/src/views/*.vue` | Register route in `router/index.ts` |
| Change DB schema | `backend/app/models.py` + `database.py` `_ensure_*` | Also update `schemas.py` |
| Add backend test | `backend/tests/test_*.py` | Use independent `AUTO_TRADE_DATABASE_URL` |
| Build / deploy | `docker-compose.yaml` | CI in `.github/workflows/dockerhub.yml` |

## CODE MAP

| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `AppRunner` | class | `backend/app/runner.py` | Main event loop: quote subscription, strategy loop, WS |
| `BrokerGateway` | class | `backend/app/core/broker.py` | Longbridge SDK wrapper (quotes, candlesticks, orders) |
| `TradeExecutionService` | class | `backend/app/services/trade_execution_service.py` | Order execution, pending reconciliation, tracked entries |
| `StrategyConfig` | model | `backend/app/models.py` | DB strategy parameters |
| `RangeEngine` | class | `backend/app/core/engine.py` | flat/long/short state machine |
| `RiskController` | class | `backend/app/core/risk.py` | Daily loss, consecutive loss, kill switch |
| `Dashboard.vue` | component | `frontend/src/views/Dashboard.vue` | Real-time status, charts, controls |
| `router/index.ts` | module | `frontend/src/router/index.ts` | Hash routes + fallback |

## CONVENTIONS

- **Python type-checking**: `basedpyright` (not mypy). Prefer `X \| None`; fall back to `Optional[X]` in generic alias contexts if PEP 604 errors.
- **Frontend type-checking**: `vue-tsc --noEmit` (strict TS). Path alias `@/*` → `src/*`.
- **Database migrations**: Runtime column patches via `database.py` `init_db()` → `_ensure_*` functions. `alembic/` is historical only and **not executed in production**.
- **Vue routing**: Intentionally uses `createWebHashHistory()` (hash mode) instead of `createWebHistory()`.
- **Tests**: Each test file gets its own SQLite DB (set `AUTO_TRADE_DATABASE_URL` **before** `from app…` imports). `conftest.py` sets a temporary `AUTO_TRADE_CREDENTIAL_KEY_PATH`.

## ANTI-PATTERNS (THIS PROJECT)

- `as any` or `@ts-ignore` / `@ts-expect-error` — never suppress TypeScript errors.
- Commit without explicit user request.
- Delete failing tests to make CI green.
- Empty catch blocks (`catch(e) {}`).
- `os.environ.pop` in tests — use `monkeypatch.delenv`.
- Submit `.env`, API credentials, or `credential_private_key.pem`.
- Blocking `asyncio` in FastAPI lifespan — use `asyncio.to_thread` (already applied in `main.py`).

## UNIQUE STYLES

- **Skip categories**: Order rejections use `skip_category` ∈ `{FEE, REPRICING, COOLDOWN, RISK, PENDING, POSITION}`; frontend `skipCategoryLabel` is the single rendering source.
- **Tracked entries**: Weighted average cost basis persisted in `tracked_entries` table; `AppRunner` loads them on startup.
- **Fee guard**: `_profit_guard_for_exit` subtracts estimated round-trip fees (`fee_rate_us` / `fee_rate_hk`) from expected profit before allowing non-loss exits.
- **LLM action cooldown**: `_last_llm_action_at[(symbol, broker_side)]` updated only on `FILLED/SUBMITTED/PARTIAL_FILLED`.
- **Market calendar**: `trade_day_for()` returns the exchange's **local calendar day** (not holiday-aware); used for daily PnL / risk reset.

## COMMANDS

```bash
# Backend dev
cd backend && uvicorn app.main:app --reload --port 8000

# Frontend dev
cd frontend && npm run dev         # http://localhost:3000

# Backend tests
cd backend && python3 -m pytest tests/ -v

# Backend type check (requires local pyrightconfig.json)
cd backend && python3 -m basedpyright

# Frontend type check
cd frontend && npm run type-check

# Docker (recommended)
docker compose up --build -d
```

## NOTES

- **Python 3.11+ required**; `tests/test_ws.py` fails on 3.9 due to `asyncio.Lock` behavior.
- `backend/.venv` and `frontend/dist` exist in the working tree but are gitignored; do not commit them.
- `pyrightconfig.json` is intentionally in `.gitignore` (developer-local); the project targets zero `basedpyright` errors.
- `AUTO_TRADE_API_KEY` is optional; the project assumes **trusted internal network** deployment.
- `database.engine` binds to `settings.database_url` at import time; in tests, monkeypatch `database.engine` or set the env variable before any `from app…` imports.
