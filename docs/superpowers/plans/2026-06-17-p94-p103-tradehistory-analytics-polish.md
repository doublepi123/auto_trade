# P94–P103 TradeHistory Analytics Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 10 read-only TradeHistory analytics polish features using existing closed-trade and analytics data.

**Architecture:** Frontend-only changes in `TradeHistory.vue`; all new values are computed from existing loaded responses. Cypress verifies behavior with deterministic intercepts.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress 15, existing typed API clients.

---

## Files

- Modify: `frontend/src/views/TradeHistory.vue` — add local computed summaries, filters, insights, and expandable round-trip rows.
- Modify: `frontend/cypress/e2e/trade_roundtrips.cy.ts` — add RED/GREEN coverage for P94–P98.
- Modify: `frontend/cypress/e2e/history.cy.ts` — extend analytics coverage for P99–P103.
- Modify: `docs/Roadmap.md` — document P94–P103 after validation.

## Tasks

### Task 1: RED tests

- [ ] Add a Cypress test in `trade_roundtrips.cy.ts` with multiple closed trades. Assert `roundtrip-summary`, `roundtrip-filter-*`, `roundtrip-symbol-search`, `roundtrip-insights`, and expanded row detail UI.
- [ ] Extend the analytics test in `history.cy.ts` to assert `trade-analytics-insights` contains calendar, hold-duration, pnl-distribution, monthly, and weekday summaries.
- [ ] Run focused Cypress and confirm failure because the new UI is missing.

### Task 2: P94–P98 round-trip polish

- [ ] Add `roundTripFilter` and `roundTripSymbolSearch` refs.
- [ ] Add computed `filteredClosedTrades`, `roundTripSummary`, `bestRoundTrip`, and `worstRoundTrip`.
- [ ] Render summary strip, filter chips, symbol search, and best/worst insight panel above the round-trip table.
- [ ] Bind the round-trip table to `filteredClosedTrades`.
- [ ] Add expandable row details showing entry/exit order IDs, entry/exit times, fee drag, gross PnL, net PnL.
- [ ] Run focused Cypress and type-check.

### Task 3: P99–P103 analytics insights

- [ ] Add computed insights for calendar, hold duration, PnL distribution, monthly summary, and weekday attribution.
- [ ] Render a compact insight panel above existing analytics cards with `data-testid="trade-analytics-insights"`.
- [ ] Keep all existing analytics cards and empty states unchanged.
- [ ] Run focused Cypress and type-check.

### Task 4: Docs and final validation

- [ ] Update `docs/Roadmap.md` with P94–P103 and validation evidence.
- [ ] Run `npm run type-check`.
- [ ] Run `npm run build`.
- [ ] Run focused Cypress specs touched in this branch.
- [ ] Run `git diff --check`.
- [ ] Request code review and address Critical/Important issues.
