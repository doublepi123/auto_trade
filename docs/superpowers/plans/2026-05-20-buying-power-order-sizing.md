# Buying Power Order Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Size new `BUY` and `SELL_SHORT` entry orders from Longbridge margin buying power instead of cash balance.

**Architecture:** Add a focused broker method that wraps Longbridge `estimate_max_purchase_quantity()` and returns `margin_max_qty`. Add a focused entry-sizing helper in `TradeExecutionService` that applies a fixed 90% safety factor and is used only by entry flows. Closing flows continue to use actual position quantity.

**Tech Stack:** Python 3.11, FastAPI backend, Longbridge `longport.openapi`, pytest unit tests, Docker Compose verification.

---

## File Structure

- Modify `backend/app/core/broker.py`: add `BrokerGateway.estimate_margin_max_quantity()` near the existing account/cash methods. This file owns Longbridge SDK adaptation and object parsing.
- Modify `backend/app/services/trade_execution_service.py`: add a private `_entry_quantity_from_margin_power()` helper and update `_execute_buy()` / `_execute_sell_short()` to use it. This file owns order execution and sizing decisions.
- Modify `backend/tests/test_broker.py`: add Longbridge response parsing and SDK-call contract tests for the new broker method.
- Modify `backend/tests/test_trade_execution_service.py`: add entry sizing tests for buy, short, zero quantity, and close-out preservation.

---

### Task 1: Broker Margin Quantity Adapter

**Files:**
- Modify: `backend/app/core/broker.py`
- Test: `backend/tests/test_broker.py`

- [ ] **Step 1: Add failing tests for Longbridge max quantity estimation**

Append these tests inside `class TestBrokerGateway` in `backend/tests/test_broker.py`:

```python
    def test_estimate_margin_max_quantity_uses_longbridge_estimate(self) -> None:
        called = {}

        class Response:
            cash_max_qty = Decimal("12")
            margin_max_qty = Decimal("45")

        class TradeContext:
            def estimate_max_purchase_quantity(self, **kwargs):
                called.update(kwargs)
                return Response()

        class OrderSide:
            Buy = "OrderSide.Buy"
            Sell = "OrderSide.Sell"

        class OrderType:
            LO = "OrderType.LO"

        class FakeModule:
            pass

        FakeModule.OrderSide = OrderSide
        FakeModule.OrderType = OrderType

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        try:
            qty = gw.estimate_margin_max_quantity("NVDA.US", "BUY", Decimal("222.50"), "USD")
        finally:
            monkeypatch.undo()

        assert qty == Decimal("45")
        assert called == {
            "symbol": "NVDA.US",
            "order_type": "OrderType.LO",
            "side": "OrderSide.Buy",
            "price": Decimal("222.50"),
            "currency": "USD",
            "fractional_shares": False,
        }

    def test_estimate_margin_max_quantity_supports_sell_side(self, monkeypatch) -> None:
        called = {}

        class Response:
            margin_max_qty = "88"

        class TradeContext:
            def estimate_max_purchase_quantity(self, **kwargs):
                called.update(kwargs)
                return Response()

        class OrderSide:
            Buy = "OrderSide.Buy"
            Sell = "OrderSide.Sell"

        class OrderType:
            LO = "OrderType.LO"

        class FakeModule:
            pass

        FakeModule.OrderSide = OrderSide
        FakeModule.OrderType = OrderType
        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        qty = gw.estimate_margin_max_quantity("NVDA.US", "SELL", Decimal("225.00"), "USD")

        assert qty == Decimal("88")
        assert called["side"] == "OrderSide.Sell"
```

- [ ] **Step 2: Run broker tests and verify they fail**

Run:

```bash
cd backend
python3 -m pytest tests/test_broker.py::TestBrokerGateway::test_estimate_margin_max_quantity_uses_longbridge_estimate tests/test_broker.py::TestBrokerGateway::test_estimate_margin_max_quantity_supports_sell_side -v
```

Expected: both tests fail with `AttributeError: 'BrokerGateway' object has no attribute 'estimate_margin_max_quantity'`.

- [ ] **Step 3: Implement the broker adapter**

In `backend/app/core/broker.py`, add this method after `get_cash()` and before `get_account()`:

```python
    def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency: str | None = None) -> Decimal:
        with self._lock:
            self._init_clients()
            module = _import_openapi()
            OrderSide = getattr(module, "OrderSide", None)
            OrderType = getattr(module, "OrderType", None)

            side_name = _SIDE_MAP.get(side, side)
            side_enum = getattr(OrderSide, side_name, side) if OrderSide else side
            lo_type = getattr(OrderType, "LO") if OrderType else "LO"

            response = self._trade_ctx.estimate_max_purchase_quantity(
                symbol=symbol,
                order_type=lo_type,
                side=side_enum,
                price=price,
                currency=currency,
                fractional_shares=False,
            )
            return _decimal_attr(response, "margin_max_qty")
```

- [ ] **Step 4: Run broker tests and verify they pass**

Run:

```bash
cd backend
python3 -m pytest tests/test_broker.py::TestBrokerGateway::test_estimate_margin_max_quantity_uses_longbridge_estimate tests/test_broker.py::TestBrokerGateway::test_estimate_margin_max_quantity_supports_sell_side -v
```

Expected: both tests pass.

---

### Task 2: Entry Order Sizing From Margin Power

**Files:**
- Modify: `backend/app/services/trade_execution_service.py`
- Test: `backend/tests/test_trade_execution_service.py`

- [ ] **Step 1: Add failing tests for margin-power entry sizing**

Append these tests inside `class TestTradeExecutionServiceBasics` in `backend/tests/test_trade_execution_service.py`:

```python
    def test_execute_buy_uses_margin_max_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-1", "NVDA.US", "BUY", Decimal("90"), Decimal("222.5"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-1", "FILLED", Decimal("90"), Decimal("222.5")))

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.5, 222.4, 222.6, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_called_once_with("NVDA.US", "BUY", Decimal("222.5"), "USD")
        broker.get_cash.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("90"), Decimal("222.5"))

    def test_execute_sell_short_uses_margin_max_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("50")
        broker.submit_limit_order.return_value = OrderResult("order-2", "NVDA.US", "SELL", Decimal("45"), Decimal("225"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-2", "FILLED", Decimal("45"), Decimal("225")))

        status = svc.execute(
            "SELL_SHORT",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_called_once_with("NVDA.US", "SELL", Decimal("225"), "USD")
        broker.get_cash.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("45"), Decimal("225"))

    def test_execute_buy_skips_zero_margin_quantity(self, svc: TradeExecutionService) -> None:
        from app.core.broker import Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("1")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.5, 222.4, 222.6, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is None
        broker.submit_limit_order.assert_not_called()
```

- [ ] **Step 2: Add failing tests for close-out behavior preservation**

Append this test inside `class TestTradeExecutionServiceBasics`:

```python
    def test_execute_sell_still_uses_position_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("7"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-3", "NVDA.US", "SELL", Decimal("7"), Decimal("225"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-3", "FILLED", Decimal("7"), Decimal("225")))

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("7"), Decimal("225"))
```

- [ ] **Step 3: Run trade execution tests and verify entry tests fail**

Run:

```bash
cd backend
python3 -m pytest tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_buy_uses_margin_max_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_sell_short_uses_margin_max_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_buy_skips_zero_margin_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_sell_still_uses_position_quantity -v
```

Expected: entry sizing tests fail because current code calls `broker.get_cash()`; the close-out preservation test should pass or fail only because of test scaffolding, not because close-out uses buying power.

- [ ] **Step 4: Add the sizing helper**

In `backend/app/services/trade_execution_service.py`, add a module-level constant near the dataclasses:

```python
ENTRY_BUYING_POWER_USAGE = Decimal("0.9")
```

Add this private method inside `TradeExecutionService`, before `_execute_buy()`:

```python
    def _entry_quantity_from_margin_power(
        self,
        broker: BrokerGateway,
        symbol: str,
        side: str,
        price: Decimal,
        cash_currency: str,
    ) -> int:
        max_qty = broker.estimate_margin_max_quantity(symbol, side, price, cash_currency)
        qty = int(max_qty * ENTRY_BUYING_POWER_USAGE)
        if qty <= 0:
            logger.warning(
                "%s: qty <= 0, margin_max_qty=%s price=%s currency=%s",
                side,
                max_qty,
                price,
                cash_currency,
            )
        return qty
```

- [ ] **Step 5: Update BUY sizing**

In `_execute_buy()`, replace:

```python
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
```

with:

```python
        price = Decimal(str(quote.last_price))
```

Then replace:

```python
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return None
```

with:

```python
        qty = self._entry_quantity_from_margin_power(broker, symbol, "BUY", price, cash_currency)
        if qty <= 0:
            return None
```

- [ ] **Step 6: Update SELL_SHORT sizing**

In `_execute_sell_short()`, replace:

```python
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
```

with:

```python
        price = Decimal(str(quote.last_price))
```

Then replace:

```python
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return None
```

with:

```python
        qty = self._entry_quantity_from_margin_power(broker, symbol, "SELL", price, cash_currency)
        if qty <= 0:
            return None
```

- [ ] **Step 7: Run trade execution tests and verify they pass**

Run:

```bash
cd backend
python3 -m pytest tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_buy_uses_margin_max_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_sell_short_uses_margin_max_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_buy_skips_zero_margin_quantity tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_execute_sell_still_uses_position_quantity -v
```

Expected: all selected tests pass.

---

### Task 3: Verification And Deployment

**Files:**
- Verify: `backend/app/core/broker.py`
- Verify: `backend/app/services/trade_execution_service.py`
- Verify: `backend/tests/test_broker.py`
- Verify: `backend/tests/test_trade_execution_service.py`

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
cd backend
python3 -m pytest tests/test_broker.py tests/test_trade_execution_service.py -v
```

Expected: all tests in both files pass.

- [ ] **Step 2: Run backend compile check in Docker**

Run:

```bash
docker compose run --rm --entrypoint python backend -m compileall app
```

Expected: command exits 0 and lists compiled backend modules.

- [ ] **Step 3: Run broader backend API tests if pytest is available**

Run:

```bash
cd backend
python3 -m pytest tests/test_api.py tests/test_runner.py tests/test_broker.py tests/test_trade_execution_service.py -v
```

Expected: all selected tests pass. If the local environment lacks pytest, record the exact missing-module error and rely on Docker compile plus any available test runner.

- [ ] **Step 4: Rebuild and deploy**

Run:

```bash
docker compose up --build -d
```

Expected: backend and frontend images build successfully and containers restart.

- [ ] **Step 5: Confirm containers are healthy**

Run:

```bash
docker ps --filter name=auto_trade --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Expected: backend and frontend containers show healthy status.

- [ ] **Step 6: Inspect final diff**

Run:

```bash
git diff -- backend/app/core/broker.py backend/app/services/trade_execution_service.py backend/tests/test_broker.py backend/tests/test_trade_execution_service.py
```

Expected: diff only contains broker margin quantity adapter, trade execution entry sizing changes, and related tests.

---

## Self-Review

- Spec coverage: broker adapter, 90% safety factor, BUY/SELL_SHORT entry flows, SELL/BUY_TO_COVER non-goals, zero quantity handling, and tests are all covered.
- Placeholder scan: no TBD/TODO/fill-in placeholders remain.
- Type consistency: plan uses existing `Decimal`, `BrokerGateway`, `Quote`, `OrderResult`, `Position`, `RiskController`, `ServerChanNotifier`, and `OrderStatus` names from the codebase.
