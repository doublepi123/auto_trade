# backend/app/api/

## Responsibility

HTTP/WebSocket **presentation layer** (FastAPI routers) for the backend: 93 router
modules plus 3 support modules (`auth.py`, `deps.py`, `trade_export.py`), exposing
**245 HTTP routes and 1 WebSocket** (`/ws`). Routers translate HTTP requests into
service / runner calls and map service errors to `HTTPException`. They hold no
business logic of their own apart from a few documented leaks (see Integration).

Two very different populations share this folder:

1. **Operating surface** (~20 routers): trade control, orders, strategy config, credentials,
   watchlist, universe, shadows, reports, alerts, notifications. Some routes mutate
   live state (pause/resume/kill-switch, strategy config, trading symbol, order cancel).
2. **Read-only analytics** (50 single-purpose routers, 60 routes, **all `GET`**):
   one router per `/api/<metric>` page (edge quality, drawdown duration, Kelly,
   Monte Carlo, …), each wrapping one record-only service.

The research platform routes (`/api/platform/*`, 202 routes; `/api/portfolio/*`) live in
`app/platform/api.py` and `app/platform/portfolio_api.py`, **not here**. See
[`../platform/codemap.md`](../platform/codemap.md).

## Design

- **Sync handlers.** 321 of 323 handler `def`s are synchronous (FastAPI runs them in
  its threadpool). Only `ws.py` is async. `/api/health`, `/api/health/db-stats`, and
  `/api/ready` are defined directly in `main.py`, not here.
- **Router declaration.** Each module declares `router = APIRouter(prefix="/api/<name>",
  tags=[...], dependencies=[Depends(require_api_key())])`. Core routers use `prefix="/api"`
  (`trade.py`, `strategy.py`, `llm_advisor.py`, `credentials.py`, `calendar.py`, plus
  `indicators.py`, `cron_health.py`, `quote_health.py`) and attach the auth dependency
  per route instead. `alert_rules.py` exports two routers (`/api/alert-rules`,
  `/api/alert-firings`). **Every one of the 245 HTTP routes is authenticated**, either
  at router level or per route.
- **Auth (`auth.py`).** `require_api_key()` is a dependency factory that compares
  `X-API-Key` with `settings.api_key` via `secrets.compare_digest`. When the key is empty,
  requests pass in `dev`/`test` (with a single warning) and get **401** in any other env.
  `ws.py::_authenticate_websocket` mirrors the same rules for the `api_key` query param
  or the `x-api-key` header, closing with code `1008` on failure (`1009` for oversized
  messages).
- **Deps (`deps.py`).** Holds the process-wide `AuditLogger` singleton
  (`init_audit_logger()` / `get_audit_logger()`, lock-guarded) and `extract_actor(request)`.
  13 routers write audit rows for mutating calls (trade, strategy, credentials, llm_advisor,
  watchlist, universe, strategy_shadow, strategy_presets, trade_notes, alert_rules,
  reports, audit_pack).
- **DB session.** `Depends(get_db)` (from `app/database.py`, used in 85 modules) yields a
  per-request `Session`. `SessionScopeMiddleware` (pure ASGI, registered in `main.py`)
  sets a per-request attribution `ContextVar` that propagates into the threadpool.
- **Schemas.** `response_model=` types come from `app/schemas.py` (imported by 38 modules).
- **Error mapping.** Handlers catch `ValueError`/`RuntimeError` from services and raise
  `HTTPException` (422 ×32, 500 ×21, 404 ×17, 400 ×16, 503 ×13, 409 ×6, 401 ×2, 501 ×1
  literal sites). **409 = "state conflict, read `detail`"**: resume blocked, strategy
  version conflict, watchlist/universe/quant-v6 publication conflicts. `main.py` registers
  a catch-all `_handle_unhandled_exception` → 500 JSON.
- **Service construction.** Standard pattern is inline `XxxService(db).method(...)` per
  request (e.g. `EdgeQualityService(db).score(...)`). Live-state routes use
  `get_runner()` (`trade.py` 16 call sites, `strategy.py` 5) and never construct
  `TradeExecutionService` themselves.

### Router map: operating surface

| File | Prefix | Routes | Purpose |
|---|---|---|---|
| `trade.py` | `/api` | 18 | Orders page + cancel/cancel-all, trade events + export, audit-log export, account; **control**: `start`/`stop`/`pause`/`resume`, `protective-exit/{enable,disable}`, `kill-switch`/`disable-kill-switch`, `force-resume` (both `/api/force-resume` and `/api/control/force-resume`), `reset-drawdown` |
| `strategy.py` | `/api` | 9 | Strategy config GET/PUT, hard-bound projections, versions/diff/rollback, `/api/status`, `/api/status/history`, **`/api/diagnostics`** (decision funnel + reconciliation state) |
| `llm_advisor.py` | `/api` | 6 | `/api/strategy/llm-interval/{preview,analyze,interactions,status,enable,disable}` (advisory only; never orders) |
| `credentials.py` | `/api` | 4 | Encrypted credential GET/PUT, broker + notification-channel test |
| `calendar.py` | `/api` | 5 | `/api/calendar/{today,session,closures,lookup,coverage}` |
| `watchlist.py` | `/api/watchlist` | 11 | CRUD, quotes, quant rank/score/snapshots, **`/{item_id}/set-trading`** (changes the live symbol via `StrategyService`) |
| `watchlist_quant_v6.py` | `/api/watchlist/quant-v6` | 5 | Publications, members, bindings, artifact bytes by digest |
| `universe.py` | `/api/universe` | 12 | Catalog, latest/runs, range/interval-width fitness, primary candidacy, entry-window overlap, promotion readiness, rotation scorecard, observation health, `POST /refresh` |
| `universe_explainer.py` | `/api/universe-explainer` | 2 | Explain one run / one symbol |
| `strategy_shadow.py` | `/api/strategy-shadow` | 19 | v2 shadow config/status/versions/evaluation/decisions/trades/replay, ADX/exit/bracket/live-exit challengers, portfolio routing, forward validation, frozen-disproof assessment, `signal-edge` |
| `opening_momentum_shadow.py` | `/api/opening-momentum-shadow` | 4 | Shadow status/runs + gated execution status/runs |
| `strategy_presets.py` | `/api/strategy-presets` | 5 | Preset CRUD + `/{id}/apply` (audited write to the strategy config) |
| `strategy_experiments.py` / `experiments.py` | `/api/strategy-experiments` / `/api/experiments` | 8 / 6 | Strategy experiments (run/export); LLM prompt-version A/B |
| `backtest.py` | `/api/backtest` | 10 | Run, list runs, export |
| `review.py` / `reports.py` | `/api/review` / `/api/reports` | 2 / 8 | Review workbench; daily/weekly/monthly/range reports, schedule preview/run/status, export |
| `alert_rules.py` | `/api/alert-rules`, `/api/alert-firings` | 9 | Rule CRUD, evaluate, effectiveness, history, firings |
| `notifications.py` | `/api/notifications` | 4 | Notification log, stats, retry, export |
| `trades.py` / `trade_notes.py` | `/api/trades` / `/api/trade-notes` | 8 / 5 | Closed-trade list/stats/export + analytics; per-order notes |
| `positions.py`, `pnl.py`, `equity.py`, `risk.py`, `broker.py` | `/api/<name>` | 1–2 each | Position PnL, PnL by symbol, equity curve, risk history, candles + buying power |
| `reconciliation.py`, `metrics.py` | `/api/reconciliation`, `/api/metrics` | 2, 1 | Reconciliation status/evidence surface; metrics summary |
| Ops health: `cron_health.py`, `quote_health.py`, `database_health.py`, `durable_job_leases.py`, `intervention_evidence.py` | `/api`, `/api/<name>` | 1 each | Cron/quote/DB/lease health, intervention evidence |
| `audit_log.py`, `audit_pack.py` | `/api/audit-logs`, `/api/audit-pack` | 2, 1 | Audit log list/stats; audit pack export |
| `llm_interactions.py`, `llm_usage.py`, `indicators.py`, `platform_catalog.py` | `/api/<name>` | 1–3 | LLM interaction detail, usage summaries, indicators, platform module catalog |
| `ws.py` | none | WS | `/ws` realtime status push (`ConnectionManager`) |

### Router map: read-only analytics (all `GET`, one service each)

`asymmetry`, `attribution`, `autocorrelation`, `benchmark`, `capital_efficiency`,
`concentration`, `correlation`, `daily_consistency`, `decay_detection`,
`decision_replay`, `distribution_shape`, `drawdown_analysis`, `drawdown_duration`,
`edge_quality`, `execution_quality`, `exit_efficiency`, `fee_drag`, `first_trade`,
`holding_time`, `intraday_seasonality`, `kelly`, `lookahead_analysis`,
`loss_containment`, `milestones`, `momentum_ranking`, `monte_carlo`,
`performance`, `prediction_score`, `profit_concentration`, `profit_factor`,
`r_multiples`, `recovery`, `reentry_analysis`, `regime`, `regime_sensitivity`,
`return_calendar`, `risk_score`, `risk_timeline`, `robustness`, `rolling_metrics`,
`rolling_var`, `scratch_analysis`, `signal_consensus`, `size_impact`,
`skip_analytics`, `streaks`, `strategy_health`, `tag_analytics`,
`time_performance`, `trade_frequency`. Each module name maps to prefix
`/api/<kebab-name>`, e.g. `edge_quality.py` → `/api/edge-quality/score`.

## Flow

1. **Request**: nginx/Vite proxy injects `X-API-Key` → `SessionScopeMiddleware` →
   route match → `require_api_key()` dependency → `get_db` session (+ `get_audit_logger`
   for writes) → sync handler in the threadpool.
2. **Read path**: `XxxService(db).compute(...)` → Pydantic `response_model` → JSON.
3. **Live-control path** (`trade.py`): `get_runner()` → runner/`RiskController` method
   (e.g. resume) → audit row. When the pause condition is unresolved, resume raises
   **409** with the blocking reason in `detail`. `force-resume` is the explicit override.
   Order cancel goes through `runner.cancel_order_by_id` (the runner owns broker mutation).
4. **Symbol switch** (`watchlist.py` `set-trading`): fetch a fresh quote for the new symbol
   via the runner's broker, then `StrategyService` updates the primary symbol (503/409 on
   failure).
5. **WebSocket**: `/ws` → `_authenticate_websocket` → `manager.connect`. The runner thread
   pushes status via `asyncio.run_coroutine_threadsafe(manager.broadcast(data), loop)`
   (`runner.py`). The `main.py` cron `_ws_cleanup_task` calls `manager.cleanup_stale()`.

> Note: the resume endpoint is **`POST /api/control/resume`** (prefix `/api`), matching
> `README.md`, `frontend/src/api/trade.ts`, the tests, and the root `AGENTS.md` deploy step.

## Integration

- **Mounted by** `app/main.py` (93 `include_router` calls, including the two platform routers).
- **Depends on**: `app.services.*` (bulk of the calls), `app.runner.get_runner`,
  `app.core` (`AuditLogger`, `market_calendar`, …), `app.schemas`, `app.database.get_db`,
  `app.config.settings`.
- **Reverse dependencies (layering leaks, do not extend):**
  - `app/platform/api.py` and `portfolio_api.py` import `require_api_key`, and
    `portfolio_api.py` also imports `extract_actor` and `get_audit_logger`.
  - Four services import the private `_active_fee_rates` from `app/api/trades.py`
    (`analytics_trade_sample_service`, `decision_replay_service`,
    `drawdown_analysis_service`, `strategy_health_service`).
  - `runner.py` imports `app.api.ws.manager`. `auto_primary_switch_service` lazily
    imports `init_audit_logger` from `deps.py`.
- **Consumers**: `frontend/src/api/*` (one client per router family; see
  [`../../../frontend/src/api/codemap.md`](../../../frontend/src/api/codemap.md)),
  Cypress stubs, and `backend/tests/test_api*.py`.
