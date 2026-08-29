# CLAUDE.md

> **Last refreshed:** 2026-08-29 / commit `51400c1`. Prefer `README.md` for user-facing product docs and `AGENTS.md` for concise agent conventions. This file is the Claude Code working guide for this repo (overrides unrelated global CLAUDE.md elsewhere).

## Project overview

`auto_trade` is a Longbridge (Longport) OpenAPI **range-trading** system with a large **read-only research layer**:

| Layer | Role |
|---|---|
| **Live (P0)** | Interval strategy state machine, risk, order execution, multi-channel notify, audit |
| **Observation** | Watchlist, universe selection, quant v5 scores, Strategy v2 / opening-momentum / portfolio shadows & challengers |
| **Research** | `/api/platform/*` plugin SDK, paper broker, portfolio analytics (200+ pure modules) |
| **UI** | Vue 3 + Vite + Element Plus + TS (hash router, 13 pages) |

**P0 hard defaults:** no short entries, no position add-ons, LLM never submits live orders, shadow/challenger paths never submit orders or auto-promote.

**Deploy assumption:** controlled/private network. Compose binds frontend to `127.0.0.1:${AUTO_TRADE_FRONTEND_PORT:-8080}` by default; `AUTO_TRADE_API_KEY` required in `prod`.

## Layout (current)

```
auto_trade/
├── backend/app/
│   ├── main.py                 # lifespan, crons, 90+ include_router
│   ├── config.py               # Settings (AUTO_TRADE_* / LONGPORT_*)
│   ├── runner.py               # AppRunner (threaded live loop + shadow jobs)
│   ├── api/                    # routers (strategy, trade, watchlist, universe,
│   │                           # strategy_shadow, opening_momentum_shadow, review, …)
│   ├── core/                   # broker, engine, risk, fees, backtest, audit, calendar
│   ├── domain/                 # pure: prompt, strategy_v2, universe_selection, opening_momentum
│   ├── services/               # execution, LLM, universe, quant, shadows, review, PnL
│   ├── platform/               # research/plugin layer + /api/platform/*
│   └── strategies/             # example platform strategy plugins
├── frontend/src/               # views, api, composables, components
├── docker-compose*.yaml
├── .env.example
├── AGENTS.md
└── README.md
```

Deep-dive for prompt plugins only: `backend/app/domain/prompt/AGENTS.md`.

## Working rules

1. **Do not wire research/shadow to live orders** without an explicit product decision.
2. **Keep P0 safety flags hard** — env “compat” toggles that claim to enable shorts/add-ons/LLM live orders must remain no-ops.
3. **Route handlers stay sync**; only lifespan/cron/WS are async. Use `asyncio.to_thread()` for blocking work.
4. **Prefer codegraph / AGENTS / README** over inventing architecture. Intermediate design docs under `docs/` are **not in git** — do not recreate them in-repo unless the user asks for product docs.
5. **Tests:** `cd backend && python3 -m pytest tests/ -v`. Frontend E2E stubs all APIs (`cy.stubApi()`). Never delete failing tests to green CI.
6. **Secrets:** never commit `.env`, keys, pems. Mask credentials in API responses.
7. **Docs ownership:** user-facing → `README.md`; agent conventions → `AGENTS.md`; keep both aligned when behavior changes.

## Commands

```bash
# Backend
cd backend
uvicorn app.main:app --reload --port 8000
python3 -m pytest tests/ -v
python3 -m basedpyright   # needs local pyrightconfig.json (gitignored)

# Frontend
cd frontend
npm run dev
npm run build
npm run cypress:run

# Docker
docker compose up --build -d

# Read-only research scripts (no orders)
cd backend
python3 scripts/evaluate_rotation_walk_forward.py --history-bars 1000
python3 scripts/build_index_membership_snapshot.py
```

## Key safety surfaces

- `AppRunner` + `TradeExecutionService` — only live order path
- `RiskController` — daily loss / consecutive loss / kill switch
- `StrategyEngine` — range FLAT/LONG (short path disabled for live P0)
- Shadow services (`strategy_v2_shadow_service`, `opening_momentum_shadow_service`, portfolio/exit challengers) — **record only**
- `universe_selection_service` / watchlist quant — **watchlist/evidence only** unless operator manually sets primary symbol

## Documentation policy

- Do **not** commit `docs/superpowers/**`, `.omo/**`, `.sisyphus/**`, battery reports, or agent review dumps (gitignored).
- Product behavior changes → update `README.md` (and `AGENTS.md` if agent-facing structure changed).
- Env vars → keep `.env.example` and README env table in sync with `config.py`.
