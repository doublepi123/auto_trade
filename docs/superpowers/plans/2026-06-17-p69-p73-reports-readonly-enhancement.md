# P69-P73 Reports Readonly Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance the existing Reports page using only the current `/api/reports/range` response.

**Architecture:** Keep backend services, schemas, and routes unchanged. Add computed frontend projections and Element Plus presentation in `Reports.vue`; update Cypress report fixtures to prove the new UI behavior. No new API calls are required.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, TypeScript strict mode, Cypress.

---

### File Structure

- Modify `frontend/src/views/Reports.vue`: add shortcut range buttons, attribution table, detail expandable rows, insight cards, and export summary helpers.
- Modify `frontend/cypress/e2e/reports.cy.ts`: add failing tests first, then assertions for all five P69-P73 behaviors.
- Modify `docs/Roadmap.md`: add the completed P69-P73 summary after implementation.

### Task 1: RED Cypress Coverage

- [x] Add Cypress assertions to `frontend/cypress/e2e/reports.cy.ts` for: shortcut range buttons, attribution table values, expandable daily order details, insight cards, and export filename preview.
- [x] Run `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/reports.cy.ts` against a temporary Vite server and confirm the new assertions fail because the UI does not exist yet.

### Task 2: P69 Shortcut Date Ranges

- [x] In `Reports.vue`, add 7/30/90 day shortcut buttons in the filter card.
- [x] Implement `applyRangePreset(days: number)` using existing `daysAgo()` and `formatDate()` helpers, then call `handleSearch()`.

### Task 3: P70 Attribution Table

- [x] Render `reportData.attribution` in a new card with columns: label, trade_count, pnl, win_rate, share.
- [x] Add an empty state when attribution is empty.

### Task 4: P71 Daily Drill-down

- [x] Add expandable rows to the daily details table.
- [x] For each daily point, map `reportData.details` by date and render order rows with broker_order_id, side, quantity, executed_price, status, filled_at, pnl.

### Task 5: P72/P73 Insights and Export Polish

- [x] Add computed insight cards: best day, worst day, profitable days, losing days, max drawdown day.
- [x] Add current query range summary and export filename preview.
- [x] Update `docs/Roadmap.md` with P69-P73 summary.

### Task 6: Verification

- [x] Run focused Cypress reports spec.
- [x] Run `npm run type-check`.
- [x] Run `npm run build`.
- [x] Request @oracle code review and fix Critical/Important issues.

## Self-Review

- Spec coverage: P69-P73 each maps to one UI feature and Cypress assertion.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: all fields already exist in `ReportResponse`, `ReportAttributionPoint`, and `ReportDayDetail`/`ReportOrderDetail`.
