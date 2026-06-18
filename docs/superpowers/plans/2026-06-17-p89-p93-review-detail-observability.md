# P89–P93 Review Detail Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five read-only Review page details that expose day composition, derived day state, order fills, event payloads, and snapshot deltas.

**Architecture:** Frontend-only Vue changes in `Review.vue`, tested by Cypress. All values are computed inline from existing `ReviewResponse` fields and no new API calls are introduced.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, Cypress 15, existing typed API clients.

---

## Files

- Modify: `frontend/src/views/Review.vue` — render new day card and row-level details; add small formatting helpers.
- Modify: `frontend/cypress/e2e/review_runtime_history.cy.ts` — add RED/GREEN coverage for P89–P93.
- Modify: `docs/Roadmap.md` — record P89–P93 after validation.

## Tasks

### Task 1: RED Cypress coverage

- [ ] Add a new Cypress test to `frontend/cypress/e2e/review_runtime_history.cy.ts` that stubs `/api/review`, `/api/status/history*`, and `/api/diagnostics`.
- [ ] Fixture includes one day with `daily_pnl: -12.5`, `trade_count: 1`, one error tag `FEE`, one LLM interaction, one filled order with `broker_order_id`, `executed_quantity`, `executed_price`, `filled_at`, one event with `payload_json`, and one snapshot with `last_price`, `last_trigger_price`, `consecutive_losses`.
- [ ] Assert missing new UI: `[data-testid="review-day-composition"]`, `[data-testid="review-day-state"]`, `[data-testid="review-order-detail"]`, `[data-testid="review-event-payload"]`, and `[data-testid="review-snapshot-detail"]`.
- [ ] Run focused Cypress and confirm failure because the UI is missing.

### Task 2: P89–P90 day-level context

- [ ] In `Review.vue`, add a day composition strip below each `.day-header` with `data-testid="review-day-composition"` containing LLM/order/event/snapshot/error counts.
- [ ] Add derived state badges with `data-testid="review-day-state"`: 盈利/亏损/打平, 有交易/无交易, 有错误/无错误.
- [ ] Run focused Cypress and keep failures limited to row-level details.

### Task 3: P91–P93 row-level details

- [ ] In order rows, add `data-testid="review-order-detail"` showing broker id, filled quantity, filled price, and filled time.
- [ ] In event rows, add `data-testid="review-event-payload"` showing compact payload text from `payload_json`.
- [ ] In snapshot rows, add `data-testid="review-snapshot-detail"` showing trigger price, price-vs-trigger delta, consecutive losses, and time.
- [ ] Add helper functions for `formatFilledQuantity`, `formatPayloadPreview`, and `formatPriceDelta` without using `any`.
- [ ] Run focused Cypress and type-check.

### Task 4: Docs and validation

- [ ] Update `docs/Roadmap.md` with P89–P93 summary and validation evidence.
- [ ] Run `npm run type-check`.
- [ ] Run `npm run build`.
- [ ] Run focused Cypress specs including `review_runtime_history.cy.ts` plus previously touched specs.
- [ ] Run `git diff --check`.
- [ ] Request code review and address Critical/Important issues.
