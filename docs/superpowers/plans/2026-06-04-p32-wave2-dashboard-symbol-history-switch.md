# P32 Wave 2 Dashboard Symbol History Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Dashboard switch the history charts between primary and observed symbols while keeping the default primary-symbol cockpit unchanged.

**Architecture:** Reuse the existing `/api/status/history?symbol=` API added in P32 Wave 1. Dashboard adds a chart-only symbol selector populated from primary strategy symbol plus multi-symbol watchlist snapshots. The rest of the cockpit remains primary-only; only `PriceChart`/`PnLChart` data source changes when the selected history symbol changes.

**Tech Stack:** Vue 3, Element Plus, Cypress, Vite.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `frontend/src/views/Dashboard.vue` | Add chart symbol selector and filtered history loading. | Modify |
| `frontend/cypress/e2e/dashboard_charts.cy.ts` | Cover switching history symbol and requesting filtered API data. | Modify |
| `docs/Roadmap.md` | Mark P32 Wave 2 complete and update next plan. | Modify |

---

## Task 1: Failing frontend test

**Files:**
- Modify: `frontend/cypress/e2e/dashboard_charts.cy.ts`

- [x] Add a test that switches chart symbol from primary to watchlist symbol and expects `/api/status/history?symbol=AAPL.US` plus updated chart sample count
- [x] Run focused Cypress spec and confirm RED

---

## Task 2: Dashboard implementation

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

- [x] Add chart symbol options from primary strategy symbol + `multiSymbolSnapshots`
- [x] Add selected chart symbol state defaulting to primary
- [x] Change `loadStatusHistory()` to request selected symbol
- [x] Reload history when selected symbol changes
- [x] Render chart symbol selector and label in chart section
- [x] Keep threshold lines only for the primary symbol

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused Cypress + frontend build checks
- [x] Update roadmap to next iteration
- [x] Report results

---

## Self-review

### Spec coverage

- Dashboard default remains primary: covered by initial selector default.
- History charts switch by symbol: covered.
- Cockpit state remains primary-only: no change to `/api/status` or top summary cards.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
