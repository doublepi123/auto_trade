# P14: Buying Power Order Sizing Implementation Plan

> **状态：✅ 已交付 2026-05-31**（commit `0780f8a` + `77112c7`，合并入 `f1600db`）
>
> 实际实现路径与本计划的 "Current State Analysis" 描述完全一致：`BrokerGateway.estimate_margin_max_quantity()` wrapper 已存在并包装 `TradeContext.estimate_max_purchase_quantity()`；本计划新增加了 `margin_safety_factor` 配置化层（0~1，默认 0.9），由 `_entry_quantity_from_margin_power` 叠加。完整交付摘要见 [主 Roadmap.md](../../Roadmap.md) 中 P14 行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace cash-based order sizing with broker-estimated margin buying power, so entry orders use the full margin capacity instead of available cash only.

**Architecture:** `BrokerGateway.estimate_margin_max_quantity()` already exists and wraps `TradeContext.estimate_max_purchase_quantity()`. `TradeExecutionService._entry_quantity_from_margin_power()` already calls it. This plan verifies the existing wiring is correct, adds a configurable safety factor, and ensures comprehensive test coverage.

**Tech Stack:** Python 3.11, pytest

**Baseline:** `pytest 621 passed`, `basedpyright` 0 errors / 0 warnings

---

## Current State Analysis

The code already has the margin-based sizing implemented:

1. **`BrokerGateway.estimate_margin_max_quantity()`** — calls `estimate_max_purchase_quantity` and returns `margin_max_qty` as `Decimal`.
2. **`TradeExecutionService._entry_quantity_from_margin_power()`** — calls `broker.estimate_margin_max_quantity()` and applies `ENTRY_BUYING_POWER_USAGE = Decimal("0.9")`.
3. **`TradeExecutionService._execute_buy()`** — calls `_entry_quantity_from_margin_power`.
4. **`TradeExecutionService._execute_sell_short()`** — calls `_entry_quantity_from_margin_power`.
5. **`TradeExecutionService._execute_sell()`** — uses `_exit_quantity_from_position` (position-based, unchanged).
6. **`TradeExecutionService._execute_buy_to_cover()`** — uses `_exit_quantity_from_position` (position-based, unchanged).

The implementation is already wired. This plan focuses on:
- Making the safety factor configurable via `StrategyConfig`
- Adding comprehensive tests for the margin sizing path
- Verifying edge cases (zero estimate, broker failure)

---

## File Structure

### Backend — Modified Files

| File | Changes |
|------|---------|
| `backend/app/models.py` | Add `margin_safety_factor` column to `StrategyConfig` |
| `backend/app/database.py` | Add `_ensure_strategy_config_margin_safety_factor` migration |
| `backend/app/schemas.py` | Add `margin_safety_factor` to strategy schemas |
| `backend/app/services/trade_execution_service.py` | Use configurable safety factor |

### Backend — Test Files

| File | Changes |
|------|---------|
| `backend/tests/test_trade_execution_service.py` | Add margin sizing tests |

### Frontend — Modified Files

| File | Changes |
|------|---------|
| `frontend/src/views/Strategy.vue` | Add margin safety factor field |
| `frontend/src/types/index.ts` | Add type field |

---

## Task 1: Write Failing Tests for Margin Sizing

**Files:**
- Create test cases in: `backend/tests/test_trade_execution_service.py`

- [ ] **Step 1: Add margin sizing tests**

```python
# Add to test_trade_execution_service.py

def test_execute_buy_uses_margin_max_quantity():
    """BUY uses margin-based sizing: floor(margin_max_qty * safety_factor)."""
    import os
    os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_margin_buy.db"
    from decimal import Decimal
    from unittest.mock import MagicMock, patch
    from app.services.trade_execution_service import TradeExecutionService

    broker = MagicMock()
    broker.estimate_margin_max_quantity.return_value = Decimal("100")
    broker.submit_limit_order.return_value = MagicMock(
        broker_order_id="ord-1",
        symbol="AAPL.US",
        side="BUY",
        quantity=Decimal("90"),
        price=Decimal("150.00"),
        status="FILLED",
    )

    svc = TradeExecutionService(
        record_order=MagicMock(),
        update_order_status=MagicMock(),
        record_risk_event=MagicMock(),
    )
    quote = MagicMock()
    quote.last_price = 150.0
    risk = MagicMock()
    risk.check.return_value = MagicMock(approved=True)

    result = svc._execute_buy(
        "AAPL.US", quote, broker, risk, MagicMock(), "USD",
    )
    # margin_max_qty=100, safety=0.9 → qty=90
    broker.estimate_margin_max_quantity.assert_called_once_with(
        "AAPL.US", "BUY", Decimal("150.00"), "USD",
    )
    assert result is not None


def test_execute_buy_skips_when_margin_estimate_zero():
    """BUY skips order when margin estimate returns zero."""
    from decimal import Decimal
    from unittest.mock import MagicMock
    from app.services.trade_execution_service import TradeExecutionService

    broker = MagicMock()
    broker.estimate_margin_max_quantity.return_value = Decimal("0")

    svc = TradeExecutionService(
        record_order=MagicMock(),
        update_order_status=MagicMock(),
        record_risk_event=MagicMock(),
    )
    quote = MagicMock()
    quote.last_price = 150.0

    result = svc._execute_buy(
        "AAPL.US", quote, broker, MagicMock(), MagicMock(), "USD",
    )
    assert result is None


def test_execute_sell_uses_position_quantity_not_margin():
    """SELL uses actual position quantity, not margin estimate."""
    from decimal import Decimal
    from unittest.mock import MagicMock
    from app.services.trade_execution_service import TradeExecutionService

    broker = MagicMock()
    broker.get_positions.return_value = [
        MagicMock(symbol="AAPL.US", side="LONG", quantity=Decimal("50"),
                  avg_price=Decimal("145.00"), available_quantity=Decimal("50")),
    ]
    broker.submit_limit_order.return_value = MagicMock(
        broker_order_id="ord-2",
        symbol="AAPL.US",
        side="SELL",
        quantity=Decimal("50"),
        price=Decimal("155.00"),
        status="FILLED",
    )

    svc = TradeExecutionService(
        record_order=MagicMock(),
        update_order_status=MagicMock(),
        record_risk_event=MagicMock(),
    )
    quote = MagicMock()
    quote.last_price = 155.0
    risk = MagicMock()
    risk.check.return_value = MagicMock(approved=True)

    result = svc._execute_sell(
        "AAPL.US", quote, broker, risk, MagicMock(),
        min_profit_amount=0, allow_loss_exit=True, fee_rate=0,
    )
    # SELL should NOT call estimate_margin_max_quantity
    broker.estimate_margin_max_quantity.assert_not_called()
    assert result is not None


def test_execute_sell_short_uses_margin_sizing():
    """SELL_SHORT uses margin-based sizing like BUY."""
    from decimal import Decimal
    from unittest.mock import MagicMock
    from app.services.trade_execution_service import TradeExecutionService

    broker = MagicMock()
    broker.estimate_margin_max_quantity.return_value = Decimal("80")
    broker.submit_limit_order.return_value = MagicMock(
        broker_order_id="ord-3",
        symbol="AAPL.US",
        side="SELL",
        quantity=Decimal("72"),
        price=Decimal("155.00"),
        status="FILLED",
    )

    svc = TradeExecutionService(
        record_order=MagicMock(),
        update_order_status=MagicMock(),
        record_risk_event=MagicMock(),
    )
    quote = MagicMock()
    quote.last_price = 155.0
    risk = MagicMock()
    risk.check.return_value = MagicMock(approved=True)

    result = svc._execute_sell_short(
        "AAPL.US", quote, broker, risk, MagicMock(), "USD",
    )
    broker.estimate_margin_max_quantity.assert_called_once()
    assert result is not None
```

- [ ] **Step 2: Run tests to see current state**

Run: `cd backend && python -m pytest tests/test_trade_execution_service.py -v -k "margin or sell_uses_position"`
Expected: These tests should PASS since the implementation already exists. They serve as regression tests.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_trade_execution_service.py
git commit -m "test(trade): add regression tests for margin-based order sizing"
```

---

## Task 2: Configurable Safety Factor

**Files:**
- Modify: `backend/app/models.py` — add `margin_safety_factor` column
- Modify: `backend/app/database.py` — add migration patch
- Modify: `backend/app/schemas.py` — add to API schema
- Modify: `backend/app/services/trade_execution_service.py` — read from config
- Modify: `backend/tests/test_trade_execution_service.py` — test configurable factor

- [ ] **Step 1: Add `margin_safety_factor` to `StrategyConfig` model**

In `backend/app/models.py`, add to `StrategyConfig` class (after the existing `fee_rate_us` or similar fields):

```python
margin_safety_factor: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.9)
```

- [ ] **Step 2: Add database migration patch in `database.py`**

Add a new `_ensure_strategy_config_margin_safety_factor` function and call it from `init_db()`:

```python
def _ensure_strategy_config_margin_safety_factor() -> None:
    """Add margin_safety_factor column to strategy_config if missing."""
    _add_column_if_missing("strategy_config", "margin_safety_factor", "FLOAT")
```

Call it in `init_db()` after the existing `_ensure_*` calls.

- [ ] **Step 3: Add to schemas**

In `backend/app/schemas.py`, add `margin_safety_factor: float | None = 0.9` to the strategy update/create schemas.

- [ ] **Step 4: Use configurable factor in TradeExecutionService**

Update `_entry_quantity_from_margin_power` to accept an optional safety factor parameter:

```python
def _entry_quantity_from_margin_power(
    self,
    broker: BrokerGateway,
    symbol: str,
    side: str,
    price: Decimal,
    cash_currency: str,
    *,
    safety_factor: float | None = None,
) -> int:
    max_qty = broker.estimate_margin_max_quantity(symbol, side, price, cash_currency)
    factor = Decimal(str(safety_factor)) if safety_factor is not None else ENTRY_BUYING_POWER_USAGE
    qty = int(max_qty * factor)
    if qty <= 0:
        logger.warning(
            "%s: qty <= 0, margin_max_qty=%s price=%s currency=%s factor=%s",
            side, max_qty, price, cash_currency, factor,
        )
    return qty
```

Update callers in `_execute_buy` and `_execute_sell_short` to pass the safety factor when available.

- [ ] **Step 5: Run tests**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 6: Run basedpyright**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
git add backend/app/models.py backend/app/database.py backend/app/schemas.py backend/app/services/trade_execution_service.py backend/tests/test_trade_execution_service.py
git commit -m "feat(trade): add configurable margin safety factor for order sizing"
```

---

## Task 3: Frontend — Margin Safety Factor Field

**Files:**
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/types/index.ts`

- [ ] **Step 1: Add `marginSafetyFactor` to TypeScript types**

In `frontend/src/types/index.ts`, add to the strategy config interface:

```typescript
marginSafetyFactor?: number | null
```

- [ ] **Step 2: Add field to Strategy form**

In `frontend/src/views/Strategy.vue`, add an input field in the execution protection section:

```html
<el-form-item label="保证金安全系数" prop="marginSafetyFactor">
  <el-input-number
    v-model="form.marginSafetyFactor"
    :min="0.1"
    :max="1.0"
    :step="0.05"
    :precision="2"
  />
  <div class="form-help">下单量 = 券商保证金最大购买量 × 安全系数（默认 0.9）</div>
</el-form-item>
```

- [ ] **Step 3: Run frontend type check and build**

Run: `cd frontend && npm run type-check && npm run build`
Expected: Both pass

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/views/Strategy.vue
git commit -m "feat(strategy): add margin safety factor field to strategy form"
```

---

## Task 4: Full Verification

- [ ] **Step 1: Run all backend tests**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 2: Run basedpyright**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 3: Run frontend type check and build**

Run: `cd frontend && npm run type-check && npm run build`
Expected: Both pass

---

## Summary

| Task | Scope | New Tests | Risk |
|------|-------|-----------|------|
| T1 | Margin sizing regression tests | ~4 | Test-only |
| T2 | Configurable safety factor | ~2 | Low — additive change |
| T3 | Frontend field | 0 | Low — form field |
| T4 | Verification | 0 | — |

**Total new tests:** ~6

**Key insight:** The margin-based sizing is already implemented and active. This plan adds configurable safety factor and comprehensive test coverage rather than building from scratch.
