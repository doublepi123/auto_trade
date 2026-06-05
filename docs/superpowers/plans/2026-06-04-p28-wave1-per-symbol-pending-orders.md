# P28 Wave 1 Per-symbol Pending Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single pending-order slot in `TradeExecutionService` with per-symbol pending order storage while preserving current single-symbol behavior.

**Architecture:** Keep `TradeExecutionService` as a single service instance, but store live pending orders in `dict[str, _PendingOrder]` keyed by symbol. Existing `has_pending_order`, `pending_order`, `cancel_pending_order()`, and `reconcile()` remain backwards-compatible by operating on the first pending order; new symbol-aware helpers make P28 multi-symbol runner work possible in later waves. `execute()` blocks only when the same symbol already has a pending order.

**Tech Stack:** Python 3.11, pytest, basedpyright, existing inline fake/MagicMock patterns in `backend/tests/test_trade_execution_service.py`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/services/trade_execution_service.py` | Convert pending order state from single slot to symbol-keyed dict and add symbol-aware accessors. | Modify |
| `backend/tests/test_trade_execution_service.py` | Add regression tests for cross-symbol pending coexistence and symbol-specific cancellation. | Modify |
| `docs/Roadmap.md` | Mark P28 Wave 1 complete and update next iteration plan. | Modify |

---

## Task 1: Per-symbol pending state tests

**Files:**
- Modify: `backend/tests/test_trade_execution_service.py`

- [x] **Step 1: Add tests before implementation**

Add these tests inside `class TestTradeExecutionServiceBasics` near existing pending-order tests:

```python
    def test_pending_orders_are_tracked_per_symbol(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            broker,
            None,
        )
        svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "SUBMITTED"),
            broker,
            None,
        )

        assert svc.has_pending_order is True
        assert svc.pending_order is not None
        assert svc.pending_order.broker_order_id == "order-nvda"
        assert svc.pending_order_for("NVDA.US") is not None
        assert svc.pending_order_for("NVDA.US").broker_order_id == "order-nvda"
        assert svc.pending_order_for("AAPL.US") is not None
        assert svc.pending_order_for("AAPL.US").broker_order_id == "order-aapl"

    def test_execute_allows_different_symbol_when_another_symbol_is_pending(self) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        pending_broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            pending_broker,
            None,
        )
        broker = MagicMock()
        broker.get_cash.return_value = {"USD": Decimal("10000")}
        broker.submit_limit_order.return_value = OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "FILLED")

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 199, 198.9, 199.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "FILLED"
        assert skipped == []
        broker.submit_limit_order.assert_called_once()

    def test_cancel_pending_order_for_symbol_leaves_other_symbols_pending(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, OrderStatusResult

        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult("order-aapl", "CANCELLED")
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            broker,
            None,
        )
        svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "SUBMITTED"),
            broker,
            None,
        )

        result = svc.cancel_pending_order_for_symbol("AAPL.US")

        assert result.status == "CANCELLED"
        assert svc.pending_order_for("AAPL.US") is None
        assert svc.pending_order_for("NVDA.US") is not None
        broker.cancel_order.assert_called_once_with("order-aapl")
```

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_pending_orders_are_tracked_per_symbol tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_allows_different_symbol_when_another_symbol_is_pending tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_cancel_pending_order_for_symbol_leaves_other_symbols_pending -v
```

Expected: fail because `pending_order_for()` and `cancel_pending_order_for_symbol()` do not exist, and same-service pending guard is still global.

---

## Task 2: Per-symbol pending implementation

**Files:**
- Modify: `backend/app/services/trade_execution_service.py`

- [x] **Step 1: Replace state storage**

Change constructor state from:

```python
self._pending_order: _PendingOrder | None = None
```

to:

```python
self._pending_orders: dict[str, _PendingOrder] = {}
```

- [x] **Step 2: Preserve backwards-compatible accessors**

Replace `has_pending_order` and `pending_order` with:

```python
    @property
    def has_pending_order(self) -> bool:
        with self._state_lock:
            return bool(self._pending_orders)

    @property
    def pending_order(self) -> _PendingOrder | None:
        with self._state_lock:
            return next(iter(self._pending_orders.values()), None)

    def pending_order_for(self, symbol: str) -> _PendingOrder | None:
        with self._state_lock:
            return self._pending_orders.get(symbol)
```

- [x] **Step 3: Make reconciliation iterate all pending orders**

Change `reconcile()` local pending fetch to:

```python
        with self._state_lock:
            pending_orders = list(self._pending_orders.values())
        for pending in pending_orders:
            self._reconcile_pending_order(
                pending,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )
```

- [x] **Step 4: Add symbol-specific cancellation and keep legacy cancellation**

Replace `cancel_pending_order()` body with a wrapper around a new method:

```python
    def cancel_pending_order(
        self,
        *,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = next(iter(self._pending_orders.values()), None)
        if pending is None:
            return OrderStatus("", "NO_PENDING_ORDER")
        return self.cancel_pending_order_for_symbol(
            pending.symbol,
            restore_engine_snapshot=restore_engine_snapshot,
        )

    def cancel_pending_order_for_symbol(
        self,
        symbol: str,
        *,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = self._pending_orders.get(symbol)
        if pending is None:
            return OrderStatus("", "NO_PENDING_ORDER")

        try:
            order_status = self._coerce_order_status(
                pending.broker.cancel_order(pending.broker_order_id),
                pending.broker_order_id,
            )
        except Exception:
            logger.exception("failed to cancel pending order %s", pending.broker_order_id)
            return OrderStatus(pending.broker_order_id, "CANCEL_FAILED")

        self._safe_update_order_status_from_result(order_status)
        self._clear_pending_order(pending.broker_order_id)
        if restore_engine_snapshot is not None and pending.engine_snapshot is not None:
            restore_engine_snapshot(pending.engine_snapshot)
        logger.info("pending order cancelled: %s status=%s", pending.broker_order_id, order_status.status)
        return order_status
```

- [x] **Step 5: Make `cancel_order_by_id()` search all pending orders**

Replace the pending lookup with:

```python
        with self._state_lock:
            pending = next(
                (item for item in self._pending_orders.values() if item.broker_order_id == order_id),
                None,
            )
        if pending is not None:
            return self.cancel_pending_order_for_symbol(
                pending.symbol,
                restore_engine_snapshot=restore_engine_snapshot,
            )
```

- [x] **Step 6: Make execute guard per-symbol**

Replace the pending guard with:

```python
        with self._state_lock:
            pending = self._pending_orders.get(symbol)
            if pending is not None:
                logger.warning("execute skipped: pending order %s still live for %s", pending.broker_order_id, symbol)
                return self._skip_order(symbol, action, "pending order in flight", skip_category="PENDING")
```

- [x] **Step 7: Update track and clear helpers**

Replace `_track_pending_order()` assignment with:

```python
        with self._state_lock:
            self._pending_orders[pending.symbol] = pending
```

Replace `_clear_pending_order()` with:

```python
    def _clear_pending_order(self, order_id: str) -> None:
        with self._state_lock:
            for symbol, pending in list(self._pending_orders.items()):
                if pending.broker_order_id == order_id:
                    del self._pending_orders[symbol]
                    return
```

Replace `_reconcile_pending_order()`'s updated pending assignment with:

```python
        with self._state_lock:
            self._pending_orders[updated_pending.symbol] = updated_pending
```

- [x] **Step 8: Run focused tests and verify GREEN**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_pending_orders_are_tracked_per_symbol tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_allows_different_symbol_when_another_symbol_is_pending tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_cancel_pending_order_for_symbol_leaves_other_symbols_pending tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_pending_rejection_records_pending_skip_category tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_cancel_pending_order_calls_broker_and_restores_snapshot tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_cancel_pending_order_returns_no_pending_when_empty -v
```

Expected: 6 passed.

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run trade execution service tests**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_trade_execution_service.py -v
```

Expected: all tests in `test_trade_execution_service.py` pass.

- [x] **Step 2: Run backend type-check**

Run:

```bash
cd backend && ./.venv/bin/basedpyright
```

Expected: 0 errors / 0 warnings / 0 notes.

- [x] **Step 3: Update roadmap**

Add a completed row for P28 Wave 1 and shift next iteration plan to P28 Wave 2:

```markdown
| 已完成 | **P28 Wave 1：pending order 按 symbol 隔离** | 已完成 2026-06-04 | `TradeExecutionService` pending order 从单槽改为 symbol-keyed dict；保留旧单标的访问器与取消语义；新增 `pending_order_for()` / `cancel_pending_order_for_symbol()`；同一 symbol 仍阻止重复下单，不同 symbol 可并存 pending，为后续 runner 多 symbol 状态隔离打基础。验证：`test_trade_execution_service.py` passed / basedpyright 0/0/0。 |
```

---

## Self-review

### Spec coverage

- P26 要求 pending order 按 symbol 隔离：本计划完成 `TradeExecutionService` 层隔离。
- P26 要求全局风控保持组合级：本计划不修改 `RiskController`。
- P28 全量多标的交易尚未完成：本计划只完成 Wave 1 基础设施，不启用 runner 多标的自动交易。

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
- 所有代码步骤包含具体代码块。
- 所有验证步骤包含精确命令和预期。

### Type consistency

- New accessor: `pending_order_for(symbol: str) -> _PendingOrder | None`。
- New cancellation method: `cancel_pending_order_for_symbol(symbol: str, *, restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None) -> OrderStatus`。
- Internal state: `_pending_orders: dict[str, _PendingOrder]`。
