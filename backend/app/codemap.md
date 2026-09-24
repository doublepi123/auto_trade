# backend/app/

## Responsibility

The application package root for the FastAPI backend: process composition and
startup lifecycle (`main.py`), the live trading loop (`runner.py`), all
configuration (`config.py`), the SQLite engine/session layer plus runtime
migrations (`database.py`), the SQLAlchemy ORM models (`models.py`), and the
Pydantic v2 request/response schemas (`schemas.py`). Everything else in the
package lives in subpackages (`api/`, `services/`, `core/`, `domain/`,
`platform/`, `strategies/`, `cli/`).

## Design

### Per-file role

| File | Lines | Role |
|---|---|---|
| `main.py` | 2,793 | FastAPI app assembly: `lifespan`, CORS + `SessionScopeMiddleware`, 93 `include_router` calls, 13 background asyncio tasks, cron-health registration, LLM interval analysis, opening-research quiet-window helpers. |
| `runner.py` | 9,973 | `AppRunner` (god module — locate the symbol, never scan the file): broker WS wiring, quote hot path, order execution handoff, pending/position/order reconciliation, state persistence, primary-symbol switching. Module-level `get_runner()` / `peek_runner()` behind `_runner_lock`. |
| `config.py` | 1,393 | Single `Settings(BaseSettings)` with all env aliases and defaults, plus `merge_longbridge_credentials()`. |
| `database.py` | 3,074 | Engine + `SessionLocal`, WAL setup, session-scope middleware/guard, `init_db()` with 59 `_ensure_*` runtime migrations. |
| `models.py` | 2,745 | 63 ORM classes (`Mapped` + `mapped_column`, sync ORM), one per `__tablename__`. |
| `schemas.py` | 5,945 | 309 Pydantic v2 `BaseModel` request/response schemas (strategy, shadow, opening momentum, challengers, trusted-frozen reports, …). |

### AppRunner threading model

- One daemon thread (`_run_loop`, 5-second cycle) started by `AppRunner.start()`
  under `_start_lock`; `start()` joins any previous runner thread (10s timeout)
  before starting a new one. Async contexts bridge via
  `asyncio.to_thread(runner.start, loop=...)` from the lifespan.
- Locks:
  - `_start_lock` (`threading.Lock`) — guards start/stop transitions.
  - `_state_lock` (`threading.RLock`) — guards engine/runtimes state; the quote
    trigger evaluation snapshot happens under it.
  - `_order_persistence_lock` (`threading.Lock`) — serializes durable order writes.
  - `TradeExecutionService.submission_guard()` — the single submission
    serialization point shared by sizing, reconciliation, and protective exits.
- A second daemon thread (`post-fill-persist`, spawned per fill settlement)
  replays the ledger, reconciles risk state, persists runtime state, and
  notifies the opening-execution registry — it never blocks the quote path.
- `_run_loop` cycle steps (each wrapped in its own try/except so one failure
  never kills the loop): resubscribe-if-needed → pending-order reconcile
  (`_trade_svc.reconcile`, single call iterates all pendings) → today-order
  sync (`sync_today_orders_from_broker`) → pause auto-resume → board-lot
  refresh → account-exposure refresh → position reconcile
  (`_reconcile_runtime_positions`) → engine/position sync
  (`_sync_engine_state_with_positions`, under `submission_guard`) → stale-quote
  refresh → silent-feed resubscribe → `_persist_runtime_state` →
  decision-funnel housekeeping → `time.sleep(5)`.
- `_persist_runtime_state` plans every row first, then writes in one
  DML-opened transaction (SQLite read-to-write upgrade workaround), skips
  no-op writes so an idle pass takes the single writer zero times, retries up
  to 3 attempts, and alerts on `RUNTIME_STATE_PERSISTENCE_STALLED`.
- `TradeExecutionService` is constructed once in the `AppRunner` constructor
  with runner-lifetime callable injections (`record_order`,
  `persist_entry`, `audit`, `decision_funnel`, entry/exit policy checks, …);
  it is the only path that mutates the broker.

### SessionLocal, WAL, runtime migrations

- `database.py` opens SQLite with `PRAGMA journal_mode=WAL` +
  `synchronous=NORMAL` + busy timeout: many concurrent readers, one writer.
- `SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)`.
- `SessionScopeMiddleware` (outermost ASGI middleware) tags each request with a
  scope token; `SessionReentrancyGuard` attributes pooled checkouts to that
  token instead of the thread id, preventing false re-entrancy storms from
  anyio's split dependency teardown. `independent_session(reason)` opts a
  blocking job out of the pooled request scope.
- `init_db()` runs `Base.metadata.create_all` then 59 idempotent
  `_ensure_*_<table>_<change>` functions that add missing columns/tables,
  enforce uniqueness (e.g. `_ensure_runtime_state_symbol_uniqueness`,
  `_ensure_order_broker_id_uniqueness`), and conservatively backfill — this is
  the migration mechanism (no Alembic).

### Settings env aliases

- `env_prefix="AUTO_TRADE_"`, `env_file=("../.env", ".env")`,
  `populate_by_name=True`, `extra="ignore"`.
- Broker credentials: `LONGPORT_APP_KEY/_SECRET/_ACCESS_TOKEN` are canonical
  (via `validation_alias`); `LONGBRIDGE_*` legacy names are accepted and
  folded in by `merge_longbridge_credentials()`.
- Notable groups: liveness/job-lease tuning; DeepSeek + MiniMax provider
  settings (`DEEPSEEK_*`, `MINIMAX_*`, `llm_provider`); engine defaults
  (`engine_cooldown_seconds=60`, `live_entry_crossing_required`); hard risk
  caps (`hard_max_position_quantity/notional/risk_per_trade`,
  `hard_stop_loss_pct`, `hard_max_holding_minutes`, entry cutoff / flatten
  minutes); P0 switches (`allow_short_entries=False`,
  `hard_allow_position_addons=False`); feature flags defaulting off (universe
  selection, opening-momentum shadow/execution, watchlist quant, auto primary
  switch, interval recenter, `live_regime_gate_enabled`); `platform_mode`.
- New `Settings` fields must be added to both compose files (Compose only
  forwards declared variables — enforced by `test_deploy_config.py`).

### Key tables by domain (`models.py`)

- **Config & runtime state**: `strategy_config`, `strategy_param_versions`,
  `strategy_presets`, `portfolio_config`, `runtime_state`,
  `runtime_state_snapshots`, `durable_job_leases`.
- **Live trading evidence (never pruned)**: `orders`, `fill_settlements`,
  `order_terminal_callbacks`, `transactions`, `tracked_entries`,
  `trade_events`, `risk_events`, `audit_logs`, `event_log`,
  `reconciliation_evidence`, `reconciliation_incidents`,
  `decision_funnel_session_summaries`.
- **Strategy v2 research**: `strategy_v2_shadow_{config,versions,state,
  decisions,trades}`, `strategy_v2_forward_{registrations,evidence,
  replay_artifacts,evidence_artifacts}`, exit/bracket challenger
  registrations+trades, `live_exit_challenger_*`,
  `strategy_v2_portfolio_{registrations,observations}`.
- **Opening momentum**: `opening_momentum_shadow_runs`,
  `opening_activity_observations`, `opening_momentum_executions`.
- **Universe / quant**: `universe_selection_{runs,candidates}`,
  `watchlist_items`, `watchlist_scores`, `watchlist_quant_v6_{registrations,
  artifacts,publications,publication_artifacts}`.
- **Research ops**: `strategy_experiments`, `strategy_experiment_runs`,
  `experiment_results`, `backtest_runs`, `platform_backtest_runs`,
  `factor_snapshots`, `factor_ic_series`, `prompt_versions`,
  `llm_interactions`, `llm_symbol_schedule_state`.
- **Notifications/ops**: `notifications`, `alert_rules`, `alert_firings`,
  `trade_notes`, `credential_config`, `paper_orders`.

## Flow

### Startup lifespan order (`main.lifespan`)

1. `init_db()` (create_all + `_ensure_*` migrations) → `init_audit_logger()`
   → log CORS allowlist.
2. `runner.start()` via `asyncio.to_thread` — failure raises `RuntimeError`
   and aborts app startup.
3. Optional `PlatformRunner` when `settings.platform_mode`.
4. `_register_cron_health_jobs()` + `_activate_cron_health_jobs()` (best
   effort, never blocks startup).
5. `LivenessWatchdog` start (if enabled; failure stops the runner and
   re-raises).
6. 13 `asyncio.create_task` background crons: liveness heartbeat, WS cleanup,
   LLM analysis, report schedule, alert rules, LLM storage maintenance,
   strategy-v2 shadow, opening-momentum shadow, universe selection, auto
   primary switch, interval recenter, watchlist quant, watchlist-quant-v6
   evaluation. Each cron owns an `asyncio.Lock`, wraps its blocking tick in
   `asyncio.to_thread` (lease-guarded via `DurableJobLeaseService`), and
   yields around the open via `_opening_research_quiet_window()`. On shutdown:
   cancel all tasks, stop watchdog, `runner.stop()` via `to_thread`.
7. Routers: 93 `include_router` calls (`/api/platform`, `/api/portfolio`, then
   the per-domain routers under their own prefixes).

### Quote hot path

Broker WS push → `AppRunner._on_quote` (quote-quality gate: spread ≤ 5%,
BBO deviation, source age ≤ 30s) → `_evaluate_quote_trigger` under
`_state_lock` (snapshots engine, `StrategyEngine.update_price() ->
TriggerResult`) → `_broadcast_status()` (WS) → `_execute_triggered_order` →
`TradeExecutionService.pre_submit_risk_check()` → the single broker mutation.

### Runner loop cycle

See the 5-second `_run_loop` step list under *Design → AppRunner threading
model*; the loop is reconciliation-driven (pendings, today's orders,
positions, engine sync, board lots, exposure) with persistence and funnel
housekeeping at the end of each pass.

## Integration

| Subpackage | Role | Deep dive |
|---|---|---|
| `api/` | FastAPI routers (90+); sync route handlers; one router per domain (`strategy`, `trade`, `platform`, …). | `api/codemap.md` |
| `services/` | Business logic: trade execution, LLM advisor, universe, quant, shadows, review, PnL (129 services). | `services/codemap.md` |
| `core/` | Broker gateway, range engine, risk controller, fees, backtest, audit, market calendar, notifiers (25 modules). | `core/codemap.md` |
| `domain/` | Pure computation, no I/O: prompt plugins, strategy_v2, universe_selection, watchlist_quant_v6, opening momentum (9 subpackages). | `domain/codemap.md` |
| `platform/` | Research/plugin SDK, paper broker, portfolio, 250+ analytics modules, 202-route `api.py`. | `platform/codemap.md` |
| `strategies/` | Platform strategy plugins (12 platform imports). | `strategies/codemap.md` |
| `cli/` | Operational command-line helpers (`python -m app.cli.<module>`). | `cli/codemap.md` |

Layering (AST-verified): app-root → api/services/core/domain/platform;
`services` may back-import `api` (do not extend); `domain` imports `core`
only; `core` imports nothing upward.
