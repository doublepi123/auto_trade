# P33 Wave 1 Multi-symbol Quote Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the runner subscribes and resubscribes to every active symbol runtime quote stream, not only the primary strategy symbol.

**Architecture:** Reuse existing `SymbolRuntime` state from P28. Introduce runner helpers that derive the desired quote subscription set from primary strategy symbol plus current `_symbol_runtimes`. Startup, disconnect recovery, stale-stream resubscribe, credential reload, and strategy reload all go through the same multi-symbol subscription path. `quotes_subscribed` remains a coarse boolean for “at least one active stream attached”.

**Tech Stack:** Python 3.11, pytest, FastAPI TestClient lifespan E2E, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/runner.py` | Subscribe/resubscribe all symbol runtimes. | Modify |
| `backend/tests/test_e2e_restart.py` | Cover multi-symbol startup + disconnect resubscribe lifecycle. | Modify |
| `docs/Roadmap.md` | Mark P33 Wave 1 complete and update next plan. | Modify |

---

## Task 1: Failing E2E test

**Files:**
- Modify: `backend/tests/test_e2e_restart.py`

- [x] Add failing test for primary + watchlist symbol subscriptions surviving disconnect resubscribe
- [x] Run focused test and confirm RED

---

## Task 2: Runner implementation

**Files:**
- Modify: `backend/app/runner.py`

- [x] Add helpers to derive desired quote symbols and subscribe a symbol list
- [x] Use helper during startup, strategy reload, credential reload, disconnect resubscribe, and stale resubscribe
- [x] Keep existing primary-only status semantics unchanged

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused test plus backend suite/typecheck
- [x] Update roadmap to next P33 wave
- [x] Report results

---

## Self-review

### Spec coverage

- Multi-symbol quote lifecycle: covered.
- Restart/disconnect path: covered by TestClient lifespan E2E.
- No new trading policy change: only stream attachment behavior changes.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
