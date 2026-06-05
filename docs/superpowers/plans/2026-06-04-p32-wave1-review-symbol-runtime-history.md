# P32 Wave 1 Review Symbol Runtime History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Review page query and visualize runtime state history for a selected symbol without changing `/api/status` primary cockpit behavior.

**Architecture:** Extend the existing `/api/status/history` endpoint with an optional `symbol` filter and return the snapshot symbol on each point. Keep the Dashboard using the existing default primary-symbol path. Review page will request filtered runtime history for its current symbol/date range and render price/PnL charts using the existing chart components.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, Vue 3, Cypress, pytest, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/schemas.py` | Add symbol to `StatusHistoryPoint`. | Modify |
| `backend/app/api/strategy.py` | Add optional `symbol` query to `/api/status/history`. | Modify |
| `backend/tests/test_api.py` | Cover symbol-filtered runtime history and marker filtering. | Modify |
| `frontend/src/types/index.ts` | Add symbol to status history point. | Modify |
| `frontend/src/api/strategy.ts` | Support filtered status history queries. | Modify |
| `frontend/src/views/Review.vue` | Load symbol runtime history and render price/PnL charts. | Modify |
| `frontend/cypress/e2e/review_runtime_history.cy.ts` | Cover symbol-scoped history query and chart rendering. | Create |
| `docs/Roadmap.md` | Mark P32 Wave 1 complete and update next plan. | Modify |

---

## Task 1: Failing tests

**Files:**
- Modify: `backend/tests/test_api.py`
- Create: `frontend/cypress/e2e/review_runtime_history.cy.ts`

- [x] Add failing API test for `/api/status/history?symbol=AAPL.US`
- [x] Add failing Cypress review chart test
- [x] Run focused tests and confirm RED

---

## Task 2: Implementation

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/strategy.py`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/strategy.ts`
- Modify: `frontend/src/views/Review.vue`

- [x] Add `symbol` to `StatusHistoryPoint`
- [x] Add optional `symbol` filter to `/api/status/history`
- [x] Filter markers by symbol when requested
- [x] Add frontend API/options support for filtered runtime history
- [x] Load and render Review runtime history charts for selected symbol

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused backend/frontend tests
- [x] Run verification suite
- [x] Update roadmap to next iteration

---

## Self-review

### Spec coverage

- Review can view runtime history by symbol: covered.
- Dashboard `/api/status` unchanged: covered by leaving default path untouched.
- Symbol snapshot context preserved in response: covered by `StatusHistoryPoint.symbol`.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
