# frontend/src/types/

## Responsibility

The single shared TypeScript contract file for the entire frontend:
`index.ts` (~3.2k lines), holding 230 `export interface` and ~32 `export type`
declarations that mirror the backend's Pydantic response schemas. Every api
client and every view imports from here (`../types` or `@/types`).

## Design

One file, no submodules — interfaces are grouped by backend domain in
declaration order. The major domains, top to bottom:

| Domain | Representative exports |
|---|---|
| Core status / diagnostics | `StatusData`, `StatusHistory`, `StatusHistoryPoint`, `QuoteQuality`, `DiagnosticSymbolRuntime`, `DiagnosticLiveSafety`, `DecisionFunnelDiagnostics`, `DiagnosticsResponse` |
| Orders / fills / account | `OrderRecord`, `OrderPage`, `OrderCancelResult`, `Position`, `AccountInfo`, `CashBalance`, `MarginInfo` |
| Trade events & notes | `TradeEventRecord`, `TradeEventPage`, `TradeNote`, `TradeNotePage`, `TimelineSource` |
| LLM advisor | `LLMSuggestion`, `LLMIntervalStatus`, `LLMAnalyzeResponse`, `LLMInteractionRecord`, `LLMUsageSummary` |
| Backtest & walk-forward | `BacktestParams`, `BacktestResult`, `BacktestMetrics`, `BacktestSweepHeatmap`, `WalkForwardWindow/Result/Request`, `StressTestResult`, `BacktestRunPage` |
| Closed-trade analytics | `ClosedTrade`, `TradeStats`, `TradeCalendarResponse`, `TradeMonthlySummaryResponse`, `EquityCurveResponse`, `SymbolAttributionResponse` |
| Alerts / notifications | `AlertRule(Page)`, `AlertFiring`, `AlertRuleEffectiveness`, `NotificationLogPage`, `NotificationStatsResponse` |
| Ops health | `DatabaseHealthSnapshot`, `CronHealthSnapshot`, `QuoteStreamHealth`, `InterventionEvidence*`, `RiskHistory*`, `MarketSessionStatus` |
| Review | `ReviewOrder`, `ReviewEvent`, `ReviewLLMInteraction` |
| Universe selection | `UniverseSelectionItem`, `UniverseSelectionRunResponse`, `UniversePromotionReadiness*` |
| Experiments | `ExperimentSummary`, `PerformanceVariant`, `StrategyExperiment*`, `LLMEvaluation*` |
| Opening momentum shadow | `OpeningMomentumShadowConfig/Run/Metrics/Status`, `OpeningMomentumExecution*` |
| Strategy v2 shadow | `StrategyShadowConfig/Latest/Metrics/Status/Decision/Version`, challenger & portfolio-routing variants (`…ExitChallenger…`, `…BracketChallenger…`, `…AdxChallenger…`, `…Warmup…`, `…ForwardValidation…`, `LiveExitChallenger…`) |
| Reconciliation | `ReconciliationStatus`, `ReconciliationEvidence`, `ReconciliationBrokerSnapshot`, `ReconciliationEvidenceSurface` |
| Primary candidacy | `PrimaryCandidacyGateParameters/PoolGate/Power/Candidate/Response`, verdict unions (`PrimaryCandidacyVerdict`, `…PoolGateStatus = 'PASS' | 'BLOCKED' | 'UNASSESSABLE'`) |

Naming conventions: response wrappers end in `Response`/`Page`; paginated pages
pair with `…Row`/`…Bucket`/`…Point` item types; union states are exported `type`
aliases (`'healthy' | 'stale' | …`). Query param shapes keep snake_case field
names to match the backend.

## Flow

Backend Pydantic schema → (hand-mirrored here) → `api/*.ts` function return
types → view/component props and template bindings. Changes flow in one
direction only: when the backend schema changes, update this file in the same
commit as the api client.

## Integration

Consumed by essentially every module under `src/` — `api/` for return types,
`composables/useConnectionHealth` + `utils/validator.ts` for runtime validation
of these shapes (validator imports the interfaces it checks), and all 65 views.
No runtime code lives here; it is types only.
