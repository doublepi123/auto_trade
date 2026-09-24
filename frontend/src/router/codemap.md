# frontend/src/router/

## Responsibility

The complete navigation surface of the SPA in one file (`index.ts`, ~75 lines):
65 lazy-loaded routes plus a catch-all redirect to `/`.

## Design

- **One flat table**: a single `routes: RouteRecordRaw[]` array — no nested
  routes, no route `meta`, no navigation guards, no named routes. Anything
  page-specific (titles, active state) is handled by the view or `App.vue`.
- **Hash history**: `createWebHashHistory()` — URLs like `/#/watchlist`. This
  makes the nginx SPA config trivial (`try_files … /index.html`) and avoids
  server-side path rewriting entirely.
- **Lazy imports**: every route component is `() => import('../views/X.vue')`,
  so each page is its own chunk (this is what the chunk-budget guard in
  `scripts/` watches).
- **Catch-all**: `{ path: '/:pathMatch(.*)*', redirect: '/' }` sends unknown
  paths to the Dashboard.
- **Two route groups** (visible in table order):
  1. **Core operating pages** (first ~16 entries): `/` Dashboard, `/backtest`,
     `/experiments`, `/strategy`, `/credentials`, `/history`, `/events`,
     `/review`, `/reports`, `/watchlist`, `/alerts`, `/notifications`, `/lab`,
     plus newer operator tools (`/signal-consensus`, `/universe-explainer`,
     `/risk-timeline`, `/platform-catalog`, …).
  2. **Read-only analytics** (remaining ~49): one path per research view —
     `/drawdown`, `/kelly`, `/correlation`, `/benchmark`, `/edge-quality`,
     `/regime-sensitivity`, `/primary-candidacy`, … Each renders exactly one
     research dataset and follows the same table/metric-card template.
- **Path ≠ filename**: many paths do not match their component file. Notable
  renames: `/events` → `DecisionTimeline.vue`, `/history` → `TradeHistory.vue`,
  `/alerts` → `AlertRules.vue`, `/notifications` → `NotificationCenter.vue`,
  `/regime` → `RegimePanel.vue`, `/kelly` → `KellySizing.vue`,
  `/correlation` → `CorrelationMatrix.vue`, `/benchmark` →
  `BenchmarkAlphaBeta.vue`. **Always resolve a page through this file**, not by
  guessing a filename.

## Flow

`main.ts` does `app.use(router)`; `App.vue` renders `<router-view>` inside the
shell. Navigation happens via `router-link`s in `App.vue`, programmatic
`router.push` (e.g. the command palette), or the browser address bar; the
catch-all folds typos back to `/`.

## Integration

- Imports every `views/*.vue` lazily — adding a page means adding one line here
  (and a nav link in `App.vue` if it should be reachable).
- `useRecentPages` records `route.path` for the command palette; no other
  module reads the route table.
