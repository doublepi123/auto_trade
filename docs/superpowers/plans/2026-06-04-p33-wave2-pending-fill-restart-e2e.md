# P33 Wave 2 Pending Fill Restart E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore symbol-scoped pending orders from persisted live orders at restart so multi-symbol pending/fill lifecycle remains consistent after process restart.

**Architecture:** Reuse `OrderRecord` as the persisted source of truth for live orders. `AppRunner` will rebuild `_PendingOrder` entries from DB live orders after broker sync, preserving existing in-memory metadata when available and deriving `submitted_at` from `created_at` for timeout continuity. Filled orders remain DB-only and must never reappear as pending.

**Tech Stack:** Python 3.11, pytest TestClient lifespan E2E, SQLAlchemy sync ORM, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/services/trade_execution_service.py` | Add pending order replacement loader for restart/sync hydration. | Modify |
| `backend/app/runner.py` | Rebuild pending orders from live `OrderRecord` rows after broker sync/startup. | Modify |
| `backend/tests/test_e2e_restart.py` | Cover multi-symbol pending/fill/restart consistency. | Modify |
| `docs/Roadmap.md` | Mark P33 Wave 2 complete and update next plan. | Modify |

---

## Task 1: Failing E2E

**Files:**
- Modify: `backend/tests/test_e2e_restart.py`

- [x] Add failing E2E for one symbol pending + another filled on restart
- [x] Verify RED

---

## Task 2: Implementation

**Files:**
- Modify: `backend/app/services/trade_execution_service.py`
- Modify: `backend/app/runner.py`

- [x] Add `load_pending_orders()` preserving existing metadata where possible
- [x] Add runner helper to rebuild pending orders from DB live orders
- [x] Call helper during startup and broker order sync

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused E2E + targeted backend verification
- [x] Update roadmap to next iteration
- [x] Report results

---

## Self-review

### Spec coverage

- Restart pending restoration: covered.
- Filled orders must not rehydrate as pending: covered by E2E assertion.
- Multi-symbol consistency: covered by pending symbol + separate filled symbol in one scenario.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
