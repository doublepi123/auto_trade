# P74-P78 LLM Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only LLM runtime observability tab to Lab.

**Architecture:** Reuse existing frontend API functions `getLLMIntervalStatus()` and `getLLMInteractions()` from `llm_advisor.ts`; keep backend untouched. Render derived runtime cards/tables in `Lab.vue` and cover behavior through Cypress stubs.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, TypeScript strict mode, Cypress.

---

### File Structure

- Modify `frontend/src/views/Lab.vue`: add runtime tab, state, loader, health computed helpers, and styles.
- Modify `frontend/cypress/support/e2e.ts`: add default stubs for LLM interval status/interactions if missing.
- Modify `frontend/cypress/e2e/lab.cy.ts`: add RED/GREEN coverage for P74-P78.
- Modify `docs/Roadmap.md`: add P74-P78 completion summary after implementation.

### Task 1: RED Cypress Coverage

- [ ] Add a Cypress test that opens Lab, clicks「运行状态」, waits for `/api/strategy/llm-interval/status` and `/interactions`, and asserts overview, budget, symbol rows, interaction rows, and health hints.
- [ ] Run focused Lab Cypress and confirm it fails because the runtime tab does not exist.

### Task 2: Runtime Data Wiring

- [ ] Import `getLLMIntervalStatus` and `getLLMInteractions` in `Lab.vue`.
- [ ] Add refs: `runtimeStatus`, `runtimeInteractions`, `runtimeLoading`, `runtimeLoaded`.
- [ ] Add `loadRuntimeStatus()` using `Promise.all` and error toast.

### Task 3: P74/P75 Overview and Budget Cards

- [ ] Add Lab tab pane `name="runtime"`.
- [ ] Render enabled state, last/next analysis time, current suggestion.
- [ ] Render budget values: hourly limit, effective symbol budget, used analyses, remaining, reset time.

### Task 4: P76/P77 Tables

- [ ] Render `symbol_statuses` table.
- [ ] Render recent interaction table with success/applied tags.

### Task 5: P78 Health Hints and Docs

- [ ] Add computed `runtimeHealthHints` for disabled LLM, budget exhausted, stale/no next analysis, and skip reasons.
- [ ] Update Roadmap with P74-P78 summary.

### Task 6: Verification

- [ ] Run focused Lab Cypress.
- [ ] Run `npm run type-check`.
- [ ] Run `npm run build`.
- [ ] Request @oracle review and fix Critical/Important items.

## Self-Review

- Spec coverage: P74-P78 each maps to a visible runtime tab element and a Cypress assertion.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: existing `LLMIntervalStatus` and `LLMInteractionRecord` interfaces provide all needed fields.
