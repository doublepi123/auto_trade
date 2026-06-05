# P34 Diagnostics Panels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface backend `/api/diagnostics` on Dashboard and Review as read-only operational panels.

**Architecture:** Keep diagnostics read-only. Extend frontend types and strategy API client, load diagnostics in Dashboard periodic refresh and in Review query flow, and render concise panels for runner/thread/quote stream/pending/runtime health. No control actions are added.

**Tech Stack:** Vue 3, Element Plus, Cypress, Vite.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `frontend/src/types/index.ts` | Add diagnostics response interfaces. | Modify |
| `frontend/src/api/strategy.ts` | Add `getDiagnostics()`. | Modify |
| `frontend/src/views/Dashboard.vue` | Add diagnostics panel and periodic loading. | Modify |
| `frontend/src/views/Review.vue` | Add symbol-focused diagnostics card. | Modify |
| `frontend/cypress/support/e2e.ts` | Stub diagnostics endpoint for shared specs. | Modify |
| `frontend/cypress/e2e/dashboard.cy.ts` | Cover Dashboard diagnostics panel. | Modify |
| `frontend/cypress/e2e/review_runtime_history.cy.ts` | Cover Review diagnostics card. | Modify |
| `docs/Roadmap.md` | Mark P34 complete and update next plan. | Modify |

---

## Task 1: Failing tests

- [x] Add Dashboard diagnostics Cypress test
- [x] Add Review diagnostics Cypress test
- [x] Run focused Cypress specs and confirm RED

## Task 2: Implementation

- [x] Add diagnostics frontend types and API client
- [x] Add Dashboard diagnostics panel with periodic refresh
- [x] Add Review diagnostics card scoped to queried symbol
- [x] Add shared Cypress diagnostics stub

## Task 3: Verification and roadmap

- [x] Run focused Cypress specs
- [x] Run frontend `type-check`, `build`, and `build:check-chunks`
- [x] Update roadmap to the next iteration

---

## Self-review

### Spec coverage

- Dashboard diagnostics panel: covered.
- Review diagnostics card: covered.
- Read-only/no controls: covered.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
