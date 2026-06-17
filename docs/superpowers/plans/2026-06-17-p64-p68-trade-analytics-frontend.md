# P64-P68 Trade Analytics Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the five existing read-only trade analytics endpoints in Trade History.

**Architecture:** Keep the backend untouched. Add TypeScript response types and typed API wrappers in the existing `trades.ts` module, then render a collapsed, supplementary analytics section in `TradeHistory.vue`. Cypress stubs and one focused history spec prove the UI consumes the endpoints.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Axios API client, Cypress, TypeScript strict mode.

---

### File Structure

- Modify `frontend/src/types/index.ts`: add interfaces for trade calendar, hold duration, PnL distribution, monthly summary, and weekday attribution responses.
- Modify `frontend/src/api/trades.ts`: add `TradeAnalyticsQuery` plus five API functions.
- Modify `frontend/src/views/TradeHistory.vue`: add a collapsed read-only analytics section below round-trip trades, load data on mount and when date filters refresh.
- Modify `frontend/cypress/support/e2e.ts`: add default intercepts for all five analytics endpoints.
- Modify `frontend/cypress/e2e/history.cy.ts`: add coverage asserting all five cards render representative values.

### Task 1: RED Cypress Coverage

- [x] Add a failing test in `frontend/cypress/e2e/history.cy.ts` that stubs `/api/trades/analytics/*`, visits `/#/history`, opens「交易分析（只读）」, and asserts the five `data-testid` cards show representative labels and values.
- [x] Run `npm run cypress:run -- --spec cypress/e2e/history.cy.ts` from `frontend/` and confirm it fails because the cards do not exist yet.

### Task 2: Types and API Client

- [x] Add response interfaces to `frontend/src/types/index.ts` matching backend schemas: `TradeCalendarResponse`, `TradeHoldDurationResponse`, `TradePnlDistributionResponse`, `TradeMonthlySummaryResponse`, `TradeWeekdayAttributionResponse`.
- [x] Add `getTradeCalendar`, `getTradeHoldDuration`, `getTradePnlDistribution`, `getTradeMonthlySummary`, and `getTradeWeekdayAttribution` to `frontend/src/api/trades.ts`.

### Task 3: TradeHistory Analytics Section

- [x] Import the five API functions and response types in `TradeHistory.vue`.
- [x] Add refs for each response plus an `analyticsLoading` flag.
- [x] Add `loadTradeAnalytics()` that calls all five endpoints with current `rtFromDate`/`rtToDate` via independent `Promise.allSettled` results.
- [x] Lazy-load analytics when「交易分析（只读）」is expanded; refresh analytics on「拉取」only while the section is open.
- [x] Render one collapsed `el-collapse` section titled「交易分析（只读）」 with five cards and the data-testid values from the test.

### Task 4: Shared Stubs and Focused Verification

- [x] Add default analytics intercepts to `frontend/cypress/support/e2e.ts` so other specs do not hit unstubbed endpoints.
- [x] Re-run `npm run cypress:run -- --spec cypress/e2e/history.cy.ts` and confirm the new test passes.

### Task 5: Full Frontend Verification

- [x] Run `npm run type-check` from `frontend/` and fix strict TypeScript errors.
- [x] Run `npm run build` from `frontend/` and fix production build issues.
- [x] Inspect `git status --short` and `git diff --stat` to ensure only intended files changed.

## Self-Review

- Spec coverage: all five P64-P68 endpoints are represented by a type, API function, UI card, and Cypress assertion.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: function and interface names match backend endpoint concepts and are imported from the existing `../api` barrel.
