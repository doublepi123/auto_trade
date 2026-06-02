# P13: Add-On Buy + Cost-Anchored LLM Implementation Plan

> **状态：✅ 已交付 2026-05-31**（commit `a9e3ce5` + `d63ddd3` + `c14a9f9` + `89c65ef` + `a60ae0d`，合并入 `f1600db`）
>
> 本计划与实际交付一致，无需偏差修正。完整交付摘要见 [主 Roadmap.md](../../Roadmap.md) 中 P13 行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the strategy engine to trigger additional BUY orders while already LONG (add-on buy), and inject real position context into LLM prompts so recommendations are cost-aware instead of price-chasing.

**Architecture:** Three changes: (1) Engine state machine gets a new LONG→BUY transition that stays LONG, (2) LLM advisor auto-fetches tracked entry data to populate prompt context, (3) Interval application gets an anti-chasing guard for buy_low in LONG state. The existing `_execute_buy`, `_entry_quantity_from_margin_power`, and `_record_entry_price` already handle multiple entries correctly — no trade execution changes needed.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest

**Baseline:** `pytest 621 passed`, `basedpyright` 0 errors / 0 warnings

---

## File Structure

### Backend — Modified Files

| File | Changes |
|------|---------|
| `backend/app/core/engine.py` | Add `price <= buy_low` → `BUY` transition in LONG state |
| `backend/app/domain/prompt/context_module.py` | Add position cost section rendering |
| `backend/app/api/llm_advisor.py` | `_position_context()` prefers tracked entries over broker positions |

### Backend — Test Files

| File | Changes |
|------|---------|
| `backend/tests/test_engine.py` | Add ~6 tests for add-on buy behavior |
| `backend/tests/test_interval_application.py` | Add ~3 tests for anti-chasing guard |
| `backend/tests/test_context_module.py` | Add ~3 tests for position cost rendering |

### Files NOT Changed

| File | Reason |
|------|--------|
| `backend/app/services/trade_execution_service.py` | Already handles multiple buys via `_record_entry_price` weighted average |
| `backend/app/services/llm_advisor_service.py` | Position context is injected by callers (`_position_context` in API layer) |
| `backend/app/services/interval_application_service.py` | `_apply_long` already has `new_buy_low <= old_buy_low` guard (P7'/P13 design alignment) |
| `backend/app/core/risk.py` | Existing risk gates apply automatically to all orders |

---

## Verification Commands

| What | Command |
|------|---------|
| Engine tests | `cd backend && python -m pytest tests/test_engine.py -v` |
| Interval application tests | `cd backend && python -m pytest tests/test_interval_application.py -v` |
| Context module tests | `cd backend && python -m pytest tests/test_context_module.py -v` |
| All backend tests | `cd backend && python -m pytest tests/ -v --tb=short` |
| Type check | `cd backend && python -m basedpyright` |

---

## Task 1: Engine — Add-On Buy in LONG State

**Files:**
- Modify: `backend/app/core/engine.py` (lines ~72-80, the `elif self.state == EngineState.LONG` block)
- Modify: `backend/tests/test_engine.py` (update existing test + add new tests)

- [ ] **Step 1: Update the existing `test_price_below_buy_low_from_long_does_not_add_on_buy` to expect add-on buy**

The existing test asserts `triggered is False`. After the change, LONG + `price <= buy_low` should trigger `BUY` and remain `LONG`.

```python
# In test_engine.py, update the test:
def test_price_below_buy_low_from_long_triggers_add_on_buy(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG

    result = engine.update_price(99.0)

    assert result.triggered is True
    assert result.action == "BUY"
    assert engine.state == EngineState.LONG
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_engine.py::TestStrategyEngine::test_price_below_buy_low_from_long_triggers_add_on_buy -v`
Expected: FAIL — engine currently returns `triggered=False` in LONG state when `price <= buy_low`.

- [ ] **Step 3: Implement add-on buy in engine.py**

In `_update_price_locked`, change the `elif self.state == EngineState.LONG` block:

```python
elif self.state == EngineState.LONG:
    if price >= self.params.sell_high:
        self.state = EngineState.FLAT
        self._mark_trigger(price)
        return TriggerResult(
            triggered=True,
            action="SELL",
            description=f"Price {price} >= sell_high {self.params.sell_high}, sell LONG",
        )
    if price <= self.params.buy_low:
        self._mark_trigger(price)
        return TriggerResult(
            triggered=True,
            action="BUY",
            description=f"Price {price} <= buy_low {self.params.buy_low}, add-on buy LONG",
        )
```

- [ ] **Step 4: Run the updated test to verify it passes**

Run: `cd backend && python -m pytest tests/test_engine.py::TestStrategyEngine::test_price_below_buy_low_from_long_triggers_add_on_buy -v`
Expected: PASS

- [ ] **Step 5: Update `test_long_position_waits_for_sell_high_even_below_buy_low`**

This test's name and assertions need updating — it now expects add-on buy:

```python
def test_long_add_on_buy_triggers_at_buy_low(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG

    first = engine.update_price(99.0)

    assert first.triggered is True
    assert first.action == "BUY"
    assert engine.state == EngineState.LONG
```

- [ ] **Step 6: Add new tests for add-on buy edge cases**

```python
def test_add_on_buy_at_buy_low_boundary(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG
    result = engine.update_price(100.0)
    assert result.triggered is True
    assert result.action == "BUY"
    assert engine.state == EngineState.LONG

def test_sell_priority_over_add_on_buy_in_long(self) -> None:
    """SELL is evaluated first; with valid config buy_low < sell_high, both can't be true."""
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG
    # price 201 >= sell_high 200 → SELL, not add-on buy
    result = engine.update_price(201.0)
    assert result.triggered is True
    assert result.action == "SELL"
    assert engine.state == EngineState.FLAT

def test_cooldown_blocks_add_on_buy(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG
    engine._cooldown_seconds = 60
    # First add-on buy
    first = engine.update_price(99.0)
    assert first.triggered is True
    assert first.action == "BUY"
    assert engine.state == EngineState.LONG
    # Immediate second add-on buy blocked by cooldown
    second = engine.update_price(98.0)
    assert second.triggered is False
    assert engine.state == EngineState.LONG

def test_cooldown_after_add_on_buy_blocks_sell(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG
    engine._cooldown_seconds = 60
    # Add-on buy
    engine.update_price(99.0)
    # Immediate sell blocked by same cooldown
    result = engine.update_price(201.0)
    assert result.triggered is False
    assert engine.state == EngineState.LONG

def test_long_stays_long_after_multiple_add_on_buys(self) -> None:
    engine = StrategyEngine(make_params(100, 200))
    engine.state = EngineState.LONG
    engine._cooldown_seconds = 0  # disable cooldown for test
    engine.update_price(99.0)
    assert engine.state == EngineState.LONG
    engine.update_price(95.0)
    assert engine.state == EngineState.LONG
    # Eventually sell
    result = engine.update_price(201.0)
    assert result.triggered is True
    assert result.action == "SELL"
    assert engine.state == EngineState.FLAT
```

- [ ] **Step 7: Run all engine tests**

Run: `cd backend && python -m pytest tests/test_engine.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/engine.py backend/tests/test_engine.py
git commit -m "feat(engine): allow add-on BUY while LONG when price <= buy_low"
```

---

## Task 2: Context Module — Position Cost Rendering

**Files:**
- Modify: `backend/app/domain/prompt/context_module.py`
- Create: `backend/tests/test_context_module.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_module.py
from app.domain.prompt.context_module import ContextModule


def _base_context(**overrides) -> dict:
    ctx = {
        "daily_candles": [],
        "minute_candles": [],
        "current_price": 200.0,
        "atr": 5.0,
        "bb_upper": 210.0,
        "bb_middle": 200.0,
        "bb_lower": 190.0,
        "rsi": 0.0,
        "macd": {},
        "volume_analysis": {},
        "current_position": "FLAT",
        "position_quantity": 0.0,
        "position_avg_price": 0.0,
        "unrealized_pnl_pct": 0.0,
    }
    ctx.update(overrides)
    return ctx


class TestContextModulePositionCost:
    def test_renders_position_cost_when_long(self) -> None:
        ctx = _base_context(
            current_position="LONG",
            position_quantity=100.0,
            position_avg_price=195.0,
            unrealized_pnl_pct=2.56,
            current_price=200.0,
        )
        rendered = ContextModule().render(ctx)
        assert "持仓方向: LONG" in rendered
        assert "持仓数量: 100" in rendered
        assert "平均成本: 195.00" in rendered
        assert "当前价格: 200.00" in rendered
        assert "+2.56%" in rendered

    def test_renders_flat_when_no_position(self) -> None:
        ctx = _base_context(current_position="FLAT", position_quantity=0.0)
        rendered = ContextModule().render(ctx)
        assert "当前无持仓" in rendered

    def test_renders_short_position(self) -> None:
        ctx = _base_context(
            current_position="SHORT",
            position_quantity=50.0,
            position_avg_price=205.0,
            unrealized_pnl_pct=2.44,
        )
        rendered = ContextModule().render(ctx)
        assert "持仓方向: SHORT" in rendered
        assert "持仓数量: 50" in rendered
        assert "平均成本: 205.00" in rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_context_module.py -v`
Expected: FAIL — ContextModule doesn't render position cost section yet.

- [ ] **Step 3: Implement position cost rendering in ContextModule**

Add the position cost section to `ContextModule.render()`, after the `## 当前技术指标` section and before the extended indicators block. Insert after the `lines.append(f"- 当前价格: {current_price:.2f}")` line:

```python
# Position cost section
current_position = context.get("current_position", "FLAT")
position_quantity = float(context.get("position_quantity", 0.0))
position_avg_price = float(context.get("position_avg_price", 0.0))
unrealized_pnl_pct = float(context.get("unrealized_pnl_pct", 0.0))

if position_quantity > 0 and position_avg_price > 0:
    lines.append("")
    lines.append("## 持仓成本")
    lines.append(f"- 持仓方向: {current_position}")
    lines.append(f"- 持仓数量: {position_quantity:.0f}")
    lines.append(f"- 平均成本: {position_avg_price:.2f}")
    lines.append(f"- 当前价格: {current_price:.2f}")
    lines.append(f"- 浮盈/浮亏: {unrealized_pnl_pct:+.2f}%")
elif current_position == "FLAT" or position_quantity <= 0:
    lines.append("")
    lines.append("## 持仓成本")
    lines.append("当前无持仓。")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_context_module.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/prompt/context_module.py backend/tests/test_context_module.py
git commit -m "feat(prompt): render real position cost context in LLM prompt"
```

---

## Task 3: Position Context — Prefer Tracked Entries Over Broker

**Files:**
- Modify: `backend/app/api/llm_advisor.py` (`_position_context` function)

- [ ] **Step 1: Update `_position_context` to use tracked entries when available**

The `_position_context` function currently only reads broker positions. Update it to prefer `tracked_entries` for `avg_price` when available, since tracked entries have the accurate weighted-average cost:

```python
def _position_context(symbol: str, current_price: float) -> dict[str, float | str]:
    runner = get_runner()
    side = runner.engine.state.value.upper()
    quantity = 0.0
    avg_price = 0.0

    # Try tracked entries first for accurate cost basis
    trade_svc = getattr(runner, "_trade_svc", None)
    if trade_svc is not None:
        with trade_svc._state_lock:
            entry = trade_svc._entry_positions.get(symbol)
            if entry is not None and entry.quantity > 0:
                quantity = float(entry.quantity)
                avg_price = float(entry.avg_price)
                side = "LONG"  # tracked entries are only for long positions currently

    # Fall back to broker positions for quantity if tracked entries absent
    if quantity <= 0:
        try:
            positions = runner.broker.get_positions()
        except Exception:
            logger.exception("failed to load position context for LLM analysis")
            return {
                "side": side,
                "quantity": 0.0,
                "avg_price": 0.0,
                "unrealized_pnl_pct": 0.0,
            }

        position = next((p for p in positions if p.symbol == symbol and p.quantity > 0), None)
        if position is None:
            return {"side": "FLAT", "quantity": 0.0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0}

        quantity = float(position.quantity)
        avg_price = float(position.avg_price)
        side = position.side

    if avg_price <= 0:
        pnl_pct = 0.0
    elif side == "SHORT":
        pnl_pct = (avg_price - current_price) / avg_price * 100
    else:
        pnl_pct = (current_price - avg_price) / avg_price * 100

    return {
        "side": side,
        "quantity": quantity,
        "avg_price": avg_price,
        "unrealized_pnl_pct": pnl_pct,
    }
```

- [ ] **Step 2: Run all existing tests to verify no regression**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: All 621+ pass

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/llm_advisor.py
git commit -m "feat(llm): prefer tracked entries for accurate cost basis in position context"
```

---

## Task 4: Verify Anti-Chasing Guard in Interval Application

**Files:**
- Modify: `backend/tests/test_interval_application.py` (add explicit anti-chasing tests)

The `_apply_long` method already implements `new_buy_low <= old_buy_low` guard. This task adds explicit tests to lock down the behavior.

- [ ] **Step 1: Add anti-chasing guard tests**

Add to `test_interval_application.py`:

```python
def test_apply_long_does_not_raise_buy_low(self, service: IntervalApplicationService) -> None:
    self._cleanup()
    db = self._get_db()
    config = self._create_config(db)
    assert config.buy_low == 180.0
    assert config.sell_high == 220.0

    result = service.apply_suggestion(
        db,
        engine_state="long",
        current_price=200.0,
        suggestion={
            "suggested_buy_low": 185.0,  # higher than current 180
            "suggested_sell_high": 225.0,
            "confidence_score": 0.85,
        },
    )

    assert result["success"] is True
    # buy_low should NOT be raised (anti-chasing guard)
    assert result["buy_low"] == 180.0
    # sell_high should be raised
    assert result["sell_high"] == 225.0

def test_apply_long_lowers_buy_low(self, service: IntervalApplicationService) -> None:
    self._cleanup()
    db = self._get_db()
    self._create_config(db)

    result = service.apply_suggestion(
        db,
        engine_state="long",
        current_price=200.0,
        suggestion={
            "suggested_buy_low": 175.0,  # lower than current 180
            "suggested_sell_high": 225.0,
            "confidence_score": 0.85,
        },
    )

    assert result["success"] is True
    assert result["buy_low"] == 175.0
    assert result["sell_high"] == 225.0

def test_apply_long_ignores_buy_low_when_none(self, service: IntervalApplicationService) -> None:
    self._cleanup()
    db = self._get_db()
    self._create_config(db)

    result = service.apply_suggestion(
        db,
        engine_state="long",
        current_price=200.0,
        suggestion={
            "suggested_buy_low": None,
            "suggested_sell_high": 225.0,
            "confidence_score": 0.85,
        },
    )

    assert result["success"] is True
    assert result["buy_low"] == 180.0  # unchanged
    assert result["sell_high"] == 225.0
```

- [ ] **Step 2: Run interval application tests**

Run: `cd backend && python -m pytest tests/test_interval_application.py -v`
Expected: All PASS (the guard already exists in code, these are regression tests)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_interval_application.py
git commit -m "test(interval): add explicit anti-chasing guard tests for LONG buy_low"
```

---

## Task 5: Full Test Suite Verification

- [ ] **Step 1: Run all backend tests**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: All tests pass (621 baseline + new tests)

- [ ] **Step 2: Run basedpyright**

Run: `cd backend && python -m basedpyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 3: Run frontend type check and build**

Run: `cd frontend && npm run type-check && npm run build`
Expected: Both pass

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: resolve any type or test issues from P13 add-on buy"
```

---

## Summary

| Task | Scope | New Tests | Risk |
|------|-------|-----------|------|
| T1 | Engine add-on buy | ~6 | Low — existing cooldown and risk gates protect |
| T2 | Position cost in prompt | ~3 | Minimal — rendering only |
| T3 | Tracked entries in position context | 0 | Low — fallback to broker |
| T4 | Anti-chasing guard tests | ~3 | Test-only |
| T5 | Full verification | 0 | — |

**Total new tests:** ~12

**Files changed:** 4 modified, 1 new test file
