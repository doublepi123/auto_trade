# Add-On Buy And Cost-Anchored LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow add-on `BUY` orders while already long and make LLM interval recommendations aware of current position quantity, average cost, and unrealized P/L.

**Architecture:** Extend the existing strategy engine state rules without adding new persisted settings. Add position context in the LLM API path and pass it into `DataAggregator.build_prompt()`. Adjust long-state interval application so `buy_low` can move lower but cannot be raised by frequent LLM refreshes.

**Tech Stack:** Python 3.11, FastAPI backend, pytest, existing Longbridge broker wrapper, Docker Compose verification.

---

## File Structure

- Modify `backend/app/core/engine.py`: add `LONG + price <= buy_low -> BUY` while keeping state `LONG`.
- Modify `backend/tests/test_engine.py`: add add-on buy and cooldown tests.
- Modify `backend/app/api/llm_advisor.py`: derive position context from runner broker positions and pass it to LLM analysis.
- Modify `backend/app/services/llm_advisor_service.py`: accept and forward position context.
- Modify `backend/app/services/data_aggregator.py`: include position context and cost-anchored rules in the prompt.
- Modify `backend/app/services/interval_application_service.py`: in long state, allow lowering `buy_low` but do not raise it.
- Modify `backend/tests/test_llm_advisor.py`: assert prompt contains quantity, average cost, and unrealized P/L guidance.
- Modify `backend/tests/test_interval_application.py`: assert long-state `buy_low` lowering is allowed and raising is blocked.
- Modify `backend/tests/test_runner.py`: add/adjust runner coverage for an add-on buy while already long.

---

### Task 1: Add Engine Rule For Long-State Add-On Buy

**Files:**
- Modify: `backend/tests/test_engine.py`
- Modify: `backend/app/core/engine.py`

- [ ] **Step 1: Write failing engine tests**

Add these tests to `class TestStrategyEngine` in `backend/tests/test_engine.py`:

```python
    def test_price_below_buy_low_from_long_triggers_add_on_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG

        result = engine.update_price(99.0)

        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_cooldown_prevents_repeated_add_on_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine._cooldown_seconds = 60

        first = engine.update_price(99.0)
        second = engine.update_price(98.0)

        assert first.triggered is True
        assert first.action == "BUY"
        assert second.triggered is False
        assert engine.state == EngineState.LONG
```

- [ ] **Step 2: Run tests and verify red**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_engine.py::TestStrategyEngine::test_price_below_buy_low_from_long_triggers_add_on_buy backend/tests/test_engine.py::TestStrategyEngine::test_cooldown_prevents_repeated_add_on_buy -v
```

Expected: first test fails because `LONG` below `buy_low` currently does not trigger.

- [ ] **Step 3: Implement add-on buy rule**

In `backend/app/core/engine.py`, change the `LONG` block to:

```python
        elif self.state == EngineState.LONG:
            if price <= self.params.buy_low:
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="BUY",
                    description=f"Price {price} <= buy_low {self.params.buy_low}, add LONG",
                )
            if price >= self.params.sell_high:
                self.state = EngineState.FLAT
                self._mark_trigger(price)
                return TriggerResult(
                    triggered=True,
                    action="SELL",
                    description=f"Price {price} >= sell_high {self.params.sell_high}, sell LONG",
                )
```

- [ ] **Step 4: Run engine tests and verify green**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_engine.py -v
```

Expected: all engine tests pass.

---

### Task 2: Add Position Context To LLM Prompt

**Files:**
- Modify: `backend/app/api/llm_advisor.py`
- Modify: `backend/app/services/llm_advisor_service.py`
- Modify: `backend/app/services/data_aggregator.py`
- Modify: `backend/tests/test_llm_advisor.py`

- [ ] **Step 1: Write failing prompt test**

Update an existing `build_prompt()` test in `backend/tests/test_llm_advisor.py` or add this test:

```python
    def test_build_prompt_includes_position_cost_context(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.68,
            current_sell_high=224.12,
            short_selling=False,
            daily_candles=[],
            minute_candles=[],
            atr=0.0,
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            current_position="LONG",
            recent_trades=[],
            position_quantity=18.0,
            position_avg_price=255.942,
            unrealized_pnl_pct=-13.34,
        )

        assert "当前持仓方向: LONG" in prompt
        assert "当前持仓数量: 18.0" in prompt
        assert "持仓成本价: 255.94" in prompt
        assert "浮动盈亏比例: -13.34%" in prompt
        assert "不要仅按当前价格" in prompt
```

- [ ] **Step 2: Run prompt test and verify red**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_llm_advisor.py::TestDataAggregator::test_build_prompt_includes_position_cost_context -v
```

Expected: fails because `build_prompt()` does not accept the new keyword arguments.

- [ ] **Step 3: Extend `DataAggregator.build_prompt()` signature and text**

In `backend/app/services/data_aggregator.py`, extend the signature:

```python
        position_quantity: float = 0.0,
        position_avg_price: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
```

Replace the current position lines with:

```python
- 当前持仓方向: {current_position}
- 当前持仓数量: {position_quantity}
- 持仓成本价: {position_avg_price:.2f}
- 浮动盈亏比例: {unrealized_pnl_pct:.2f}%
```

Replace the current-price-only recommendation line with:

```python
6. FLAT 状态可参考当前价格和 ATR；已有持仓时必须结合持仓成本价、持仓数量和浮动盈亏设计区间，不要仅按当前价格 ±1% 滚动追价
7. LONG 状态下，buy_low 是加仓触发价，应结合成本价和回撤幅度；sell_high 应优先考虑持仓成本价，不要在未说明止损的情况下长期低于成本价
```

- [ ] **Step 4: Add position context helper in API path**

In `backend/app/api/llm_advisor.py`, add helper functions near the top:

```python
def _position_context(symbol: str, current_price: float) -> dict[str, float | str]:
    try:
        positions = get_runner().broker.get_positions()
    except Exception:
        logger.exception("failed to load position context for LLM analysis")
        return {"side": get_runner().engine.state.value.upper(), "quantity": 0.0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0}

    position = next((p for p in positions if p.symbol == symbol and p.quantity > 0), None)
    if position is None:
        return {"side": "FLAT", "quantity": 0.0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0}

    avg_price = float(position.avg_price)
    if avg_price <= 0:
        pnl_pct = 0.0
    elif position.side == "SHORT":
        pnl_pct = (avg_price - current_price) / avg_price * 100
    else:
        pnl_pct = (current_price - avg_price) / avg_price * 100
    return {"side": position.side, "quantity": float(position.quantity), "avg_price": avg_price, "unrealized_pnl_pct": pnl_pct}
```

Then in `analyze_llm_interval()`, compute:

```python
    position_context = _position_context(config.symbol, current_price)
```

Pass these values to `advisor.analyze()`:

```python
        position_quantity=float(position_context["quantity"]),
        position_avg_price=float(position_context["avg_price"]),
        unrealized_pnl_pct=float(position_context["unrealized_pnl_pct"]),
```

Set `current_position=str(position_context["side"]).lower()` or keep engine state if preferred, but the prompt must receive real position values.

- [ ] **Step 5: Extend `LLMAdvisorService.analyze()` and forward context**

In `backend/app/services/llm_advisor_service.py`, add parameters:

```python
        position_quantity: float = 0.0,
        position_avg_price: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
```

Pass them into `build_prompt()` with the same names.

- [ ] **Step 6: Run LLM tests**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_llm_advisor.py -v
```

Expected: all LLM advisor tests pass.

---

### Task 3: Prevent Long-State LLM Buy Trigger From Chasing Price Upward

**Files:**
- Modify: `backend/app/services/interval_application_service.py`
- Modify: `backend/tests/test_interval_application.py`

- [ ] **Step 1: Write failing interval application tests**

Add these tests to `backend/tests/test_interval_application.py`:

```python
    def test_apply_long_does_not_raise_buy_low(self, service: IntervalApplicationService) -> None:
        db = DummyDB()
        config = DummyConfig(buy_low=219.0, sell_high=224.0)
        db.config = config

        result = service.apply_suggestion(db, "long", 222.0, {
            "suggested_buy_low": 221.0,
            "suggested_sell_high": 226.0,
            "confidence_score": 0.8,
        })

        assert result["success"] is True
        assert config.buy_low == 219.0
        assert config.sell_high == 226.0

    def test_apply_long_allows_lower_buy_low(self, service: IntervalApplicationService) -> None:
        db = DummyDB()
        config = DummyConfig(buy_low=219.0, sell_high=224.0)
        db.config = config

        result = service.apply_suggestion(db, "long", 222.0, {
            "suggested_buy_low": 217.0,
            "suggested_sell_high": 226.0,
            "confidence_score": 0.8,
        })

        assert result["success"] is True
        assert config.buy_low == 217.0
        assert config.sell_high == 226.0
```

- [ ] **Step 2: Run interval tests and verify red**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_interval_application.py::TestIntervalApplicationService::test_apply_long_does_not_raise_buy_low backend/tests/test_interval_application.py::TestIntervalApplicationService::test_apply_long_allows_lower_buy_low -v
```

Expected: at least one test fails because long-state application ignores `buy_low` today.

- [ ] **Step 3: Implement long-state buy_low rule**

Change `_apply_long()` in `backend/app/services/interval_application_service.py` to accept `new_buy_low`:

```python
    @staticmethod
    def _apply_long(db: Any, config: Any, current_price: float, new_buy_low: float | None, new_sell_high: float | None) -> bool:
        old_buy_low = config.buy_low
        old_sell_high = config.sell_high

        if new_buy_low is not None and new_buy_low <= old_buy_low:
            config.buy_low = new_buy_low

        if new_sell_high is not None:
            min_sell_high = current_price * (1 + settings.llm_interval_volatility_threshold_pct / 100)
            if new_sell_high >= old_sell_high:
                config.sell_high = new_sell_high
            else:
                config.sell_high = max(new_sell_high, min_sell_high)

        return config.buy_low != old_buy_low or config.sell_high != old_sell_high
```

Update caller:

```python
            applied = self._apply_long(db, config, current_price, buy_low, sell_high)
```

- [ ] **Step 4: Run interval tests and verify green**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_interval_application.py -v
```

Expected: all interval application tests pass.

---

### Task 4: Runner Add-On Buy Coverage

**Files:**
- Modify: `backend/tests/test_runner.py`

- [ ] **Step 1: Add runner test for second BUY while long**

Add this test to `class TestAppRunner` in `backend/tests/test_runner.py`:

```python
    def test_on_quote_submits_add_on_buy_when_long_below_buy_low(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions: list[tuple[str, Decimal]] = []

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions.append((side, quantity))
                return OrderResult(f"order-{len(self.submissions)}", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.engine.last_trigger_at = None
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, ""))

        assert broker.submissions == [("BUY", Decimal("9"))]
        assert runner.engine.state == EngineState.LONG
```

- [ ] **Step 2: Run runner test**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_runner.py::TestAppRunner::test_on_quote_submits_add_on_buy_when_long_below_buy_low -v
```

Expected: pass after Tasks 1 and buying-power sizing changes are present.

---

### Task 5: Verification And Deployment

**Files:**
- Verify all changed backend files and tests.

- [ ] **Step 1: Run focused tests**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_engine.py backend/tests/test_llm_advisor.py backend/tests/test_interval_application.py backend/tests/test_runner.py backend/tests/test_broker.py backend/tests/test_trade_execution_service.py -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Compile backend in Docker**

Run:

```bash
docker compose run --rm --entrypoint python backend -m compileall app
```

Expected: exits 0.

- [ ] **Step 3: Build and deploy**

Run:

```bash
docker compose up --build -d
```

Expected: containers rebuild and start.

- [ ] **Step 4: Verify health**

Run:

```bash
docker ps --filter name=auto_trade --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -s http://127.0.0.1:8000/api/health
```

Expected: backend and frontend healthy, health API returns `{"ok": true, "env": "dev"}`.

- [ ] **Step 5: Inspect diff**

Run:

```bash
git diff -- backend/app/core/engine.py backend/app/api/llm_advisor.py backend/app/services/llm_advisor_service.py backend/app/services/data_aggregator.py backend/app/services/interval_application_service.py backend/tests/test_engine.py backend/tests/test_llm_advisor.py backend/tests/test_interval_application.py backend/tests/test_runner.py
```

Expected: diff only contains add-on buy engine behavior, position-aware prompt context, long-state interval application stability, and tests.

---

## Self-Review

- Spec coverage: add-on buy behavior, cooldown, buying-power sizing reuse, LLM position context, cost anchoring, stable long-state `buy_low`, and non-goals are covered.
- Placeholder scan: no TBD/TODO/fill-in placeholders remain.
- Type consistency: plan uses existing `StrategyEngine`, `EngineState`, `Quote`, `OrderResult`, `IntervalApplicationService`, `DataAggregator`, and runner test helper names.
