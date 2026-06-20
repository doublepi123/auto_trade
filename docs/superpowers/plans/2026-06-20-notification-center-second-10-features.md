# Notification Center Second 10 Feature Iterations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ten additional low-risk notification triage helpers on top of the existing notification center enhancements.

**Architecture:** Keep this batch frontend-only in `NotificationCenter.vue`, deriving all new behavior from the currently loaded notification page and local UI state. Avoid backend contract changes because the available Python runtime cannot validate backend changes against the pinned dependency set.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress E2E.

---

## File Map

- Modify: `frontend/src/views/NotificationCenter.vue`
  - Add unread-only view, copy helpers, source derivation/filtering, result grouping, quick time ranges, search highlighting, persisted view preferences, error-rate summary, and detail navigation.
- Modify: `frontend/cypress/e2e/notification_center.cy.ts`
  - Add a focused spec that proves the ten new behaviors.

## Tasks

### Task 1: Write failing Cypress coverage

- [x] Add a Cypress test named `supports second-wave triage helpers` covering: unread-only toggle, copy page summary, copy title, inferred category filter, result-group view, quick date range, highlight, persisted view preferences, error-rate tag, and detail previous/next navigation.
- [x] Run `npx cypress run --config baseUrl=http://127.0.0.1:3010 --spec cypress/e2e/notification_center.cy.ts` with Vite on port 3010 and verify the new test fails because the new controls do not exist.

### Task 2: Implement minimal Vue behavior

- [x] Add local UI refs/computed values for `unreadOnly`, inferred category filter, `groupMode`, quick range labels, highlighted text fragments, persisted preferences, and detail navigation index.
- [x] Add template controls and render states for all ten features.
- [x] Keep all filtering clearly labeled as current-page/text-inferred behavior.

### Task 3: Verify and review

- [x] Run focused Cypress spec until all notification-center tests pass.
- [x] Run `npm run type-check`.
- [x] Run `npm run build`.
- [x] Request code review and fix Critical/Important findings.

## Verification Record

- RED: focused Cypress failed on missing `notif-error-rate` before implementation.
- GREEN: focused Cypress passed with `3 passing` after implementation and review fixes.
- `npm run type-check` passed.
- `npm run build` passed.
- Code review found no Critical issues; Important issues were fixed by renaming source to inferred category, changing the quick range wording, implementing real text highlighting, guarding localStorage writes, and keeping retry available in result grouping.
