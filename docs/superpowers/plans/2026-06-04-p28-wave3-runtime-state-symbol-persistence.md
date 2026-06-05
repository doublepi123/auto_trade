# P28 Wave 3 Runtime State Symbol Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add symbol-scoped runtime state and snapshot persistence so P28 Wave 2 `SymbolRuntime` engines can survive restarts without enabling multi-symbol auto trading.

**Architecture:** Preserve the existing primary runtime row as `symbol=''` for full backward compatibility with `/api/status`, risk persistence, and current tests. Add `symbol` columns to `runtime_state` and `runtime_state_snapshots`; secondary runtimes persist only engine fields under their real symbol, while global risk remains on the primary row. `RuntimeStateService` gains symbol-aware helpers used by `AppRunner._sync_symbol_runtimes()` and the run-loop persist step.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 sync ORM, SQLite manual ALTER migrations, pytest, basedpyright.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/models.py` | Add `symbol` fields to runtime persistence models. | Modify |
| `backend/app/database.py` | Add manual migration functions for legacy DBs. | Modify |
| `backend/app/services/strategy_service.py` | Scope runtime state reads/writes by symbol with default `''`. | Modify |
| `backend/app/services/runtime_state_service.py` | Add symbol-aware load/persist/history APIs. | Modify |
| `backend/app/runner.py` | Load secondary runtime state and persist secondary runtimes. | Modify |
| `backend/tests/test_runtime_state_service.py` | Cover scoped runtime state and history. | Modify |
| `backend/tests/test_database.py` | Cover legacy symbol column migration. | Modify |
| `backend/tests/test_runner.py` | Cover runner loading persisted secondary runtime. | Modify |
| `docs/Roadmap.md` | Mark P28 Wave 3 complete and shift next plan. | Modify |

---

## Task 1: Symbol-scoped tests

**Files:**
- Modify: `backend/tests/test_runtime_state_service.py`
- Modify: `backend/tests/test_database.py`
- Modify: `backend/tests/test_runner.py`

- [x] **Step 1: Add failing runtime service tests**

Add tests proving `StrategyService.get_runtime_state(symbol='NVDA.US')`, `RuntimeStateService.persist_symbol()`, and `query_history(symbol='NVDA.US')` are symbol scoped.

- [x] **Step 2: Add failing database migration test**

Add a legacy SQLite DB test that creates `runtime_state` / `runtime_state_snapshots` without `symbol`, runs `database.init_db()`, and asserts both tables gain `symbol`.

- [x] **Step 3: Add failing runner test**

Add a runner test proving `_sync_symbol_runtimes()` loads persisted secondary runtime engine state.

- [x] **Step 4: Run RED tests**

Run focused tests and confirm failures are from missing symbol-aware persistence.

---

## Task 2: Symbol persistence implementation

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/database.py`
- Modify: `backend/app/services/strategy_service.py`
- Modify: `backend/app/services/runtime_state_service.py`

- [x] **Step 1: Add model fields**

Add `symbol: Mapped[str] = mapped_column(String(50), default='', index=True)` to `RuntimeState` and `RuntimeStateSnapshot`.

- [x] **Step 2: Add migration functions**

Add `_ensure_runtime_state_symbol_columns(engine)` and call it from `init_db()` after runtime daily PnL migration.

- [x] **Step 3: Scope strategy runtime rows**

Change `get_runtime_state(symbol: str = '')` to filter by symbol and create a row for that symbol. Change `update_runtime_state(symbol: str = '', **kwargs)` to update the scoped row.

- [x] **Step 4: Add runtime service symbol helpers**

Add `load_symbol_runtime(db, engine, symbol)`, `persist_symbol(db, engine, symbol=None)`, and symbol args on `record_snapshot()` / `query_history()`.

---

## Task 3: Runner wiring and verification

**Files:**
- Modify: `backend/app/runner.py`
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Load symbol runtime state**

After creating/reusing a secondary `SymbolRuntime`, call `RuntimeStateService.load_symbol_runtime()` for non-primary runtimes.

- [x] **Step 2: Persist secondary runtimes**

In the run-loop DB persist block, persist primary via existing `persist()` and persist each non-primary runtime via `persist_symbol()`.

- [x] **Step 3: Run verification**

Run runtime service tests, database tests, runner focused tests, full backend pytest, and basedpyright.

- [x] **Step 4: Update roadmap**

Mark P28 Wave 3 complete and set next iteration to P28 Wave 4 multi-symbol auto-trading enablement.

---

## Self-review

### Spec coverage

- RuntimeState symbol dimension: covered by model/service/migration changes.
- RuntimeStateSnapshot symbol dimension: covered by snapshot model/service/history tests.
- Runner multi-symbol restart skeleton: covered by secondary runtime load/persist wiring.
- Multi-symbol auto trading remains disabled: no order trigger policy changes beyond existing primary quote guard.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。

### Type consistency

- Runtime row key: `symbol: str`, default `''`.
- Service API: `get_runtime_state(symbol='')`, `update_runtime_state(symbol='', **kwargs)`, `persist_symbol(db, engine, symbol=None)`, `load_symbol_runtime(db, engine, symbol)`.
