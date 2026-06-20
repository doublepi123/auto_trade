# Watchlist Triage 10 Feature Iterations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ten frontend-only Watchlist triage helpers that make large watchlists easier to search, filter, sort, copy, export, and refresh.

**Architecture:** Keep changes in `Watchlist.vue` and one focused Cypress spec. All new behavior derives from already-loaded watchlist items, quotes, and scores; existing backend APIs remain unchanged.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress E2E.

---

## File Map

- Modify: `frontend/src/views/Watchlist.vue`
  - Add client-side search/filter/sort controls, selection state, copy/export helpers, refresh timestamp, and filtered empty-state hint.
- Add: `frontend/cypress/e2e/watchlist_triage.cy.ts`
  - Cover the 10 new user-visible behaviors with stubbed APIs.

## Tasks

### Task 1: Red Cypress test

- [x] Add `watchlist_triage.cy.ts` with assertions for: search, market filter, active filter, score bucket, hide stale scores, sort toggle, copy symbol, bulk selection export, bulk delete, manual refresh timestamp, and filtered empty-state hint.
- [x] Run focused Cypress and verify the new spec fails because controls do not exist.

### Task 2: Vue implementation

- [x] Add refs/computed state for `searchText`, `marketFilter`, `statusFilter`, `scoreBucket`, `hideStaleScores`, `sortMode`, `selectedIds`, `lastRefreshAt`, and `filteredItems`.
- [x] Render toolbar controls and switch table data from `items` to `filteredItems`.
- [x] Add row selection, copy symbol, bulk CSV export, bulk delete, and refresh timestamp.
- [x] Keep labels explicit: filters and exports are current-client-list only.

### Task 3: Verification and review

- [x] Run focused Watchlist Cypress spec until green.
- [x] Run `npm run type-check`.
- [x] Run `npm run build`.
- [x] Request code review and fix Critical/Important feedback.

## Verification Record

- RED: `watchlist_triage.cy.ts` failed on missing `watchlist-filter-summary` before implementation.
- GREEN: focused Watchlist Cypress passed after implementation and review fixes.
- `npm run type-check` passed.
- `npm run build` passed.
- Code review found no Critical issues; Important issues were fixed by limiting bulk operations to visible selections, adding all-settled delete handling, clarifying successful quote refresh time, strengthening Cypress delete assertions, and stabilizing clipboard mocking.
