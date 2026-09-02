# `backend/app/services/` — Business Logic Layer

## OVERVIEW
129 flat files, ~72k lines. Largest layer, no subpackages. The load-bearing distinction is **live-order path vs record-only research** — get it wrong and you breach P0.

## LIVE vs RECORD-ONLY (read this before touching anything)

| Class | Services | Note |
|---|---|---|
| **Submits broker orders** | `trade_execution_service.py` **only** | `_submit_limit_order` → `broker.submit_limit_order` is the sole call site in this layer |
| **Submits via runner, config-gated** | `opening_momentum_execution_service.py` | `opening_momentum_execution_enabled` → `runner.execute_opening_momentum_entry`. Named like a shadow, is NOT |
| **Changes the live symbol** | `auto_primary_switch_service.py` | Only path that does; must pass `runner.assert_primary_switch_safe` |
| **Gates live entries (read-only decision)** | `live_entry_policy_service.py`, `interval_application_service.py`, `llm_order_policy.py` | Fail-closed; `ALLOW / REJECT / SHADOW`; P0 pins LLM to SHADOW |
| **Live bookkeeping / risk state** | `trade_event_service`, `order_terminal_callback_service`, `reconciliation_incident_service`, `daily_pnl_service`, `runtime_state_service`, `credentials_service`, `decision_funnel_service` | Affect live risk state, never submit |
| **Record-only research** | everything else | shadows/challengers, `universe_*`, `watchlist_*`, `watchlist_quant_v6_*`, ~50 analytics services |

Shadow/challenger docstrings say so explicitly (`live_exit_challenger_service.py`: "without submitting orders"). Keep that line when editing.

## SERVICE FAMILIES
Live execution core (~13) · gated opening execution (1) · shadow/challenger (6) · universe+watchlist+quant-v6 (14) · LLM (7) · read-only analytics (~50, one per `/api/<metric>` page) · ops/platform (~30) · risk reporting (5).

## CONVENTIONS
- **Construction**: plain class, `__init__(self, db: Session)`, instantiated per request / per tick. No DI container, no singletons. Optional collaborators are keyword-only (`*, candle_provider=..., transaction_fence=..., operation_checkpoint=...`).
- **Exception**: `TradeExecutionService` takes **callables only** (no db, no broker), is built once in `AppRunner.__init__`, and owns `_submission_lock`. Never construct it per request.
- **Infra singletons only**: `api/deps.py` AuditLogger, `get_notification_sink()`, `OrderTerminalCallbackService` (own `SessionLocal` + module `_CLAIM_LOCK`), `DurableJobLeaseService(session_factory=SessionLocal)`.
- **Pure-function modules** (no class) for analytics: `compute_<thing>(...) -> <Thing>Result` returning frozen dataclasses (`trade_stats_service.py`, `equity_curve_service.py`, `event_list_service.py`).
- **Naming**: `<domain>_<role>_service.py` + one `XxxService`. Non-`_service` suffixes are reserved for infra roles: `*_policy.py`, `*_provider.py`, `*_deadline.py`, `*_supervisor.py`, `*_backoff.py`, `*_inspector.py`, `snapshot_helper.py`, `data_aggregator.py`.
- **Errors**: `ValueError` for invalid input/config, `RuntimeError` (or a subclass: `OrderPersistenceError`, `BrokerSubmissionUncertainError`, `QuantV6PublicationError`) for state violations. The API layer translates to `HTTPException`.
- **Loggers**: `logging.getLogger("auto_trade.<module>")` in large services, `__name__` in small ones. Repeated warnings dedupe via class-level `set` + `threading.Lock`.
- **Wiring**: routers do `Depends(get_db)` → inline `Service(db)`. Crons do `await asyncio.to_thread(_x_tick_sync)`, and `_tick_sync` opens `SessionLocal()`, ticks, then `db.rollback()` + `logger.exception` on failure.

## ANTI-PATTERNS (THIS DIR)
- Assuming anything named `*_shadow*`-adjacent is inert — check `opening_momentum_execution_service.py` first.
- Constructing `TradeExecutionService` with a `Session`, or per request.
- Raising `HTTPException` here (zero occurrences today — keep it that way).
- Calling a sync DB service directly inside an async cron — always hop through `asyncio.to_thread`.
- Adding a long-running write loop without `DurableJobLeaseService` + `transaction_fence` as the first DML — SQLite has one writer.
- Downgrading a possibly-live order failure to a warning. Audit/notify failures are swallowed **by design**; data-integrity failures are fail-closed (`DailyPnlService` refuses incomplete replay and preserves live risk state; ambiguous submissions become `UNCERTAIN`, never `FAILED`).
- Extending the `services → api` back-import. Three services import the private `_active_fee_rates` from `app.api.trades`; that helper is shared business logic living in the wrong layer. Do not add a fourth.
