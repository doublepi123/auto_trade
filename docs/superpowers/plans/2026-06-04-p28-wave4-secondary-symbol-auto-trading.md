# P28 Wave 4 Secondary Symbol Auto-trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable quote-triggered auto trading for secondary `SymbolRuntime` symbols while preserving primary `/api/status` compatibility and global risk behavior.

**Architecture:** Keep `AppRunner.engine` as the primary cockpit engine and keep LLM/manual order paths single-primary. Route each quote to its `SymbolRuntime.engine`; if that engine triggers, execute the order with symbol-specific market/params and symbol-scoped pending checks. Global `RiskController`, `_trigger_in_flight`, notifications, and broker stay shared.

**Tech Stack:** Python 3.11, pytest, basedpyright, existing `AppRunner` and `TradeExecutionService` patterns.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/runner.py` | Route secondary quote triggers through their runtime engine and execute symbol-specific orders. | Modify |
| `backend/tests/test_runner.py` | Update secondary quote behavior and add secondary auto-trading regression coverage. | Modify |
| `docs/Roadmap.md` | Mark P28 Wave 4 complete and update next iteration plan. | Modify |

---

## Task 1: Secondary auto-trading tests

**Files:**
- Modify: `backend/tests/test_runner.py`

- [x] **Step 1: Add failing tests**

Add tests proving a secondary symbol quote can submit an order without mutating the primary engine, and that same-symbol pending blocks duplicate secondary submissions.

- [x] **Step 2: Verify RED**

Run focused tests and confirm failures come from current secondary quote early return / missing secondary execution.

---

## Task 2: Runner implementation

**Files:**
- Modify: `backend/app/runner.py`

- [x] **Step 1: Add engine-specific snapshot helpers**

Add `_engine_snapshot_for()`, `_restore_engine_snapshot_for()`, and `_restore_engine_state_preserve_trigger_for()` so secondary runtime rollback does not mutate the primary engine.

- [x] **Step 2: Route quote processing by symbol runtime**

In `_on_quote()`, choose the runtime engine for `quote.symbol`. Primary quote keeps existing `self.engine`; secondary quote uses `runtime.engine` and no longer returns before trigger evaluation.

- [x] **Step 3: Use symbol-scoped pending guard**

Replace the trigger guard with `pending_order_for(quote.symbol)` so one symbol's pending order does not block other symbols.

- [x] **Step 4: Execute with runtime params**

When a secondary runtime triggers, pass `trigger_symbol`, `runtime.market`, `runtime.engine.params.min_profit_amount`, and the engine-specific restore callbacks to `TradeExecutionService.execute()`.

- [x] **Step 5: Preserve primary status semantics**

Do not replace `self.engine`; `/api/status`, LLM execution, and manual order paths remain primary-only.

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run focused runner tests**

Run secondary auto-trading focused tests plus existing primary quote tests.

- [x] **Step 2: Run backend verification**

Run `test_runner.py`, full backend pytest, and basedpyright.

- [x] **Step 3: Update roadmap**

Mark P28 Wave 4 complete and set next iteration to P29 production observability.

---

## Self-review

### Spec coverage

- Multi-symbol quote-triggered auto trading: covered for secondary runtime BUY trigger.
- Pending isolation: uses existing P28 Wave 1 symbol-scoped pending guard.
- Runtime isolation: uses existing P28 Wave 2/3 `SymbolRuntime` and symbol persistence.
- Global risk remains shared: no `RiskController` split.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。

### Type consistency

- Primary engine: `self.engine`.
- Secondary engine: `SymbolRuntime.engine`.
- Pending guard: `TradeExecutionService.pending_order_for(symbol)`.
