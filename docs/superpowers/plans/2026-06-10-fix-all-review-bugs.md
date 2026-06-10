# Fix All Code Review Bugs (2026-06-10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 20 bugs identified in the full code review (docs/reviews/2026-06-10-full-code-review.md).

**Architecture:** Fixes are grouped into 5 batches by dependency and complexity. Batches 1-3 are independent fixes that can be parallelized. Batch 4 (pending order lifecycle) involves interrelated fixes in broker.py, runner.py, and trade_execution_service.py. Batch 5 covers risk/sync fixes.

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / Vue 3 / TypeScript

---

## Batch 1: Simple Fixes (independent, parallelizable)

### Task 1.1: #11 — Route order fix (strategy_experiments.py)

**Files:**
- Modify: `backend/app/api/strategy_experiments.py:125`

Move the `/llm-evaluations` route registration BEFORE `/{experiment_id}` to prevent the int path param from matching "llm-evaluations".

- [ ] Move `@router.get("/llm-evaluations", ...)` handler above `@router.get("/{experiment_id}", ...)` handler
- [ ] Verify with `basedpyright`

### Task 1.2: #14 — Move trigger price update after analysis (main.py)

**Files:**
- Modify: `backend/app/main.py:249-253`

Move `_last_llm_trigger_price` update to AFTER `advisor.analyze()` returns, only on success.

- [ ] Cut the `with _llm_globals_lock:` block at lines 249-253
- [ ] Paste it inside the `if result.get("success"):` block, after `analyzed_count += 1`
- [ ] Verify with `basedpyright`

### Task 1.3: #19 — Move UPDATE inside column-missing branch (database.py)

**Files:**
- Modify: `backend/app/database.py:157-159`

Move the `UPDATE runtime_state SET daily_pnl = 0 ...` statement inside the `if "daily_pnl_date" not in columns:` branch.

- [ ] Indent line 157-159 to be inside the `if "daily_pnl_date" not in columns:` block (after line 148)
- [ ] Verify with `basedpyright`

### Task 1.4: #20 — Preview throttle cold start guard (llm_advisor_service.py)

**Files:**
- Modify: `backend/app/services/llm_advisor_service.py:391`

Add `> 0` guard matching the analysis path pattern.

- [ ] Change `if time.monotonic() - _LAST_PREVIEW_TIMESTAMP < _PREVIEW_THROTTLE_SECONDS:` to `if _LAST_PREVIEW_TIMESTAMP > 0 and time.monotonic() - _LAST_PREVIEW_TIMESTAMP < _PREVIEW_THROTTLE_SECONDS:`
- [ ] Verify with `basedpyright`

---

## Batch 2: Frontend Fixes (independent, parallelizable)

### Task 2.1: #15 — Notification reads wrong fields (useNotificationStream.ts)

**Files:**
- Modify: `frontend/src/composables/useNotificationStream.ts:134-135, 197-198`

Change `item.detail`/`item.action` to `item.message`/`item.payload`. Use `event_type` for title.

- [ ] In `handleEvent()`: change `evt.action` → `evt.event_type` for title
- [ ] In `handleEvent()`: change `detailHash(evt.detail)` → stringified message from `evt.detail` (keep working with detail field for WS events)
- [ ] In `processEvents()`: change title to use `item.event_type`
- [ ] In `processEvents()`: change message to use `item.message ?? detailHash(item.detail)`
- [ ] Verify with `vue-tsc`

### Task 2.2: #16 — Notification dedup collision across tables (useNotificationStream.ts)

**Files:**
- Modify: `frontend/src/composables/useNotificationStream.ts:127, 182-183`

Change dedup key from bare `id` to `${source}:${id}`.

- [ ] Change `sharedKnownEventIds` from `Map<number, number>` to `Map<string, number>`
- [ ] Change dedup key construction from `item.id` to `${item.source ?? 'trade'}:${item.id}`
- [ ] Verify with `vue-tsc`

---

## Batch 3: Moderate Backend Fixes (independent, parallelizable)

### Task 3.1: #7 — GET /api/status uses UTC day for PnL (strategy.py)

**Files:**
- Modify: `backend/app/api/strategy.py:114`

Pass `trade_day` and `to_trade_day` using `trade_day_for(config.market)`.

- [ ] Import `trade_day_for` from `app.core.market_calendar`
- [ ] Change `DailyPnlService(db).calculate()` to `DailyPnlService(db).calculate(trade_day=trade_day_for(config.market), to_trade_day=lambda instant=None: trade_day_for(config.market, instant))`
- [ ] Verify with `basedpyright`

### Task 3.2: #9 — confidence_score=None TypeError + cron break (interval_application_service.py + main.py + llm_advisor.py)

**Files:**
- Modify: `backend/app/services/interval_application_service.py:93`
- Modify: `backend/app/main.py:358`
- Modify: `backend/app/api/llm_advisor.py:248`

- [ ] In `interval_application_service.py:93`: change `confidence = suggestion.get("confidence_score", 0.0)` to `confidence = suggestion.get("confidence_score") or 0.0`
- [ ] In `main.py:358`: change `break` to `continue` so remaining symbols aren't skipped
- [ ] In `llm_advisor.py:248`: apply same `or 0.0` guard to `confidence_score`
- [ ] Verify with `basedpyright`

### Task 3.3: #12 — Push quote bid/ask always 0 (broker.py + runner.py)

**Files:**
- Modify: `backend/app/runner.py:998`

Change `_evaluate_quote_quality` to only require `last_price > 0` for push quotes, skip bid/ask requirement.

- [ ] In `_evaluate_quote_quality`: change `price_positive` to `last_price > 0` (not requiring bid/ask > 0)
- [ ] Only check spread when both bid > 0 and ask > 0
- [ ] Verify with `basedpyright`

### Task 3.4: #17 — Trade event severity hardcoded None (event_list_service.py)

**Files:**
- Modify: `backend/app/services/event_list_service.py:43`

Add severity mapping for risk-related event types.

- [ ] Add a `_trade_event_severity` mapping function: RISK_PAUSED → WARNING, ORDER_REJECTED → WARNING, KILL_SWITCH → CRITICAL, ORDER_TIMEOUT → CRITICAL, etc.
- [ ] Use it in `_trade_row_to_out` instead of hardcoded `severity=None`
- [ ] Verify with `basedpyright`

### Task 3.5: #18 — skip_category filter not applied to audit + deep pagination (event_list_service.py)

**Files:**
- Modify: `backend/app/services/event_list_service.py:137-155`

- [ ] When `skip_category` is set and source == "all", skip audit query entirely (return empty audit rows + 0 audit_total)
- [ ] Clamp total to `len(merged)` when merged set is smaller than `(page - 1) * page_size + page_size`
- [ ] Verify with `basedpyright`

---

## Batch 4: Pending Order Lifecycle (interrelated, sequential within batch)

### Task 4.1: #1 — Remove blind retry for submit_limit_order (broker.py)

**Files:**
- Modify: `backend/app/core/broker.py:578-584`

Remove `_call_with_retry` wrapper from `submit_limit_order`. Order submission must not be blindly retried.

- [ ] Change `submit_limit_order` to call `_submit_limit_order_inner` directly without retry
- [ ] Add logging for submission failures
- [ ] Verify with `basedpyright`

### Task 4.2: #2 — _PendingOrder carries restore target (trade_execution_service.py + runner.py)

**Files:**
- Modify: `backend/app/services/trade_execution_service.py:73-83` (_PendingOrder dataclass)
- Modify: `backend/app/runner.py:609, 764, 1289`

Add `restore_engine_snapshot_fn` to `_PendingOrder` so each pending knows its own restore callback.

- [ ] Add `restore_engine_snapshot_fn: Callable[[EngineSnapshot], None] | None = None` field to `_PendingOrder`
- [ ] Update `_track_pending_order` to accept and store the restore callback
- [ ] Update `_reconcile_pending_order` and `_handle_pending_order_timeout` to use `pending.restore_engine_snapshot_fn` when available
- [ ] Update runner.py callers to pass the correct engine's restore function
- [ ] Verify with `basedpyright`

### Task 4.3: #3 — load_pending_orders finalizes state-flipped entries (runner.py + trade_execution_service.py)

**Files:**
- Modify: `backend/app/runner.py:1615-1658` (`_load_pending_orders`)
- Modify: `backend/app/services/trade_execution_service.py:134-163` (`load_pending_orders`)

Before clearing in-memory pending, check if any existing pending orders have transitioned to terminal state in DB and finalize them.

- [ ] In `load_pending_orders`: before clearing, check each existing in-memory pending against DB status
- [ ] If DB status is terminal and memory still has it, call `_finalize_pending_fill` for FILLED or restore snapshot for REJECTED/CANCELLED
- [ ] Verify with `basedpyright`

### Task 4.4: #4 — Timeout attempts cancel_order (trade_execution_service.py)

**Files:**
- Modify: `backend/app/services/trade_execution_service.py:894-949` (`_handle_pending_order_timeout`)

Add `cancel_order` attempt in the timeout path when order is still live.

- [ ] After the status query in `_handle_pending_order_timeout`, if order is still live (SUBMITTED/PARTIAL_FILLED), attempt `cancel_order`
- [ ] If cancel succeeds, process any partial fill
- [ ] If cancel fails with "already filled", process the fill instead of clearing
- [ ] Verify with `basedpyright`

### Task 4.5: #5 — Per-order reconcile in-flight guard (trade_execution_service.py)

**Files:**
- Modify: `backend/app/services/trade_execution_service.py:202-225` (`reconcile`)

Add `_reconcile_in_flight: set[str]` to prevent two threads from reconciling the same order simultaneously.

- [ ] Add `_reconcile_in_flight: set[str]` field to `TradeExecutionService`
- [ ] In `reconcile`, set/clear the guard per order_id around the reconcile call
- [ ] Skip orders already being reconciled by another thread
- [ ] Verify with `basedpyright`

### Task 4.6: #10 — Cancel handles partial fills (trade_execution_service.py)

**Files:**
- Modify: `backend/app/services/trade_execution_service.py:241-266` (`cancel_pending_order_for_symbol`)

Check `executed_quantity > 0` in cancel result and finalize partial fills.

- [ ] After cancel, check if `order_status.executed_quantity > 0`
- [ ] If so, call `_finalize_pending_fill` before clearing
- [ ] Verify with `basedpyright`

---

## Batch 5: Risk/Sync Fixes

### Task 5.1: #6 — Ledger replay clears real losses (runner.py)

**Files:**
- Modify: `backend/app/runner.py:1105-1141` (`_sync_risk_from_order_ledger`)

Don't overwrite risk state with more optimistic values. Only update if the new values are worse (more losses).

- [ ] Only update `daily_pnl` if new value is <= current (more negative or equal)
- [ ] Only update `consecutive_losses` if new value >= current
- [ ] Log a warning when rejecting an optimistic overwrite
- [ ] Verify with `basedpyright`

### Task 5.2: #8 — Position sync races with fill (runner.py)

**Files:**
- Modify: `backend/app/runner.py:1350-1395` (`_sync_engine_state_with_positions`)

Add a `_last_fill_at` timestamp guard: skip sync if a fill was processed more recently than the position snapshot.

- [ ] Add `_last_fill_at: float = 0.0` field to `AppRunner`
- [ ] Update `_last_fill_at` after any successful fill (in `_on_quote` flow)
- [ ] In `_sync_engine_state_with_positions`: record `snapshot_time = time.monotonic()` before `get_positions()`, then in lock check `snapshot_time < self._last_fill_at` and skip if true
- [ ] Verify with `basedpyright`

---

## Execution Order

1. Batches 1 + 2 + 3 can run **in parallel** (independent files)
2. Batch 4 must be **sequential** (shared files, interrelated fixes)
3. Batch 5 can run **after** Batch 4 (touches runner.py which Batch 4 also modifies)
4. Final: run full test suite
