# backend/app/services/

134 flat `.py` files (~72k lines), no subpackages — the largest and busiest layer of the backend.

## Responsibility

Classic **Service Layer** between the API routers (`app/api/`) and the domain/core layers: it owns use-case orchestration — trade execution, order/fill persistence and reconciliation, PnL accounting, LLM advisory, universe/watchlist research pipelines, and ~50 read-only analytics — while pure computation stays in `app/domain/` and broker/risk/engine primitives stay in `app/core/`. Services are transaction-script style classes over a SQLAlchemy `Session`; they translate `ValueError`/`RuntimeError` (and subclasses such as `OrderPersistenceError`, `BrokerSubmissionUncertainError`, `FillSettlementConflict`, `QuantV6PublicationError`) into results that the API layer maps to `HTTPException`. No service raises `HTTPException` itself.

The load-bearing safety distinction (P0): **`trade_execution_service.py` is the only file in this layer that submits broker orders**, one config-gated exception (`opening_momentum_execution_service.py`) routes through the runner, and everything else is record-only.

## Design

### Construction patterns

- **Standard service**: plain class, `__init__(self, db: Session)`, built per request / per tick. No DI container. Optional collaborators are keyword-only (`*, candle_provider=..., transaction_fence=..., operation_checkpoint=...`).
- **Callable-injected exception**: `TradeExecutionService` (4706 lines) takes **callables only** (no db, no broker), is constructed exactly once in `AppRunner.__init__` (runner.py:319), and owns an `RLock` `_submission_lock` exposed as `submission_guard()`. Every mutation path (`execute`, `reconcile`, `cancel_*`, `_submit_limit_order`) runs under that guard.
- **Infra singletons** (module-level, own their `SessionLocal`): `OrderTerminalCallbackService` (+ module `_CLAIM_LOCK`), `get_notification_sink()` / `NotificationLogSink`, `DurableJobLeaseService(session_factory=SessionLocal)`; `AuditLogger` lives in `api/deps.py`.
- **Pure-function modules** (no class) for analytics: `compute_trade_stats()` (`trade_stats_service.py`), `compute_equity_curve()` (`equity_curve_service.py`), `list_timeline_events()` (`event_list_service.py`) — frozen dataclass results over `ClosedRoundTrip` inputs.
- **Protocols over mocks**: `OpeningExecutionRunner`, `CandleProvider`, `QuoteProvider`, `WatchlistMarketDataProvider`, `HistoricalPreviewReader`, `HistoricalHttpTransport` — collaborators are injected as Protocols.

### File groups

**Live order path (broker mutation reachable)**

| File | Role |
|---|---|
| `trade_execution_service.py` | Whole live order path; `pre_submit_risk_check()` boundary returns frozen `ApprovedOrder`; `_submit_limit_order` is the layer's **sole** `broker.submit_limit_order` call site, wrapped in `risk.protective_submission_guard()` |
| `opening_momentum_execution_service.py` | Config-gated (`opening_momentum_execution_enabled`) opening entry; submits indirectly via injected `OpeningExecutionRunner` → `runner.execute_opening_momentum_entry`. Named like a shadow — **it is not one** |
| `auto_primary_switch_service.py` | The only path that changes the **live symbol**; must pass `runner.assert_primary_switch_safe`; ADX/reach-rate/signal-edge gates |

**Entry gating (read-only decisions, fail-closed)**

| File | Role |
|---|---|
| `live_entry_policy_service.py` | Entry eligibility (`ALLOW/REJECT/SHADOW`) incl. HK board-lot residual inhibition — blocks entries via policy, never via `risk.pause()` |
| `llm_order_policy.py` | LLM order disposition; P0 pins LLM to `SHADOW` — LLM never places live orders |
| `interval_application_service.py` | Applies accepted LLM interval suggestions to strategy config |

**Reconciliation & ledger (live bookkeeping, never submits)**

| File | Role |
|---|---|
| `trade_event_service.py` | `record_trade_event()` — sole writer of `trade_events` (Decision Timeline source) |
| `fill_settlement_service.py` | `FillSettlementLedger.record_or_get(SettlementIntent)` — idempotent accounting receipts; repeat fills matched via `domain.fill_settlement.compare_repeat`, conflicts raise `FillSettlementConflict` |
| `order_terminal_callback_service.py` | `claim/complete/release` receipts in `order_terminal_callbacks` — dedupes broker terminal callbacks |
| `daily_pnl_service.py` | FIFO round-trip pairing, PnL replay, `reconcile_risk_state()`; **refuses incomplete replay** (fail-closed, preserves live risk state) |
| `position_pnl_service.py` | Open-position mark-to-market via injected `QuoteProvider` |
| `reconciliation_incident_service.py` | Deduped reconcile incidents (`record_failure`/`record_recovery`, `POSITION_RECONCILIATION_UNCERTAIN` etc.) |
| `reconciliation_backoff.py` | Capped alert backoff for repeated reconcile failures |
| `runtime_state_service.py` | Engine/risk state load/persist/stage/plan across runner restarts |
| `recovery_service.py` | Post-incident recovery analysis |
| `historical_ledger_import_service.py` | Import historical broker order ledger (plan/apply, conflict + replay errors) |
| `historical_order_completeness_reader.py` | Longport historical-fill completeness proof |
| `credentials_service.py` | AES-GCM + RSA hybrid encryption; plaintext only via `get_plain_credentials()` |
| `decision_funnel_service.py` | `DecisionFunnelTracker` 9-stage counters (quote→…→persisted) → `GET /api/diagnostics` |
| `data_aggregator.py` | Candle fetching + ATR/Bollinger computation |
| `snapshot_helper.py` | `open_read_snapshot()` safe read-only DB engine pool |

**PnL / risk reporting & health**

`risk_history_service.py`, `risk_score_service.py`, `risk_timeline_service.py`, `loss_containment_service.py`, `milestone_service.py`, `strategy_health_service.py`, `research_observation_health_service.py` (3251 lines), `quote_stream_health_service.py` (`QuoteStreamHealthTracker` used by runner), `cron_health_service.py`, `database_health_service.py` — all record-only.

**LLM family (7)**

`llm_advisor_service.py` (provider calls, `analyze`/`preview`), `llm_interaction_service.py` (interaction log, pruning, context compaction), `llm_order_policy.py`, `llm_recommendation_evaluator.py`, `llm_symbol_state_service.py`, `llm_usage_service.py`, plus `interval_application_service.py`. Prompt assembly itself lives in `domain/prompt/`.

**Universe & watchlist & quant-v6 (14)**

`universe_selection_service.py` (3132 lines, `refresh()` with run claims), `universe_promotion_service.py`, `universe_run_history_service.py`, `universe_explainer_service.py`, `watchlist_service.py`, `watchlist_score_service.py`, `watchlist_quant_service.py` (walk-forward scoring), `watchlist_quant_v6_{evaluation,publication,reader,historical_provider,spawn_supervisor,deadline}.py` (quote-only historical evaluation pipeline with subprocess workers, memory/db fences, publication receipts), `momentum_ranking_service.py`, `primary_candidacy_service.py` (read-only candidacy report; shares the switch's gates via `classify_candidate_row`, never calls the runner).

**Shadows & challengers (record-only, never order)**

`strategy_v2_shadow_service.py` (7390 lines; `tick()` / `replay()`), `strategy_v2_bracket_challenger_service.py`, `strategy_v2_exit_challenger_service.py`, `strategy_v2_portfolio_service.py`, `live_exit_challenger_service.py` ("without submitting orders" — keep that docstring line), `opening_momentum_shadow_service.py` (5403 lines), `trusted_frozen_assessment_service.py`, `signal_edge_service.py`, `backtest_run_service.py`, `intervention_evidence_service.py`.

**Review & reports & strategy config**

`review_service.py`, `report_service.py` (daily/weekly/monthly/range), `report_schedule_service.py` (`maybe_send(runner)` cron), `strategy_service.py` (config CRUD + primary runtime state), `strategy_version_service.py`, `strategy_preset_service.py`, `trade_note_service.py`, `first_trade_service.py`, `strategy_experiment_service.py`, `experiment_grid_service.py`, `decision_replay_service.py`.

**Notifications & alerts**

`alert_rule_service.py` (`evaluate(runner)` cron), `notification_log_service.py` (`NotificationLogSink` + `get_notification_sink()` singleton).

**Housekeeping / ops**

`durable_job_lease_service.py` + `durable_job_lease_inspector.py` (SQLite single-writer leases for long jobs), `research_artifact_retention_service.py` (artifact byte pruning; provenance rows kept forever), `audit_log_service.py`, `platform_catalog_service.py`.

**Read-only analytics (~50)**

One service per `/api/<metric>` page: `asymmetry`, `autocorrelation`, `benchmark`, `calendar_coverage`, `capital_efficiency`, `concentration`, `correlation`, `daily_consistency`, `decay_detection`, `distribution_shape`, `drawdown_analysis`, `drawdown_duration`, `edge_quality`, `entry_window_overlap`, `equity_curve`, `execution_quality`, `exit_efficiency`, `fee_drag`, `hard_bound_projection`, `holding_time`, `intraday_seasonality`, `interval_recenter`, `interval_width_fitness`, `kelly`, `lookahead_analysis`, `monte_carlo`, `performance_attribution`, `prediction_score`, `profit_concentration`, `profit_factor`, `r_multiples`, `range_fitness`, `reentry_analysis`, `regime_sensitivity`, `regime_service`, `robustness`, `rolling_metrics`, `rolling_var`, `rotation_forward_scorecard`, `scratch_analysis`, `signal_consensus`, `size_impact`, `skip_analytics`, `statistics_quality`, `streak`, `symbol_attribution`, `tag_analytics`, `time_performance`, `trade_analytics`, `trade_frequency`, `trade_stats`, `event_list`, `analytics_trade_sample` — all `_service.py`, all record-only.

## Flow

**Live entry (quote hot path).** Broker WS push → `runner._on_quote` → `_evaluate_quote_trigger` (under `_state_lock`; snapshots engine, `StrategyEngine.update_price() -> TriggerResult`) → `_execute_triggered_order` → `TradeExecutionService.execute(...)` → `pre_submit_risk_check()` returns a frozen `ApprovedOrder` (side derived from action; approved price = `max(request, fresh executable)`) or a rejection → `_submit_limit_order` submits **that** price, persists, and handles immediate live/terminal/filled outcomes under `submission_guard()` + `risk.protective_submission_guard()`. HK quantities are board-lot-normalized after sizing and *before* the pre-submit boundary using an injected resolver; the sizing path never touches the network.

**Fill settlement.** Broker terminal event → `OrderTerminalCallbackService.claim()` (dedupe receipt) → `SettlementIntent` built with attribution decided *before* any durable write → `FillSettlementLedger.record_or_get()` inside the caller's accounting transaction → `tracked_entries` update → runner's `post-fill-persist` daemon thread runs `DailyPnlService.calculate()`; incomplete replay pauses trading with a post-fill reason and preserves live risk state (fail-closed); unattributable fills become `UNCERTAIN`, never `FAILED`.

**Pending reconciliation.** Runner 5s loop → `TradeExecutionService.reconcile()` (under submission guard) → on uncertain outcome: `ReconciliationIncidentService.record_failure()` + `ReconciliationBackoff` capped alerts → `POSITION_RECONCILIATION_UNCERTAIN` pause → `record_recovery()` on verified recovery. Today-order sync writes the `orders` table only; cost basis comes solely from `fill_settlement_service` receipts.

**LLM interval.** Cron → `LLMAdvisorService.analyze()` (prompt plugins from `domain/prompt/`) → `evaluate_llm_order_policy()` (SHADOW-pinned) → human/config-gated `IntervalApplicationService.apply_suggestion()`; outcome recorded by `LLMInteractionService`.

**Research (record-only).** Crons / API → `StrategyV2ShadowService.tick()` / `replay()`, `OpeningMomentumShadowService`, challengers, `UniverseSelectionService.refresh()`, quant-v6 spawn→evaluate→publish pipeline — none can reach a broker mutation or auto-promote.

## Integration

**Consumers**

- `app/api/*` — 86 routers import services; wiring is `Depends(get_db)` → inline `Service(db)`.
- `app/runner.py` — 62 service imports; constructs `TradeExecutionService` once, owns the quote hot path, post-fill persist thread, and shadow job orchestration.
- `app/main.py` lifespan/crons — `StrategyService`, `LLMSymbolStateService`, `LLMAdvisorService`, `IntervalApplicationService`, `LLMInteractionService`, `ReportScheduleService.maybe_send(runner)`, `AlertRuleService.evaluate(runner)`, `DurableJobLeaseService`, `StrategyV2ShadowService`, `ResearchArtifactRetentionService`, `RiskHistoryService`; async crons hop to sync services via `asyncio.to_thread(_tick_sync)`.

**Dependencies (AST-verified layer graph)**

- `services → domain(56) core(40) platform(2) api(3 back-import)`; downward only otherwise.
- `domain` imports stay pure (strategy_v2, fill_settlement, prompt, universe_selection, watchlist_quant_v6); `core` provides `BrokerGateway`, `RiskController`, `StrategyEngine`, market calendar, fees.
- Only two `platform` imports: `opening_momentum_shadow_service.py` ← `app.platform.multiple_testing`, `strategy_v2_shadow_service.py` ← `app.platform.strategy_quality`.

**Back-import — do NOT extend.** Four services import the private `_active_fee_rates` from `app.api.trades`: `analytics_trade_sample_service.py`, `decision_replay_service.py`, `drawdown_analysis_service.py`, `strategy_health_service.py` (shared business logic living in the wrong layer; do not add another). Separately, `auto_primary_switch_service.py` does a function-local `from app.api.deps import init_audit_logger` (runner.py:598 region) — same rule applies.
