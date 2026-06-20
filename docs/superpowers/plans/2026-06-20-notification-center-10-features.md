# Notification Center 10 Feature Iterations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ten small, user-visible Notification Center enhancements with Cypress coverage and minimal frontend-only risk.

**Architecture:** Keep all behavior in `NotificationCenter.vue` as derived client-side state over the already-loaded notification page. Avoid backend/API contract changes because the local Python 3.13 environment cannot install the backend's pinned Pydantic 2.0 stack.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress E2E.

---

## File Map

- Modify: `frontend/src/views/NotificationCenter.vue`
  - Add active filter chips, reset button, symbol/page-size/sort controls, richer summary/detail display, and current-page derived metrics.
- Modify: `frontend/cypress/e2e/notification_center.cy.ts`
  - Add a focused failing spec first for the ten new behaviors.

## Tasks

### Task 1: Cypress coverage for all ten notification-center enhancements

- [x] Add a new test case that asserts: success rate, time span, active chips, reset filters, symbol filter, page size, newest/oldest sort, failed-only note, detail metadata, copy content button.
- [x] Run the spec and verify it fails before implementation.

### Task 2: Implement minimal Vue changes

- [x] Add computed `displayedItems`, `symbolOptions`, `activeFilterLabels`, `summaryMetrics`, and `sortedItems` state.
- [x] Replace render sites that show `items` with `displayedItems` where the feature should be client-side.
- [x] Add controls and detail metadata/copy action.
- [x] Run the focused Cypress spec and type-check/build where available.

### Task 3: Verification and cleanup

- [x] Run frontend type verification with installed local tooling if available; otherwise record environment blocker.
- [x] Run relevant Cypress spec if Cypress binary is available; otherwise record environment blocker.
- [x] Inspect diff for minimal scope and no unrelated formatting.

## Verification Record

- RED: `npx cypress run --config baseUrl=http://127.0.0.1:3010 --spec cypress/e2e/notification_center.cy.ts` failed on missing `notif-success-rate` before implementation.
- GREEN: focused Cypress spec passed after implementation.
- `npm run type-check` passed.
- `npm run build` passed.
- Backend baseline was not runnable in this container because only Python 3.13 is available and the backend's pinned Pydantic 2.0 stack requires the project-supported Python 3.11/3.12 environment.
