# P37 Wave 1 Symbol State Composables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract shared symbol-history and diagnostics request/state logic from Dashboard and Review into reusable composables without changing behavior.

**Architecture:** Keep page-specific rendering and labels where they are. Introduce one composable for runtime history loading and one for diagnostics loading/selection, then wire Dashboard and Review to those composables. Existing Cypress specs remain the regression proof.

**Tech Stack:** Vue 3 Composition API, TypeScript strict, Cypress.

---

## Task 1: Regression proof
- [x] Use existing dashboard/review Cypress specs as the red/green safety net for the refactor.

## Task 2: Implementation
- [x] Create `useStatusHistorySeries.ts` for shared history state/loading/error/load/reset.
- [x] Create `useDiagnosticsSnapshot.ts` for shared diagnostics state/loading/error/runtime selection.
- [x] Rewire Dashboard and Review to the shared composables with no behavior change.

## Task 3: Verification and roadmap
- [x] Run focused Cypress + frontend build checks.
- [x] Update roadmap to the next iteration.
- [x] Report results.

---

## Self-review
- 无占位词。
- 纯结构整理，不改变接口和页面行为。
