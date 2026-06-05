# P30 Wave 2 LLM Budget Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose multi-symbol LLM scheduling budget and per-symbol cooldown status without changing analysis frequency.

**Architecture:** Keep scheduling behavior unchanged. Add read-only status data only: config-level LLM symbol/hour budget in `Settings`, per-symbol cooldown/pending snapshot in `AppRunner`, and extended `/api/strategy/llm-interval/status` response. This gives operators and future schedulers visibility into which symbols are eligible before any broader auto-analysis rollout.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pytest, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/config.py` | Add configurable LLM symbol/hour budget settings. | Modify |
| `backend/app/schemas.py` | Add LLM budget/status response schemas. | Modify |
| `backend/app/runner.py` | Add read-only per-symbol LLM cooldown snapshot. | Modify |
| `backend/app/api/llm_advisor.py` | Extend `GET /api/strategy/llm-interval/status` with budget and symbol states. | Modify |
| `backend/tests/test_runner.py` | Cover per-symbol LLM status snapshot. | Modify |
| `backend/tests/test_api.py` | Cover LLM interval status response includes budget/symbol states. | Modify |
| `docs/Roadmap.md` | Mark P30 Wave 2 complete and update next plan. | Modify |

---

## Task 1: Failing tests

**Files:**
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_api.py`

- [x] Add failing runner test for `llm_symbol_statuses()`
- [x] Add failing API test for `/api/strategy/llm-interval/status`
- [x] Run focused tests and confirm RED

---

## Task 2: Implementation

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/runner.py`
- Modify: `backend/app/api/llm_advisor.py`

- [x] Add `llm_max_symbols_per_cycle` and `llm_max_analyses_per_hour` settings
- [x] Add `LLMBudgetStatus` and `LLMSymbolStatus` schemas
- [x] Implement `AppRunner.llm_symbol_statuses()`
- [x] Extend LLM interval status endpoint with `budget` and `symbol_statuses`

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused tests
- [x] Run backend verification
- [x] Update roadmap to next iteration

---

## Self-review

### Spec coverage

- Budget visibility: covered by settings + status response.
- Per-symbol cooldown visibility: covered by runner snapshot.
- No scheduling behavior changes: endpoint remains read-only.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。

### Type consistency

- Runner method: `llm_symbol_statuses()`.
- Status schema: `LLMBudgetStatus`, `LLMSymbolStatus`.
- Endpoint: existing `/api/strategy/llm-interval/status` extended in place.
