# P29 Runner Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only diagnostics endpoint that exposes runner, quote stream, pending-order, risk, and symbol-runtime health for production troubleshooting.

**Architecture:** Keep diagnostics read-only and side-effect free. `AppRunner.diagnostics()` builds a stable dictionary under `_state_lock`; `GET /api/diagnostics` returns typed Pydantic response from the strategy router. No broker calls, DB writes, order actions, or notifier calls are allowed.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pytest, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/runner.py` | Build side-effect-free diagnostics snapshot. | Modify |
| `backend/app/schemas.py` | Add diagnostics response schemas. | Modify |
| `backend/app/api/strategy.py` | Expose `GET /api/diagnostics`. | Modify |
| `backend/tests/test_runner.py` | Cover diagnostics snapshot content. | Modify |
| `backend/tests/test_api.py` | Cover diagnostics endpoint response. | Modify |
| `docs/Roadmap.md` | Mark P29 complete and update next plan. | Modify |

---

## Task 1: Diagnostics tests

**Files:**
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_api.py`

- [x] **Step 1: Add runner diagnostics test**

Add a test proving `AppRunner.diagnostics()` reports primary and secondary symbol runtimes, pending symbols, quote subscription state, and global risk.

- [x] **Step 2: Add API diagnostics test**

Add a test proving `GET /api/diagnostics` returns the runner diagnostics payload with typed fields.

- [x] **Step 3: Verify RED**

Run focused tests and confirm failures are from missing diagnostics API/method.

---

## Task 2: Implementation

**Files:**
- Modify: `backend/app/runner.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/strategy.py`

- [x] **Step 1: Add diagnostics response schemas**

Add `DiagnosticQuoteStream`, `DiagnosticRiskState`, `DiagnosticSymbolRuntime`, and `DiagnosticsResponse` in `schemas.py`.

- [x] **Step 2: Add `AppRunner.diagnostics()`**

Return a dict with `runner_running`, `thread_alive`, `quotes_subscribed`, `trigger_in_flight`, `pending_order_symbols`, `quote_stream`, `risk`, and `symbol_runtimes`.

- [x] **Step 3: Add endpoint**

Add `@router.get('/diagnostics', response_model=DiagnosticsResponse)` in `api/strategy.py` returning `DiagnosticsResponse.model_validate(get_runner().diagnostics())`.

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run focused diagnostics tests**

Run runner/API diagnostics tests.

- [x] **Step 2: Run backend verification**

Run `test_runner.py`, `test_api.py`, full backend pytest, and basedpyright.

- [x] **Step 3: Update roadmap**

Mark P29 complete and set next iteration to P30 multi-symbol LLM scheduling.

---

## Self-review

### Spec coverage

- Production observability: diagnostics endpoint covers runner, quote stream, pending, risk, and symbol runtime state.
- No trading behavior changes: endpoint/method are read-only.
- Multi-symbol support: diagnostics includes every symbol runtime and pending symbols.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。

### Type consistency

- Method: `AppRunner.diagnostics() -> dict[str, Any]`.
- Endpoint: `GET /api/diagnostics`.
- Response schema: `DiagnosticsResponse`.
