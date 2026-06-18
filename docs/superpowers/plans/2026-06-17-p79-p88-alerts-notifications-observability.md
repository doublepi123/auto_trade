# P79–P88 Alerts & Notifications Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 10 read-only observability improvements across Notification Center and Alert Rules.

**Architecture:** All work is frontend-only. Notification and alert summaries are computed from already loaded rows; no new API, table, scheduler, broker, order, runner, or risk behavior is introduced.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress 15, existing axios API clients.

---

## Files

- Modify: `frontend/src/views/NotificationCenter.vue` — P79–P83 notification summary, search, chips, day grouping, empty/result copy.
- Modify: `frontend/cypress/e2e/notification_center.cy.ts` — RED/GREEN coverage for P79–P83.
- Modify: `frontend/src/views/AlertRules.vue` — P84–P88 rule health cards, filters, recent firing summary, history summaries.
- Modify: `frontend/cypress/e2e/alert_firings.cy.ts` — RED/GREEN coverage for P84–P88.
- Modify: `docs/Roadmap.md` — document P79–P88 after validation.

## Tasks

### Task 1: P79–P83 Notification Center observability

- [ ] Add Cypress RED test in `frontend/cypress/e2e/notification_center.cy.ts` that visits `/#/notifications`, stubs 4 notifications, and asserts severity summary, quick filters, search, day grouping, and empty context.
- [ ] Run focused Cypress and confirm failure because the new UI is missing.
- [ ] Implement computed `keyword`, `quickFilter`, `filteredItems`, `notificationStats`, `groupedNotifications`, and empty/result copy in `NotificationCenter.vue`.
- [ ] Render summary cards, search input, quick filter buttons, day grouping cards, and table bound to `filteredItems`.
- [ ] Keep server severity dropdown behavior unchanged.
- [ ] Run focused Cypress and type-check.

### Task 2: P84–P88 Alert firing observability

- [ ] Add Cypress RED tests in `frontend/cypress/e2e/alert_firings.cy.ts` that visit `/#/alerts`, stubs 4 rules and one rule history, and asserts rule health cards, rule filters, recent firing summary, history stats, severity counts, and message context.
- [ ] Run focused Cypress and confirm failure because the new UI is missing.
- [ ] Implement computed `ruleFilter`, `filteredRules`, `ruleStats`, and `recentFiredRules` in `AlertRules.vue`.
- [ ] Render rule health cards, quick filter buttons, recent firing summary, and bind table to `filteredRules`.
- [ ] Implement history dialog computed stats from `historyDialog.items` and render summary cards plus severity count text.
- [ ] Run focused Cypress and type-check.

### Task 3: Documentation and final verification

- [ ] Update `docs/Roadmap.md` with P79–P88 and validation evidence.
- [ ] Run `npm run type-check`.
- [ ] Run `npm run build`.
- [ ] Run focused Cypress specs: `notification_center.cy.ts` and `alert_firings.cy.ts`.
- [ ] Run `git diff --check`.
- [ ] Request code review and address Critical/Important issues.
