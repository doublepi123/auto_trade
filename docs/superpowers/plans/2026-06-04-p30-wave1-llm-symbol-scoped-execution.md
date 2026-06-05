# P30 Wave 1 LLM Symbol-scoped Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow LLM order decisions to target a secondary symbol runtime explicitly while preserving primary-symbol defaults.

**Architecture:** Keep existing LLM advisor analysis endpoint primary-symbol by default. Extend `AppRunner.execute_llm_order_decision()` and internal helpers so a decision payload with `symbol` uses that symbol's `SymbolRuntime.engine`, symbol-scoped pending order, cooldown key, quote, market, cash currency, fee rate, and rollback callbacks. Decisions without `symbol` keep the existing primary `self.engine` path.

**Tech Stack:** Python 3.11, pytest, basedpyright, existing `AppRunner` + `TradeExecutionService` patterns.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/runner.py` | Add symbol-aware LLM decision execution helpers and keep primary fallback. | Modify |
| `backend/app/api/llm_advisor.py` | Make account pending context symbol-specific. | Modify |
| `backend/tests/test_runner.py` | Cover secondary symbol LLM execution and symbol-scoped cooldown. | Modify |
| `backend/tests/test_api.py` | Cover account context selects pending order for requested symbol. | Modify |
| `docs/Roadmap.md` | Mark P30 Wave 1 complete and update next iteration plan. | Modify |

---

## Task 1: LLM symbol-scope tests

**Files:**
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_api.py`

- [x] **Step 1: Add failing runner tests**

Add tests proving `execute_llm_order_decision({'symbol': 'AAPL.US', 'order_action': 'BUY'})` uses the secondary runtime engine and records cooldown under `('AAPL.US', 'BUY')` without mutating primary engine.

- [x] **Step 2: Add failing account-context test**

Add a test proving `_account_context('AAPL.US', 'US', 199.0, False)` reports the AAPL pending order even if another symbol also has a pending order.

- [x] **Step 3: Verify RED**

Run focused tests and confirm failures are from primary-only LLM execution / global pending context.

---

## Task 2: Implementation

**Files:**
- Modify: `backend/app/runner.py`
- Modify: `backend/app/api/llm_advisor.py`

- [x] **Step 1: Add runtime resolver**

Add `_runtime_for_symbol(symbol)` returning `(symbol, market, engine)` where missing/primary symbol resolves to `self.engine`.

- [x] **Step 2: Make state mutation helper engine-specific**

Add `_set_engine_state_for_order_action_on(engine, action)` and keep `_set_engine_state_for_order_action(action)` as primary wrapper.

- [x] **Step 3: Make LLM precheck symbol-specific**

Add symbol/params arguments to `_precheck_llm_action()` so cooldown and repricing use the target symbol/engine params.

- [x] **Step 4: Make `_execute_llm_trade_action()` symbol-aware**

Accept `symbol`, `market`, `engine` optional keyword args; use engine-specific snapshot/restore, cash currency, fee, min profit, cooldown key.

- [x] **Step 5: Use symbol-specific pending order**

In `execute_llm_order_decision()`, derive target symbol from decision payload and use `pending_order_for(symbol)` / `cancel_pending_order_for_symbol(symbol)`.

- [x] **Step 6: Fix account context pending order**

In `_account_context()`, use `runner._trade_svc.pending_order_for(symbol)` when available.

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run focused tests**

Run runner/API LLM symbol-scope tests.

- [x] **Step 2: Run backend verification**

Run `test_runner.py`, `test_api.py`, full backend pytest, and basedpyright.

- [x] **Step 3: Update roadmap**

Mark P30 Wave 1 complete and set next iteration to P30 Wave 2 LLM multi-symbol scheduling budget/status.

---

## Self-review

### Spec coverage

- Multi-symbol LLM execution: covered for explicit decision symbol.
- Cooldown isolation: covered by target symbol cooldown key.
- Primary compatibility: decisions without symbol keep old path.
- Advisor scheduling remains unchanged: Wave 2 will handle budget/status scheduling.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。

### Type consistency

- Decision field: `symbol`.
- Runtime resolver: `_runtime_for_symbol(symbol: str | None)`.
- Pending access: `pending_order_for(symbol)` and `cancel_pending_order_for_symbol(symbol)`.
