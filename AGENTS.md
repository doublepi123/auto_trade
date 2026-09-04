# Repository Guidelines

> **Last refreshed:** 2026-09-02 / commit `c1e4760` / branch `main`. Product docs live in `README.md`; this file is for coding agents.

Full-stack automated range-trading system for Longbridge (HK/US equities), plus a large **read-only research layer** (universe selection, quant scoring, Strategy v2 / opening-momentum / portfolio-routing shadows, `/api/platform/*` analytics). Backend: Python 3.11+ FastAPI + SQLAlchemy 2.0 + SQLite. Frontend: Vue 3 + Vite + Element Plus + TypeScript (strict). Optional LLM interval advisor (DeepSeek / MiniMax). Docker Compose deployment (nginx SPA → uvicorn).

**P0 live safety (hard defaults):** no short entries, no position add-ons, LLM never places live orders, shadow / challenger paths never submit orders or auto-promote.

**Distinct subdirectory deep-dives** (read the local file before editing that tree):

| File | Scope |
|---|---|
| `backend/app/services/AGENTS.md` | 129 services; live-order path vs record-only research split |
| `backend/app/platform/AGENTS.md` | 259 research modules + the 202-endpoint `api.py` pattern |
| `backend/app/domain/AGENTS.md` | Purity contract for the 9 pure-computation subpackages |
| `backend/app/domain/prompt/AGENTS.md` | LLM prompt plugin architecture (PromptModule + builder + selector) |
| `backend/tests/AGENTS.md` | 481 test files; DB isolation, fake naming, rule-enforcing tests |
| `frontend/AGENTS.md` | Routes, composable singletons, axios clients, Cypress stubs |

---

## Architecture & Data Flow

```
FastAPI (async lifespan) ─── mount 90+ routers
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
   │   ├── strategy_v2/       [shadow engine, bracket, profit_lock, portfolio_routing,
   │   │                       signal_edge (edge gate ahead of parameter tuning)]
   │   ├── universe_selection/[catalog, selector, rotation walk-forward]
   │   ├── watchlist_quant_v6/[quote-only historical evaluation + evidence publication]
   │   ├── llm_interval_forward/
   │   ├── opening_momentum*  [opening path / extension pure logic]
   │   └── analysis|sentiment|performance|experiment
   │
   ├── core/                  [broker, engine, risk, fees, backtest, audit, calendar, notifiers]
   ├── platform/              [research/plugin SDK, paper broker, portfolio, 250+ analytics modules]
   ├── strategies/            [platform strategy plugins]
   ├── runner.py              [AppRunner: quotes, interval loop, shadow jobs, WS]
   └── config|database|models|schemas
```

### Frontend

Hash routes (`createWebHashHistory`), ~65 total: 13 core operating pages (Dashboard, Watchlist, Review, Reports, Strategy, History, Events, Backtest, Experiments, Credentials, Alerts, Notifications, Lab) plus ~50 single-purpose read-only analytics pages (exit efficiency, edge quality, drawdown duration, regime sensitivity, …) that each render one research view. No Pinia — composable module-level `ref()` singletons. Charts are pure SVG.

### Key Architecture Decisions

- **Synchronous trading loop**: `AppRunner` uses `threading.Lock` + `threading.Event`. Bridge async contexts via `asyncio.to_thread()`.
- **P0 live vs research split**: Live path is range engine + risk + execution. Universe / quant / strategy_v2 / opening-momentum / platform APIs are observation or offline research unless a flag explicitly wires a *read* gate (e.g. live regime gate) — still no auto-promotion.
- **One pre-submit boundary**: every entry passes `TradeExecutionService.pre_submit_risk_check()`, which returns a frozen `ApprovedOrder` (side derived from action; approved price = `max(request price, fresh executable price)` and *that* price is submitted) or a rejection. Exactly one broker mutation sits behind it — `test_pre_submit_risk_boundary_topology.py` spies every path.
- **Prompt plugins**: `PromptModule` ABC + `FeatureSelector` for dynamic indicator gating.
- **Hybrid credential encryption**: AES-GCM + RSA; plaintext only via `CredentialsService.get_plain_credentials()`.
- **Bilingual prompts**: Chinese instructions in LLM prompts; code/logs/models English.

### Real Layer Dependencies (AST-verified, not aspirational)

```
app-root (main/runner/config) → api(99) services(62) core(25) domain(8) platform(4)
api      → services(128) core(27) domain(4) platform(1)
services → domain(56) core(40) platform(2) api(3)   ← back-import, do NOT extend
domain   → core(26) only; zero services imports
platform → api(3) core(1) domain(1)                 ← reuses app.api.auth/deps
strategies → platform(12)
core     → nothing upward
```

Three god modules dominate: [`runner.py`](file:///home/lcy/code/auto_trade/backend/app/runner.py) 8846 lines, [`platform/api.py`](file:///home/lcy/code/auto_trade/backend/app/platform/api.py) 6835 lines / 202 routes, [`strategy_v2_shadow_service.py`](file:///home/lcy/code/auto_trade/backend/app/services/strategy_v2_shadow_service.py) 7390 lines.
 Locate the function, never scan the file.

---

## CODE MAP

`main.py` lifespan: `init_db()` → `init_audit_logger()` → `runner.start()` via `asyncio.to_thread` (failure aborts startup) → optional `PlatformRunner` → 11 cron-health registrations → `LivenessWatchdog` → **12 background asyncio tasks** → **93 `include_router`** calls. Each cron holds its own `asyncio.Lock`; `_opening_research_quiet_window()` makes research crons yield around the open.

`AppRunner`: one daemon `_run_loop` thread on a 5s cycle (resubscribe → pending reconcile → today-order sync → auto-resume → position reconcile → engine/position sync → stale-quote refresh → silent-feed resubscribe → state persist → funnel housekeeping), plus a `post-fill-persist` daemon thread. Locks: `_start_lock`, `_state_lock` (RLock), `_order_persistence_lock`, and `TradeExecutionService.submission_guard()`. Quote hot path: broker WS push → `_on_quote` → `_evaluate_quote_trigger` (under `_state_lock`, snapshots engine, calls `StrategyEngine.update_price() -> TriggerResult`) → `_broadcast_status()` → `_execute_triggered_order` → `TradeExecutionService`.

| Symbol | Location | Refs | Role |
|---|---|---|---|
| `SessionLocal` | `app/database.py` | 64 | Session factory (WAL + runtime `_ensure_*` migrations) |
| `AuditLogger` | `app/core/audit.py` | 62 | `audit_logs` writer; **failures swallowed by design** |
| `get_runner()` | `app/runner.py` | 56 / 16 files | Process-wide AppRunner accessor |
| `BrokerGateway` | `app/core/broker.py` | 55 | Longbridge gateway + tiered `_call_with_retry` |
| `RiskController` | `app/core/risk.py` | 50 | Daily loss / drawdown / consecutive loss / kill switch / `TradingState` |
| `StrategyService` | `app/services/strategy_service.py` | 42 | Strategy config CRUD + primary runtime state |
| `record_trade_event()` | `app/services/trade_event_service.py` | 34 | Sole `trade_events` writer (Decision Timeline source) |
| `trade_day_for()` / `is_trading_hours()` | `app/core/market_calendar.py` | 28 / 22 | Exchange-local day cut + RTH |
| `StrategyV2ShadowService` | `app/services/strategy_v2_shadow_service.py` | 22 | v2 shadow tick / replay (never orders) |
| `DailyPnlService` | `app/services/daily_pnl_service.py` | 19 | FIFO pairing + PnL replay + `reconcile_risk_state` |
| `TradeExecutionService` | `app/services/trade_execution_service.py` | 1 ctor | Whole live order path; callable-injected, runner-lifetime |
| `StrategyEngine` | `app/core/engine.py` | core | `update_price() -> TriggerResult`; FLAT/LONG + `EngineSnapshot` |

---

## Key Directories

| Path | Purpose |
|---|---|
| `backend/app/core/` | Broker, engine, risk, fees, backtest, audit, calendar, notifiers, crypto |
| `backend/app/services/` | Business logic (execution, LLM, universe, quant, shadows, review, PnL) |
| `backend/app/domain/` | Pure computation (prompt, strategy_v2, universe_selection, watchlist_quant_v6, opening momentum) |
| `backend/app/platform/` | Research/plugin layer + `/api/platform/*` |
| `backend/app/api/` | FastAPI routers |
| `frontend/src/views/` | Page components (13 core + ~50 analytics routes) |
| `frontend/src/api/` | Per-domain axios clients |
| `frontend/src/composables/` | Shared reactive state + realtime health |
| `frontend/cypress/e2e/` | Cypress E2E (API fully stubbed) |

---

## Development Commands

```bash
# Backend
cd backend
uvicorn app.main:app --reload --port 8000
python3 -m pytest tests/ -v      # pytest.ini already adds --cov=app --cov-fail-under=80
python3 -m basedpyright          # backend/pyrightconfig.json is tracked; root one is gitignored

# Frontend
cd frontend
npm run dev               # :3000, proxies /api and /ws to :8000, injects X-API-Key server-side
npm run type-check        # vue-tsc --noEmit
npm run build             # vue-tsc && vite build
npm run cypress:run       # baseUrl defaults to Docker :8080

# Docker
docker compose up --build -d          # AUTO_TRADE_API_KEY is `${VAR:?}` — compose refuses to start without it
docker compose -f docker-compose.dockerhub.yaml up

# Read-only research / ops scripts — DEV IMAGE ONLY
# (prod backend image ships only scripts/import_historical_order_ledger.py)
cd backend
python3 scripts/evaluate_rotation_walk_forward.py --history-bars 1000
python3 scripts/build_index_membership_snapshot.py
python3 scripts/database_maintenance.py [--apply] [--vacuum]   # default PREVIEW; --vacuum needs --apply + no market in RTH
python3 scripts/reconcile_broker_order_ledger.py --start-at 2026-08-01T00:00:00Z   # needs LONGPORT_*; window ≤90d; exit 2 on RECONCILIATION_INCOMPLETE
python3 scripts/audit_synced_fill_cost_basis.py [--window-seconds 120] [--json-out path]
python3 scripts/screen_strategy_plugin_inventory.py --data <minute-bars.json> --symbol NVDA.US
```

CI (`.github/workflows/dockerhub.yml`): backend `pytest tests/ -v` + `basedpyright`; frontend `npm run type-check` + `npm run build`. Both must pass.

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

pytest 9, `asyncio_mode=auto`, no `unittest.TestCase`; conftest sets env only; inline `_Fake*` over MagicMock; Cypress stubs every API via `cy.stubApi()`. Details and the rule-enforcing test inventory: `backend/tests/AGENTS.md`.

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
| `backend/app/services/trade_execution_service.py` | Whole live order path; `pre_submit_risk_check()` boundary |
| `backend/app/services/decision_funnel_service.py` | 9-stage funnel counters → `GET /api/strategy/diagnostics` |
| `backend/app/services/reconciliation_incident_service.py` | Deduped reconcile incidents + capped backoff alerts |
| `backend/app/services/research_artifact_retention_service.py` | Prunes artifact bytes; provenance/checksum rows kept forever |
| `backend/app/core/log_throttle.py` | `RepeatedLogThrottle` + `HealthcheckAccessFilter` |
| `backend/app/domain/strategy_v2/PREREGISTRATION.md` | Frozen-v5 governance contract (in-package, not `docs/`) |
| `backend/app/services/universe_selection_service.py` | Dynamic candidate pool |
| `backend/app/services/strategy_v2_shadow_service.py` | Strategy v2 forward shadow |
| `frontend/src/router/index.ts` | Hash routes |
| `frontend/src/types/index.ts` | Shared TS interfaces |
| `.env.example` | Env template (keep in sync with Settings) |

New tables since the last refresh (all with `_ensure_*` runtime migrations in `database.py`): `order_terminal_callbacks`, `reconciliation_incidents`, `decision_funnel_session_summaries`.

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

## Autonomous Commit & Deploy

**Default: commit, deploy, and resume trading without asking.** Do not stop to request permission at each step. The gates below are the authorization — passing them *is* the approval, failing one is a hard stop.

### The gates (ALL must pass, in order)

| # | Gate | Binary pass condition |
|---|---|---|
| 1 | RED→GREEN | Every behavior change has a test that failed first for a *behavioral* reason (not import/fixture/syntax), then passes |
| 2 | Type check | `python3 -m basedpyright` → exit 0 |
| 3 | Full suite | `python3 -m pytest tests/ -v` → **no NEW failures vs pristine HEAD** |
| 4 | Post-deploy health | After restart: `/api/health` ready, `/api/diagnostics` coherent, container logs free of new ERROR/traceback |

**Gate 3 requires a pristine baseline.** Never classify a failure as "pre-existing" from a `git stash` alone: `stash` leaves the untracked `.env` in place, and `Settings` loads `env_file=("../.env", ".env")` (`config.py`), which injects prod config into tests and manufactures unrelated failures. Compare against `git worktree add /tmp/<name> HEAD` and run both sides. Local runs need `AUTO_TRADE_ENV=test`; CI has no `.env`.

### Sequence

1. Gates 1–3 pass → `git commit` (atomic; production + its tests in one commit; never `.env`, secrets, or `credential_private_key.pem`).
2. `git push origin main`. Committing without pushing is not shipping: the work is invisible to CI, to Docker Hub, and to every other machine.
3. Watch the run to a conclusion (`/repos/:owner/:repo/actions/runs?branch=main`). Compare its failing jobs against the **previous** run before judging: `main` can already be red, in which case the bar is "no job fails that was not already failing", not "green". A red `main` that predates your change is still a defect to report, not to inherit silently.
4. `docker compose up --build -d`.
5. Gate 4. Verify the deployed image actually contains the change (grep a new symbol inside the container) — a healthy container proves nothing if it is still running the old image.
6. `POST /api/trade/control/resume` if the system is paused and the pause reason is resolved by this change. On `409`, read the `detail`, resolve the named condition, retry; escalate to `force-resume` only when the 409 reason is demonstrably stale.
7. Report what shipped, with the literal gate output as evidence.

Local deploy does not wait on CI — the image is built from the working tree, so a slow or flaky pipeline never blocks restoring a halted system. But CI failing on your own SHA still has to be chased down to a verdict, because `Build and push Docker images` is gated behind every test job: while it is red, no image reaches Docker Hub and any other deployment target silently keeps the old code.

### Rollback (not optional)

If gate 4 fails, roll back immediately — `git revert` the commit, rebuild, confirm health recovers — then report. Never leave a failed deploy running while investigating. Capture the failing evidence *before* reverting.

### Still requires explicit user approval

- Force-resuming when the 409 condition is real and unresolved.
- Disabling a kill switch, or resuming with an open position whose cost basis is unproven.
- Schema/data migrations that are not reversible by `git revert` alone.
- Anything that would relax a P0 invariant (short entries, position add-ons, LLM/shadow order submission).
- Committing when gate 3 shows a NEW failure, however unrelated it appears.

---

## Anti-Patterns

- `as any` / `@ts-ignore` / `@ts-expect-error`
- Committing or deploying work whose gates did not pass (see "Autonomous Commit & Deploy" — the gates are the authorization, not a user prompt)
- Delete failing tests to green CI
- Empty catch blocks
- `os.environ.pop` in tests — use `monkeypatch.delenv`
- Commit `.env`, API secrets, `credential_private_key.pem`
- Blocking asyncio in lifespan — use `asyncio.to_thread()`
- Adding a `Settings` field without also adding it to BOTH compose files — Compose only forwards variables it declares, so the container silently keeps the default (see `test_deploy_config.py`)
- Wiring shadow/research paths to live order submission without explicit product decision
- Submitting intermediate design docs (`docs/superpowers/**`, agent session JSON, battery reports)
- **Bypassing `pre_submit_risk_check()`** or adding a second broker mutation — the topology test spies every path
- **Writing `tracked_entries` from today-order sync**: `_upsert_broker_order` owns the `orders` table ONLY. Cost basis comes solely from confirmed-position snapshots or locally-submitted orders reaching terminal state. Attribution must be decided *before* any durable write; unattributable fills → pause with `ORDER_RECONCILIATION_UNCERTAIN`, never a guessed booking (this once produced a P0-forbidden add-on)
- **Editing frozen v5 parameters** without a preregistration decision — `test_strategy_v2_preregistration.py` pins a SHA-256; updating the hash to silence the test is forbidden
- **`VolumeShareSlippageModel` in plugin cost scenarios** — the dataset has no volume, so it fabricates zero slippage
- Free-form periodic log lines — use `RepeatedLogThrottle` (suppressed counts are reported, not dropped)

---

## Domain Concepts

- **Skip categories**: `FEE | REPRICING | COOLDOWN | RISK | PENDING | POSITION | SESSION` — UI via `skipCategoryLabel`
- **Tracked entries**: Weighted cost basis in SQLite; loaded on runner start; drift → `TRACKED_ENTRY_DRIFT`
- **Fee guard**: Non-loss exits require fee-adjusted profit ≥ `min_profit_amount`
- **Market calendar**: Exchange-local day for PnL/risk reset; static NYSE/HKEX holidays 2024–2027
- **`TradingState`** (`core/risk.py`): `ACTIVE | REDUCING | HALTED`, derived from `RiskController`, never persisted. `REDUCING` rejects position-increasing orders but must still pass reductions and stops; `HALTED` rejects everything.
- **Decision funnel**: 9 stages (quote → evaluation → crossing → trigger → sized → submit attempt → broker ack → persisted, plus pre-submit check and skip classes) exposed at `GET /api/strategy/diagnostics` alongside `order_reconciliation_state`; session rows land in `decision_funnel_session_summaries`.
- **Universe / quant / shadows**: Default off; evidence-only; promotion-readiness is human review only
- **Automatic primary switching**: Opt-in (`AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED`, default off) and the one path that *does* change the live symbol. Deliberately relaxes the otherwise-standing "never auto-switch" rule. A candidate must clear the ADX trend-share ceiling, the reach-rate floor, AND (`..._REQUIRE_SIGNAL_EDGE`, default **true**, fail-closed) a proven signal edge; come from the latest `COMPLETE` selection run marked `selected`; pass `assert_primary_switch_safe`; and get its interval reset around its own last close
- **Reach-rate**: Share of closed shadow trades whose peak favourable excursion cleared 0.4%. Trend share alone says price is not trending, not that swings clear the ~0.14% round-trip cost; measured over 247 trades reach-rate separated winners from losers without exception (85% vs 22%) while trend share ranked barely better than chance. Uses a longer lookback than the bar window because closed trades accumulate ~100x slower
- **Signal edge gate** (`domain/strategy_v2/signal_edge.py`): Prove edge BEFORE tuning parameters. First-passage — driftless `P(target first) = stop/(stop+target)`; a signal at or below that baseline carries no directional information, so no exit re-parameterisation can rescue it, and counting is restricted to one barrier-version cohort (changing barriers changes the baseline). Cluster-robust significance — trades cluster by day across correlated symbols, so per-trade t-statistics overstate by ~`sqrt(trades/days)`; the estimator is trade-weighted by day. Verdicts: `PASS | FAIL | FEE_BLOCKED | INSUFFICIENT_DATA` — `FEE_BLOCKED` separates "fees ate a real edge" from "signal is wrong", and thin evidence is never `FAIL`. Judge on the **net** CI lower bound > 0; gross is reported only as contrast. Edge is assessed across ALL symbols because the entry rule is shared and per-symbol samples never reach the day count.
- **Futility**: `signal-edge.futility` separately reports whether a cost-clearing gross edge remains `ALIVE`, is `FUTILE`, or is still `INSUFFICIENT_DATA`, using preregistered 10 bps cost / 20 bps daily sigma constants, a fixed `mean + 2.0·SE` upper bound, and the verdict evidence floors. It is read-only; abandonment still requires the written human decision in `PREREGISTRATION.md` §9.5.4.
- **Frozen v5 negative control** (`domain/strategy_v2/PREREGISTRATION.md`): v5 keeps running unchanged as a negative control — if the pipeline ever certifies it as having edge, the pipeline is wrong. Promotion needs four ANDs: net CI lower > 0; version-specific first-passage beating its own driftless baseline; ≥60 distinct days and ~180 resolved brackets; deflated Sharpe `distinguishable_from_luck`. Any parameter change resets the evidence clock.
- **Research artifact retention**: Artifact *bytes* expire (30d replay/quant-v6, 90d diagnostic WAIT, 14d ordinary WAIT); provenance/checksum rows and every live evidence table (`orders`, `transactions`, `trade_events`, `audit_logs`, `risk_events`, `tracked_entries`, `strategy_v2_shadow_trades`) are never pruned. `0` disables a window.
- **Prompt modules**: Composable modules assembled by `PromptBuilder`
