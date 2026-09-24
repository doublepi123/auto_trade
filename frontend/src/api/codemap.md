# frontend/src/api/

## Responsibility

Typed axios client layer: one file per backend domain, each exporting `async function getX(): Promise<Typed>` wrappers that return `resp.data`. 85 `.ts` files (~3.8k lines) covering every backend route family — live trading ops, research/shadow, review/reporting, and ~40 single-endpoint analytics endpoints. Views and composables never call `fetch` or raw `axios` (one exception: `views/Watchlist.vue` imports `axios` directly — legacy, do not copy).

## Design

- **Shared instance** — `client.ts` is three lines: `export const api = axios.create({ baseURL: '', timeout: 10000 })`. `baseURL: ''` means paths like `/api/...` are relative, so dev (Vite proxy on :3000) and Docker (nginx on :8080) both work unchanged.
- **No interceptors, no error normalization, no auth headers.** The `X-API-Key` is injected server-side: by the Vite dev proxy for `/api` and `/ws`, and by nginx in the Docker deployment. The browser never sees the key.
- **Response types come from `../types`** (`types/index.ts`, the 3.1k-line shared interface file); response-shape interfaces specific to one domain (e.g. `WatchlistScore`) are declared locally in the client file.
- **Query params stay snake_case** (`lookback_days`, `page_size`) matching the backend Pydantic aliases — no camelCase conversion anywhere.
- **CSV export** uses `responseType: 'blob'`: `backtest.ts`, `notifications.ts` (export), `trades.ts` (export).
- **Naming**: camelCase filenames except four that mirror backend module names — `llm_advisor.ts`, `strategy_shadow.ts`, `opening_momentum_shadow.ts`, `strategy_experiments.ts`.
- **Barrel**: `index.ts` re-exports every module, so both `from '../api'` and `from '../api/edgeQuality'` resolve.
- **Path ≠ filename for a few multi-domain clients** (table below): `trade.ts` actually serves `/api/orders` + `/api/control/*` + `/api/account`; `api/strategy.ts` (live strategy CRUD + status/diagnostics) is unrelated to `views/Strategy.vue`'s analytics cousins.

## Flow

`view/composable → exported function → api.get/post('/api/...') → Vite proxy / nginx → FastAPI router`. The function awaits the response, applies any client-side normalization (e.g. `trade.ts getOrders()` coerces a bare array into an `OrderPage`), and returns `resp.data` typed. Errors propagate as raw axios errors; views catch them and feed `DataState.vue` / ElMessage — there is no centralized error mapper.

## Integration

- Backend counterpart: `backend/app/api/` routers (93 `include_router` calls) plus `platform/api.py` (`/api/platform/*` family — not all surfaced here).
- Upstream consumers: `views/*.vue` (direct imports), `composables/*` (`useConnectionHealth` → `getStatus`, `useDashboardData` → `getStatus`/`getStrategy`, `useNotificationStream` → `/api/events`, `useMarketSession`, `useAccountRefresh`, etc.).
- `cypress/support/e2e.ts` stubs these same `/api/*` paths via `cy.intercept` — adding an endpoint means adding a stub there.

### Client → backend prefix

| Client file | Backend prefix | Notes |
|---|---|---|
| `client.ts` | — | shared `api` axios instance |
| `strategy.ts` | `/api/strategy`, `/api/status`, `/api/status/history`, `/api/diagnostics` | live strategy config CRUD + runner status |
| `trade.ts` | `/api/orders`, `/api/control/*`, `/api/account`, `/api/metrics/summary` | order pages, pause/resume/kill-switch, protective exit |
| `trades.ts` | `/api/trades*` | closed-trade list/stats/export + `/api/trades/analytics/*` (calendar, monthly, weekday, pnl-distribution, hold-duration) |
| `positions.ts`, `equity.ts`, `pnl.ts` | `/api/positions/pnl`, `/api/equity/curve`, `/api/pnl/by-symbol` | position & PnL views |
| `risk.ts`, `riskTimeline.ts`, `riskScore.ts` | `/api/risk/history`, `/api/risk-timeline/*`, `/api/risk-score` | risk history & derived analytics |
| `broker.ts`, `calendar.ts`, `events.ts`, `tradeNotes.ts`, `reconciliation.ts` | `/api/broker/candles`, `/api/calendar/session`, `/api/events`, `/api/trade-notes`, `/api/reconciliation/*` | candles, session clock, event feed, notes, reconcile status + force-resume |
| `credentials.ts`, `alertRules.ts`, `notifications.ts` | `/api/credentials*`, `/api/alert-rules*`, `/api/notifications*` | config pages; notifications has blob export + stats |
| `cronHealth.ts`, `databaseHealth.ts`, `quoteHealth.ts` | `/api/cron-health`, `/api/database-health`, `/api/quote-health` | ops health panels |
| `watchlist.ts`, `universe.ts`, `universeExplainer.ts`, `primaryCandidacy.ts` | `/api/watchlist*`, `/api/universe/*`, `/api/universe-explainer/run`, `/api/universe/primary-candidacy` | watchlist CRUD/scores/snapshots; universe catalog/refresh/scorecard |
| `strategy_shadow.ts` | `/api/strategy-shadow/*` (14 endpoints) | v2 shadow config/status/decisions/challengers/validation |
| `opening_momentum_shadow.ts` | `/api/opening-momentum-shadow/*` | shadow runs + execution status |
| `strategy_experiments.ts`, `lab.ts` | `/api/strategy-experiments*`, `/api/experiments*` + `/api/indicators`, `/api/performance/*`, `/api/llm-usage/summary` | Experiments page + Lab workbench |
| `llm_advisor.ts`, `llmInteractions.ts` | `/api/strategy/llm-interval/*`, `/api/llm-interactions/*` | interval advisor enable/disable/preview/analyze |
| `backtest.ts`, `review.ts`, `reports.ts` | `/api/backtest*`, `/api/review*`, `/api/reports/*` (daily/weekly/monthly/range/schedule) | blob exports |
| `strategyHealth.ts`, `strategyPresets.ts`, `regime.ts`, `signalConsensus.ts`, `interventionEvidence.ts`, `attribution.ts`, `executionQuality.ts`, `decisionReplay.ts`, `drawdownAnalysis.ts`, `benchmark.ts`, `kelly.ts`, `correlation.ts`, `monteCarlo.ts`, `lookaheadAnalysis.ts`, `recovery.ts`, `streaks.ts` | matching kebab-case prefix | research/evidence pages |
| ~40 analytics files (`asymmetry.ts`, `autocorrelation.ts`, `capitalEfficiency.ts`, `concentration.ts`, `dailyConsistency.ts`, `decayDetection.ts`, `distributionShape.ts`, `drawdownDuration.ts`, `edgeQuality.ts`, `exitEfficiency.ts`, `feeDrag.ts`, `firstTrade.ts`, `holdingTime.ts`, `intradaySeasonality.ts`, `lossContainment.ts`, `milestones.ts`, `momentumRanking.ts`, `predictionScore.ts`, `profitConcentration.ts`, `profitFactor.ts`, `reentryAnalysis.ts`, `regimeSensitivity.ts`, `returnCalendar.ts`, `rMultiples.ts`, `robustness.ts`, `rollingMetrics.ts`, `rollingVar.ts`, `scratchAnalysis.ts`, `sizeImpact.ts`, `skipAnalytics.ts`, `tagAnalytics.ts`, `timePerformance.ts`, `tradeFrequency.ts`, …) | 1:1 kebab-case (`edgeQuality.ts` → `/api/edge-quality`) | one read-only research view each; add a row only when the mapping is not 1:1 |
