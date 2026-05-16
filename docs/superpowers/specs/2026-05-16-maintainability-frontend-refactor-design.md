# Maintainability and Frontend Experience Refactor Design

## Goal

Refactor the project around clearer module boundaries and a better frontend operating experience. The work prioritizes long-term maintainability and UI quality over preserving existing internal APIs, local SQLite data, or current page structure.

## Decisions

- Refactor direction: module boundary first.
- Primary focus: code structure maintainability and frontend experience.
- Compatibility: breaking full-stack changes are allowed.
- Data migration: old local SQLite data does not need to be preserved.
- Verification target: backend unit tests, frontend build, Cypress flows, and Docker Compose local full-stack startup.
- Trading semantics: do not expand the real trading feature set in this refactor unless needed to fix objective bugs discovered during implementation.

## Current Problems

The backend is functionally covered by tests, but `backend/app/runner.py` mixes lifecycle management, quote callbacks, trade execution, order persistence, risk notifications, runtime state persistence, and credential reload behavior. This makes local reasoning difficult and increases the risk of regressions when changing one concern.

The frontend builds successfully, but page components hold too many responsibilities. `Dashboard.vue` manages initial loading, WebSocket connection state, polling fallback, account refresh, control actions, and rendering. API calls are centralized in one file, which is simple now but becomes noisy as domains grow.

The existing review history also shows repeated fixes around runner lifecycle, WebSocket cleanup, credential reload, and page loading states. Those fixes reduced acute bugs, but the remaining structure still encourages future coupling.

## Backend Architecture

### Runner Orchestration

`AppRunner` should become the lifecycle coordinator. It owns start, stop, reload hooks, thread management, quote subscription, and the quote callback entry point. It should not contain low-level order execution logic or direct persistence details beyond delegating to focused services.

Responsibilities kept in `AppRunner`:

- Build and hold long-lived collaborators.
- Start and stop the background persistence loop.
- Subscribe and resubscribe quotes when strategy or credentials change.
- Route quote events to the strategy engine, risk controller, trade execution service, and broadcast service.
- Restore engine state when an execution attempt fails.

### Trade Execution Service

Create a focused service for order-producing actions. It accepts the current strategy action, quote, broker, risk controller, and persistence hooks. It calculates quantities, validates prices and positions, submits the order, records the order, sends notifications, and updates realized risk PnL for closing actions.

This service should make action handling explicit for:

- `BUY`
- `SELL`
- `SELL_SHORT`
- `BUY_TO_COVER`

It should preserve existing behavior unless tests identify a real bug. Known non-goals include order status polling, broker webhook integration, and true fill-price PnL calculation.

### Runtime State Service

Create a service that owns loading and persisting strategy runtime snapshots. It should convert database state into engine and risk state and persist current snapshots back to the database.

This separates runtime persistence from the runner loop and makes it easier to test state restore and persistence without starting threads.

### Credential and Strategy Reload Boundaries

Credential reload should remain a runner-level operation because it replaces broker and notifier dependencies. Strategy reload should update engine parameters and risk configuration, then resubscribe quotes only when the symbol changes.

The implementation should keep the failure boundary clear: a failed credential resubscribe should not silently swap in an unusable broker, and a failed strategy resubscribe should leave the runner in a known state.

### API Layer

API modules should stay thin. They validate request payloads, call a service or runner method, and return response schemas. They should avoid embedding persistence logic, credential logic, or trade behavior directly in route handlers.

## Frontend Architecture

### API Modules

Split the single `frontend/src/api/index.ts` into domain-focused modules while preserving a shared axios client:

- `client.ts`: axios instance, API key request injection, 401 handling event.
- `strategy.ts`: strategy config and status calls.
- `credentials.ts`: credential calls.
- `trade.ts`: orders, account, and control actions.
- `index.ts`: optional re-export for existing imports during transition.

This makes API behavior easier to scan and gives Cypress and page code clearer domain names.

### Composables

Move page state machinery into small composables:

- Dashboard data loading and refresh.
- Status stream with WebSocket plus polling fallback.
- Account refresh.
- Form save state for dirty, saving, saved, and error indicators.

These composables should stay local to the app and not introduce a global state library. Pinia or another store is out of scope unless implementation proves the composable approach cannot stay simple.

### Dashboard Experience

Rework Dashboard into clearer sections:

- Connection and load status.
- Strategy status and latest price.
- Account summary and cash balances.
- Risk status.
- Trading controls.
- Positions.

The page should distinguish unavailable data from valid zero values. Failed account loading should show an explicit unavailable or retry state instead of silently rendering `$0.00` as if it were real data.

The WebSocket authentication timing should remain compatible with the backend behavior, but the UI should expose connection state so users know whether live updates or polling fallback is active.

### Strategy and Credentials Forms

Strategy and credentials pages should share consistent form behavior:

- Loading disables saves.
- Edits clear the saved indicator.
- Save success is explicit.
- Save failures show actionable messages.
- Credentials clearly explain whether blank input means preserve existing value or clear it.

If clearing credentials is supported, it should be explicit in the UI rather than ambiguous through blank fields.

### Navigation and Layout

The current Element Plus shell can remain, but the layout should be more responsive. Desktop can use broader cards and tables; mobile should avoid cramped multi-column grids.

## Database and Migration Approach

Old local SQLite data does not need to be preserved. The refactor may reset development data and update models without a migration framework. Documentation should state that local data can be removed by deleting the SQLite file under the configured data directory.

The work should not introduce Alembic unless a later requirement needs persistent production upgrades.

## Testing Strategy

Backend tests should be updated or added for:

- Trade execution service action routing, quantity checks, and failure behavior.
- Runtime state load and persist behavior.
- Runner lifecycle delegation and reload behavior.
- API routes after route/service boundary cleanup.

Frontend verification should include:

- TypeScript build with `npm run build`.
- Cypress coverage for dashboard, strategy, credentials, controls, history, and navigation flows.
- Updated selectors or assertions when page structure changes.

Deployment verification should include:

- Docker Compose build and startup.
- Backend health endpoint check.
- Frontend accessibility through the exposed frontend port.
- Confirmation that missing real broker credentials fail safely and do not crash the web app.

## Non-Goals

- Preserve old SQLite data.
- Implement broker order status polling or webhook handling.
- Change the trading strategy model beyond structure needed for maintainability.
- Add a global frontend state library by default.
- Redesign visual branding from scratch.
- Add multi-symbol trading, backtesting, or advanced strategy features.

## Risks

Deep refactoring can create regressions even when behavior is intended to stay similar. The mitigation is to split the implementation into small tasks, keep tests close to each extracted service, and run full backend, frontend, Cypress, and Docker verification before declaring completion.

Allowing breaking changes simplifies cleanup but can invalidate existing Cypress selectors and README examples. The implementation must update tests and documentation in the same branch.

Skipping data migration is acceptable for this stage, but it must be documented clearly so stale local databases do not create confusing behavior after model changes.

## Acceptance Criteria

- `runner.py` no longer contains direct implementations of every trading action and persistence detail.
- Backend service boundaries are testable without starting the full app.
- Frontend API calls are grouped by domain behind a shared client.
- Dashboard separates unavailable data from real zero values.
- Strategy and credentials forms use consistent save and error behavior.
- Backend pytest suite passes.
- Frontend production build passes.
- Cypress key flows are updated and pass in the local environment.
- Docker Compose can build and start the stack locally, and `/api/health` responds.
