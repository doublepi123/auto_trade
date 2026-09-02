# `frontend/` — Vue 3 SPA + Cypress

## OVERVIEW
~46k lines under `src/`: views 64 files / 31.9k, api 84 / 3.8k, components 18 / 3.8k, composables 20 / 1.7k, `types/index.ts` 3.1k (single file), utils 9 / 631, router 1 / 74. Plus 86 Cypress specs.

## ROUTING
`createWebHashHistory`, **one flat table** in `src/router/index.ts`: 64 lazy-loaded routes + a catch-all redirect to `/`. No nesting, no `meta`, no navigation guards.

13 core operating pages: `/` Dashboard, `/watchlist`, `/review`, `/reports`, `/strategy`, `/history`, `/events`, `/backtest`, `/experiments`, `/credentials`, `/alerts`, `/notifications`, `/lab`. The other 51 are single-purpose read-only analytics views.

**Path ≠ filename** for many routes — `/events`→`DecisionTimeline.vue`, `/history`→`TradeHistory.vue`, `/alerts`→`AlertRules.vue`, `/notifications`→`NotificationCenter.vue`, `/regime`→`RegimePanel.vue`, `/kelly`→`KellySizing.vue`, `/correlation`→`CorrelationMatrix.vue`, `/benchmark`→`BenchmarkAlphaBeta.vue`. Always resolve a page through the router file. (`api/strategy.ts` and `views/Strategy.vue` are unrelated despite the name.)

## SHARED STATE — MODULE-LEVEL `ref()` SINGLETONS
Declare `ref`s at **module top level**, not inside the composable function; every caller then shares one instance. No Pinia, no prop drilling.

- `useConnectionHealth.ts` owns the app's realtime connection (WS + polling fallback, `connecting/connected/reconnecting/polling`), survives navigation, and validates WS payloads through `utils/validator.ts`. `useDashboardData` delegates status to it rather than duplicating.
- `useSymbolStore.ts` is one-shot: writers set `requestedSymbol`, the consumer calls `consumeRequestedSymbol()` which reads and clears.
- localStorage persistence lives in composables (9 of them). `usePersistedColumns` merges stored overrides onto defaults so newly added columns stay visible until explicitly hidden.

## API CLIENTS
`src/api/client.ts` is three lines: `axios.create({ baseURL: '', timeout: 10000 })`. No interceptors, no error normalization, no auth — the API key is injected server-side by the Vite proxy in dev and by nginx in Docker.

One file per backend domain exporting `async function getX(): Promise<Typed>` that returns `resp.data`; query params stay snake_case; CSV export uses `responseType: 'blob'`. Filenames are camelCase except four that mirror backend module names: `llm_advisor.ts`, `strategy_shadow.ts`, `opening_momentum_shadow.ts`, `strategy_experiments.ts`. `index.ts` re-exports everything, so both `from '../api'` and `from '../api/edgeQuality'` work.

`views/Watchlist.vue` imports `axios` directly — a lone exception, not a pattern to copy.

## COMPONENTS & CHARTS
No chart library: charts are hand-written SVG (`PriceChart`, `PnLChart`, `EquityCurvePanel`, `RiskHistoryPanel`, `BacktestChart`). Most analytics views are tables and metric cards instead.

Shared contracts: `DataState.vue` renders error → loading → empty → default slot in that order and exposes `data-testid="data-state-error|loading|empty"` (Cypress depends on these); `MetricStat.vue` for metric cards; `StatisticsQualityAlert.vue` to surface `statistics_quality` evidence on research pages.

`utils/labels.ts` is the single source for Chinese enum copy (`engineStateLabel`, `orderStatusLabel`, `skipCategoryLabel`, …). Only 8 files consume it today — new code must route copy through it rather than hardcoding strings in components.

## CYPRESS
`baseUrl` = `CYPRESS_BASE_URL` or `http://localhost:8080` (the Docker stack), viewport 1280×720, `allowCypressEnv: false`, specs at `cypress/e2e/**/*.cy.ts`.

All stubbing is centralized in `cypress/support/e2e.ts` (~5.3k lines) as inline TS objects, **not** fixtures. Three commands: `cy.stubApi()` registers 100+ `cy.intercept`s with `.as()` aliases (some stateful, e.g. a mutable status object), `cy.visitApp(path)`, and `cy.setupApp()` (now a no-op). Later intercepts win — a specific route like `/api/notifications/stats*` must be registered **after** the broader `/api/notifications?*`.

## COMMANDS
```bash
npm run dev          # :3000, proxies /api + /ws to :8000
npm run type-check   # vue-tsc --noEmit
npm run build        # vue-tsc && vite build (type check is part of the build)
npm run cypress:run
```
`tsconfig.json`: `strict: true`, alias `@/* → ./src/*`, `moduleResolution: bundler`.

## ANTI-PATTERNS (THIS DIR)
- Adding Pinia, or declaring shared `ref`s inside a composable function body (breaks the singleton).
- New `axios.create` or direct `axios` imports — use `src/api/client.ts`.
- Hardcoded Chinese enum copy in components instead of `utils/labels.ts`.
- Pulling in a chart library.
- Cypress fixtures for new stubs, or registering a specific intercept before a broader one.
- Removing `data-testid` attributes from `DataState.vue`.
- Manual chunk splitting for element-plus in `vite.config.ts` — circular imports mean Rollup must own those boundaries (`npm run build:check-element-plus` guards this).
- Growing `Watchlist.vue` (4799 lines) or `Lab.vue` (4677) further — extract a component or composable instead.
