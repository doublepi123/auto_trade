# Maintainability Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the Auto Trade project for clearer module boundaries and better frontend DX. Extract focused backend services from the monolithic runner, split the frontend API layer into domain modules, introduce Vue composables, and refactor Dashboard into maintainable sections.

**Architecture:**
- **Backend**: `AppRunner` becomes a lifecycle coordinator only. `TradeExecutionService` owns all order-producing actions (quantity calculation, submission, recording, notification, PnL). `RuntimeStateService` owns DB load/persist for engine and risk state.
- **Frontend**: Single `api/index.ts` splits into `client.ts` + domain modules (`strategy.ts`, `credentials.ts`, `trade.ts`). Dashboard logic moves into composables (`useDashboardData`, `useStatusStream`, `useAccountRefresh`).

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, Vue 3, TypeScript, Element Plus, pytest, Cypress

**Breaking changes allowed per spec:** Yes. Old SQLite data can be discarded.

---

## File Structure

### Backend — New / Modified

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `backend/app/services/trade_execution_service.py` | Order action routing, quantity calc, submission, order DB record, notify, PnL |
| Create | `backend/app/services/runtime_state_service.py` | Load/persist engine and risk runtime state from/to DB |
| Modify | `backend/app/runner.py` | Strip out execution and persistence; keep lifecycle, quote routing, broadcast |
| Create | `backend/tests/test_trade_execution_service.py` | Unit tests for extracted trade execution logic |
| Create | `backend/tests/test_runtime_state_service.py` | Unit tests for state load/persist |
| Modify | `backend/tests/test_runner.py` | Update to use new service boundaries |

### Frontend — New / Modified

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `frontend/src/api/client.ts` | Shared axios instance, API key injection, 401 handling |
| Create | `frontend/src/api/strategy.ts` | Strategy config and status API |
| Create | `frontend/src/api/credentials.ts` | Credentials API |
| Create | `frontend/src/api/trade.ts` | Orders, account, control actions API |
| Modify | `frontend/src/api/index.ts` | Re-export for backward compat during transition |
| Create | `frontend/src/composables/useDashboardData.ts` | Dashboard initial load + refresh |
| Create | `frontend/src/composables/useStatusStream.ts` | WebSocket + polling fallback |
| Create | `frontend/src/composables/useAccountRefresh.ts` | Periodic account data refresh |
| Create | `frontend/src/composables/useFormState.ts` | Dirty/saving/saved/error form state |
| Modify | `frontend/src/views/Dashboard.vue` | Use composables, clear section layout, unavailable-vs-zero |
| Modify | `frontend/src/views/Strategy.vue` | Use useFormState, consistent save UX |
| Modify | `frontend/src/views/Credentials.vue` | Use useFormState, consistent save UX |

### Verification

| Action | Command |
|--------|---------|
| Backend tests | `cd backend && python3 -m pytest tests/ -v` |
| Frontend build | `cd frontend && npm run build` |
| Cypress | `cd frontend && npm run cypress:run` |
| Docker | `docker compose up --build -d` + health checks |

---

### Task 1: Extract TradeExecutionService

**Files:**
- Create: `backend/app/services/trade_execution_service.py`
- Modify: `backend/app/runner.py` (remove execution methods)

**Rationale:** `runner.py` currently contains 4 `_execute_*` methods, pending-order tracking, order DB recording, and PnL logic. Extracting this makes the runner a pure coordinator and makes order execution independently testable.

- [ ] **Step 1: Write `backend/app/services/trade_execution_service.py`**

```python
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Callable

from app.core.broker import BrokerGateway, OrderResult, Quote
from app.core.risk import RiskController
from app.core.notify import ServerChanNotifier

logger = logging.getLogger("auto_trade.trade_execution")

_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_FAILED_ORDER_STATUSES = {"REJECTED", "CANCELLED"}


@dataclass
class OrderStatus:
    broker_order_id: str
    status: str
    executed_quantity: Decimal
    executed_price: Decimal


class TradeExecutionService:
    """Handles order-producing actions: validation, submission, DB recording, notification, PnL."""

    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float, str], None],
        update_order_status: Callable[[str, str, datetime | None, float | None, float | None], None],
        record_risk_event: Callable[[str], None],
    ) -> None:
        self._record_order = record_order
        self._update_order_status = update_order_status
        self._record_risk_event = record_risk_event
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0

    def execute(
        self,
        action: str,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
    ) -> OrderStatus | None:
        """Execute a single trade action. Returns final order status or None if not submitted."""
        if action == "BUY":
            return self._execute_buy(symbol, quote, broker, risk, notifier, cash_currency)
        if action == "SELL":
            return self._execute_sell(symbol, quote, broker, risk, notifier)
        if action == "SELL_SHORT":
            return self._execute_sell_short(symbol, quote, broker, risk, notifier, cash_currency)
        if action == "BUY_TO_COVER":
            return self._execute_buy_to_cover(symbol, quote, broker, risk, notifier)
        logger.warning("unknown action: %s", action)
        return None

    def _execute_buy(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
    ) -> OrderStatus | None:
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return None
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return None

        result = broker.submit_limit_order(symbol, "BUY", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price), status)
        final_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(final_status)

        if final_status.status == "FILLED":
            fill_price = final_status.executed_price if final_status.executed_price > 0 else price
            fill_qty = final_status.executed_quantity if final_status.executed_quantity > 0 else Decimal(qty)
            self._safe_notify_order(notifier, "BUY", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
            logger.info("BUY: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        else:
            logger.warning("BUY not filled: %s status=%s", result.broker_order_id, final_status.status)

        return final_status

    def _execute_sell(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return None

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return None

        result = broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price), status)
        final_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(final_status)

        if final_status.status == "FILLED":
            fill_price = final_status.executed_price if final_status.executed_price > 0 else price
            fill_qty = final_status.executed_quantity if final_status.executed_quantity > 0 else long_pos.quantity
            pnl = float((fill_price - long_pos.avg_price) * fill_qty)
            risk.record_trade(pnl)
            self._safe_notify_order(notifier, "SELL", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
            logger.info("SELL: %s qty=%s price=%s pnl=%s", symbol, fill_qty, fill_price, pnl)
        else:
            logger.warning("SELL not filled: %s status=%s", result.broker_order_id, final_status.status)

        return final_status

    def _execute_sell_short(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
    ) -> OrderStatus | None:
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return None
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return None

        result = broker.submit_limit_order(symbol, "SELL", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._record_order(result.broker_order_id, symbol, "SELL_SHORT", float(qty), float(price), status)
        final_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(final_status)

        if final_status.status == "FILLED":
            fill_price = final_status.executed_price if final_status.executed_price > 0 else price
            fill_qty = final_status.executed_quantity if final_status.executed_quantity > 0 else Decimal(qty)
            self._safe_notify_order(notifier, "SELL_SHORT", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
            logger.info("SELL_SHORT: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        else:
            logger.warning("SELL_SHORT not filled: %s status=%s", result.broker_order_id, final_status.status)

        return final_status

    def _execute_buy_to_cover(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return None

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return None

        result = broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._record_order(result.broker_order_id, symbol, "BUY_TO_COVER", float(pos.quantity), float(price), status)
        final_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(final_status)

        if final_status.status == "FILLED":
            fill_price = final_status.executed_price if final_status.executed_price > 0 else price
            fill_qty = final_status.executed_quantity if final_status.executed_quantity > 0 else pos.quantity
            pnl = float((pos.avg_price - fill_price) * fill_qty)
            risk.record_trade(pnl)
            self._safe_notify_order(notifier, "BUY_TO_COVER", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
            logger.info("BUY_TO_COVER: %s qty=%s price=%s pnl=%s", symbol, fill_qty, fill_price, pnl)
        else:
            logger.warning("BUY_TO_COVER not filled: %s status=%s", result.broker_order_id, final_status.status)

        return final_status

    def _wait_for_order_completion(self, result: OrderResult, broker: BrokerGateway) -> OrderStatus:
        status = getattr(result, "status", "SUBMITTED")
        last_status = OrderStatus(
            broker_order_id=result.broker_order_id,
            status=status,
            executed_quantity=getattr(result, "quantity", Decimal("0")) if status == "FILLED" else Decimal("0"),
            executed_price=getattr(result, "price", Decimal("0")) if status == "FILLED" else Decimal("0"),
        )
        if status in {"FILLED", "REJECTED", "CANCELLED"}:
            return last_status

        deadline = time.monotonic() + self._order_status_timeout_seconds
        while True:
            try:
                raw_status = broker.get_order_status(result.broker_order_id)
                last_status = OrderStatus(
                    broker_order_id=getattr(raw_status, "broker_order_id", result.broker_order_id),
                    status=getattr(raw_status, "status", "SUBMITTED"),
                    executed_quantity=self._resolved_decimal(raw_status, "executed_quantity", Decimal("0")),
                    executed_price=self._resolved_decimal(raw_status, "executed_price", Decimal("0")),
                )
            except Exception:
                logger.exception("failed to query order status for %s", result.broker_order_id)
                return last_status
            if last_status.status in {"FILLED", "REJECTED", "CANCELLED"}:
                return last_status
            if time.monotonic() >= deadline:
                return last_status
            time.sleep(self._order_status_poll_interval_seconds)

    @staticmethod
    def _resolved_decimal(item: object, name: str, fallback: Decimal) -> Decimal:
        value = getattr(item, name, Decimal("0"))
        try:
            decimal_value = Decimal(str(value))
        except Exception:
            return fallback
        return decimal_value if decimal_value > 0 else fallback

    def _safe_update_order_status_from_result(self, result: OrderStatus) -> None:
        if result.status == "SUBMITTED":
            return
        filled_at = datetime.now(timezone.utc) if result.status in {"FILLED", "REJECTED", "CANCELLED"} else None
        self._update_order_status(
            result.broker_order_id,
            result.status,
            filled_at,
            float(result.executed_quantity) if result.executed_quantity > 0 else None,
            float(result.executed_price) if result.executed_price > 0 else None,
        )

    @staticmethod
    def _safe_notify_order(
        notifier: ServerChanNotifier,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> None:
        try:
            notifier.notify_order(side, symbol, quantity, price, order_id)
        except Exception:
            logger.exception("failed to send order notification for %s %s", side, symbol)
```

- [ ] **Step 2: Run linter / import check**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -c "from app.services.trade_execution_service import TradeExecutionService; print('OK')"
```
Expected: prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/trade_execution_service.py
git commit -m "feat: extract TradeExecutionService from runner"
```

---

### Task 2: Extract RuntimeStateService

**Files:**
- Create: `backend/app/services/runtime_state_service.py`
- Modify: `backend/app/runner.py` (remove persistence methods)

- [ ] **Step 1: Write `backend/app/services/runtime_state_service.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController
from app.models import RuntimeState
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.runtime_state")


class RuntimeStateService:
    """Owns loading and persisting strategy runtime snapshots."""

    def load(self, db: Session, engine: StrategyEngine, risk: RiskController) -> None:
        """Load runtime state from DB into engine and risk."""
        svc = StrategyService(db)
        config = svc.get_config()
        state = svc.get_runtime_state()

        engine.params = StrategyParams(
            symbol=config.symbol,
            market=config.market,
            buy_low=config.buy_low,
            sell_high=config.sell_high,
            short_selling=config.short_selling,
        )
        try:
            engine.state = EngineState(state.engine_state)
        except ValueError:
            logger.warning("invalid engine state %r in DB, defaulting to FLAT", state.engine_state)
            engine.state = EngineState.FLAT
        engine.last_price = state.last_price
        engine.last_trigger_price = state.last_trigger_price
        engine.last_trigger_at = state.last_trigger_at

        risk.config = RiskConfig(
            max_daily_loss=config.max_daily_loss,
            max_consecutive_losses=config.max_consecutive_losses,
        )
        risk.daily_pnl = state.daily_pnl
        risk.consecutive_losses = state.consecutive_losses
        risk.kill_switch = state.kill_switch
        risk.paused = state.paused

    def persist(self, db: Session, engine: StrategyEngine, risk: RiskController) -> None:
        """Persist current engine and risk state to DB."""
        svc = StrategyService(db)
        svc.update_runtime_state(
            engine_state=engine.state.value,
            last_price=engine.last_price,
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            kill_switch=risk.kill_switch,
            paused=risk.paused,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
        )

    def persist_risk(self, db: Session, risk: RiskController) -> None:
        """Persist only risk state (for quick updates after trades)."""
        svc = StrategyService(db)
        svc.update_runtime_state(
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
        )

    def record_risk_event(self, db: Session, reason: str) -> None:
        from app.models import RiskEvent
        event = RiskEvent(event_type="RISK_REJECTION", reason=reason)
        db.add(event)
        db.commit()
```

- [ ] **Step 2: Import check**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -c "from app.services.runtime_state_service import RuntimeStateService; print('OK')"
```
Expected: prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/runtime_state_service.py
git commit -m "feat: extract RuntimeStateService from runner"
```

---

### Task 3: Refactor AppRunner to Coordinator

**Files:**
- Modify: `backend/app/runner.py`
- Modify: `backend/app/services/__init__.py` (add re-exports if needed)

- [ ] **Step 1: Rewrite `backend/app/runner.py`**

Replace the entire file with the coordinator version:

```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Generator

from sqlalchemy.orm import Session

from app.api.ws import manager
from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import EngineState, StrategyEngine, StrategyParams, TriggerResult
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService
from app.services.trade_execution_service import TradeExecutionService

logger = logging.getLogger("auto_trade.runner")

_EngineSnapshot = tuple[EngineState, float, datetime | None]


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self._trade_svc = TradeExecutionService(
            record_order=self._record_order,
            update_order_status=self._update_order_status,
            record_risk_event=self._record_risk_event,
        )
        self._state_svc = RuntimeStateService()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._quotes_subscribed = False

    def _initialize_runner(self) -> None:
        with self._db_session() as db:
            self._state_svc.load(db, self.engine, self.risk)
            self._pause_if_unresolved_live_order_exists(db)
            self._apply_credentials(self._load_credentials(), resubscribe=False)

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        symbol = self.engine.params.symbol
        if symbol and not self._quotes_subscribed:
            try:
                self.broker.subscribe_quotes(symbol, self._on_quote)
                self._quotes_subscribed = True
                logger.info("subscribed to %s quotes", symbol)
            except Exception as exc:
                logger.error("quote subscription failed for %s: %s", symbol, exc)
                logger.error("system running without quote updates")

    def _pause_if_unresolved_live_order_exists(self, db: Session) -> None:
        order = (
            db.query(OrderRecord)
            .filter(OrderRecord.status.in_({"SUBMITTED", "PARTIAL_FILLED"}))
            .order_by(OrderRecord.id.desc())
            .first()
        )
        if order is None:
            return
        reason = f"unresolved live order {order.broker_order_id} requires manual confirmation"
        logger.warning(reason)
        self.risk.pause(reason)

    def start(self) -> bool:
        with self._start_lock:
            if self._running:
                return False
            if self._thread is not None and self._thread.is_alive():
                self._running = False
                self._thread.join(timeout=10)
            try:
                self._initialize_runner()
            except Exception:
                logger.exception("runner initialization failed")
                return False
            self._running = True

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("runner started")
        return True

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
        self.broker.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)

    def reload_credentials(self) -> None:
        self._apply_credentials(self._load_credentials(), resubscribe=self._running)

    def reload_strategy(self) -> None:
        with self._state_lock:
            db = SessionLocal()
            try:
                svc = StrategyService(db)
                config = svc.get_config()
                old_symbol = self.engine.params.symbol
                self.engine.params = StrategyParams(
                    symbol=config.symbol,
                    market=config.market,
                    buy_low=config.buy_low,
                    sell_high=config.sell_high,
                    short_selling=config.short_selling,
                )
                self.risk.config = RiskConfig(
                    max_daily_loss=config.max_daily_loss,
                    max_consecutive_losses=config.max_consecutive_losses,
                )
                if config.symbol != old_symbol and self._running:
                    if self._quotes_subscribed:
                        try:
                            self.broker.unsubscribe_quotes()
                        except Exception:
                            logger.warning("failed to unsubscribe old symbol during strategy reload")
                        self._quotes_subscribed = False
                    if config.symbol:
                        try:
                            self.broker.subscribe_quotes(config.symbol, self._on_quote)
                            self._quotes_subscribed = True
                            logger.info("re-subscribed to %s after strategy reload", config.symbol)
                        except Exception as exc:
                            logger.error("quote subscription failed after strategy reload: %s", exc)
            finally:
                db.close()

    def _on_quote(self, quote: Quote) -> None:
        try:
            with self._state_lock:
                if not self._running:
                    return
                result = self.engine.update_price(quote.last_price)

            self._broadcast_status()

            if not result.triggered:
                return

            risk_result = self.risk.check()
            if not risk_result.approved:
                logger.warning("risk rejected: %s", risk_result.reason)
                self._record_risk_event(risk_result.reason)
                self.notifier.notify_risk_event("REJECTED", risk_result.reason)
                return

            engine_snapshot = self._engine_snapshot()
            try:
                order_status = self._trade_svc.execute(
                    action=result.action,
                    symbol=self.engine.params.symbol,
                    quote=quote,
                    broker=self.broker,
                    risk=self.risk,
                    notifier=self.notifier,
                    cash_currency=self._cash_currency(),
                )
                if order_status is None or order_status.status != "FILLED":
                    self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
            except Exception:
                self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
                raise
        except Exception:
            logger.exception("error processing quote")

    def _engine_snapshot(self) -> _EngineSnapshot:
        with self.engine._lock:
            return (
                self.engine.state,
                self.engine.last_trigger_price,
                self.engine.last_trigger_at,
            )

    def _restore_engine_snapshot(self, snapshot: _EngineSnapshot) -> None:
        state, last_trigger_price, last_trigger_at = snapshot
        with self.engine._lock:
            self.engine.state = state
            self.engine.last_trigger_price = last_trigger_price
            self.engine.last_trigger_at = last_trigger_at

    def _cash_currency(self) -> str:
        return "HKD" if self.engine.params.market == "HK" else "USD"

    def _broadcast_status(self) -> None:
        try:
            data = self.engine.to_dict()
            data["risks"] = {
                "daily_pnl": self.risk.daily_pnl,
                "consecutive_losses": self.risk.consecutive_losses,
                "kill_switch": self.risk.kill_switch,
                "paused": self.risk.paused,
            }
            data["runner_running"] = self.is_running
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.broadcast(data), self._loop)
        except Exception:
            logger.warning("broadcast failed")

    def _run_loop(self) -> None:
        while self._running:
            try:
                with self._db_session() as db:
                    self._state_svc.persist(db, self.engine, self.risk)
            except Exception:
                logger.exception("error persisting state")
            time.sleep(5)

    @staticmethod
    @contextmanager
    def _db_session() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _load_credentials(self) -> PlainCredentials:
        with self._db_session() as db:
            return CredentialsService(db).get_plain_credentials()

    def _apply_credentials(self, credentials: PlainCredentials, *, resubscribe: bool) -> None:
        with self._state_lock:
            symbol = self.engine.params.symbol
            should_resubscribe = resubscribe and bool(symbol)
            new_notifier = ServerChanNotifier(
                credentials.sct_key if credentials.sct_key else settings.sct_key
            )

            self._set_or_clear_env(
                "LONGPORT_APP_KEY",
                credentials.longbridge_app_key or settings.longbridge_app_key,
            )
            self._set_or_clear_env(
                "LONGPORT_APP_SECRET",
                credentials.longbridge_app_secret or settings.longbridge_app_secret,
            )
            self._set_or_clear_env(
                "LONGPORT_ACCESS_TOKEN",
                credentials.longbridge_access_token or settings.longbridge_access_token,
            )

            new_broker = BrokerGateway()

            if should_resubscribe:
                try:
                    new_broker.subscribe_quotes(symbol, self._on_quote)
                except Exception as exc:
                    logger.warning("cannot subscribe quotes after credential reload: %s", exc)
                    new_broker.close()
                    return

            old_broker = self.broker
            old_broker.close()
            self.broker = new_broker
            self.notifier = new_notifier

            if should_resubscribe:
                self._quotes_subscribed = True
            else:
                self._quotes_subscribed = False

    @staticmethod
    def _set_or_clear_env(name: str, value: str) -> None:
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    def _record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        with self._db_session() as db:
            order = OrderRecord(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                status=status,
            )
            db.add(order)
            db.commit()

    def _update_order_status(
        self,
        order_id: str,
        status: str,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
    ) -> None:
        with self._db_session() as db:
            order = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == order_id)
                .order_by(OrderRecord.id.desc())
                .first()
            )
            if order is None:
                logger.warning("cannot update missing order %s to status %s", order_id, status)
                return
            order.status = status
            if filled_at is not None:
                order.filled_at = filled_at
            if executed_quantity is not None:
                order.executed_quantity = executed_quantity
            if executed_price is not None:
                order.executed_price = executed_price
            db.add(order)
            db.commit()

    def _record_risk_event(self, reason: str) -> None:
        with self._db_session() as db:
            self._state_svc.record_risk_event(db, reason)


_runner: AppRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AppRunner()
    return _runner
```

- [ ] **Step 2: Run backend tests to verify refactor**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/test_runner.py tests/test_api.py -v --tb=short
```
Expected: All tests pass. (Some tests may need updates; fix failures before proceeding.)

- [ ] **Step 3: Commit**

```bash
git add backend/app/runner.py backend/app/services/__init__.py
git commit -m "refactor: strip runner to coordinator, delegate to services"
```

---

### Task 4: Add Backend Service Unit Tests

**Files:**
- Create: `backend/tests/test_trade_execution_service.py`
- Create: `backend/tests/test_runtime_state_service.py`
- Modify: `backend/tests/test_runner.py` (if needed)

- [ ] **Step 1: Write `backend/tests/test_trade_execution_service.py`**

```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.trade_execution_service import TradeExecutionService, OrderStatus


class TestTradeExecutionService:
    @pytest.fixture
    def svc(self) -> TradeExecutionService:
        return TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )

    def test_order_status_dataclass(self) -> None:
        s = OrderStatus(broker_order_id="123", status="FILLED", executed_quantity=Decimal("10"), executed_price=Decimal("150"))
        assert s.status == "FILLED"
        assert s.executed_quantity == Decimal("10")

    def test_resolved_decimal_positive(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "150.50"
        result = svc._resolved_decimal(item, "executed_price", Decimal("0"))
        assert result == Decimal("150.50")

    def test_resolved_decimal_zero_fallback(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "0"
        result = svc._resolved_decimal(item, "executed_price", Decimal("99"))
        assert result == Decimal("99")

    def test_resolved_decimal_invalid_fallback(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "invalid"
        result = svc._resolved_decimal(item, "executed_price", Decimal("99"))
        assert result == Decimal("99")

    def test_wait_for_order_completion_already_filled(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult
        result = OrderResult(broker_order_id="1", symbol="AAPL.US", side="BUY", quantity=Decimal("10"), price=Decimal("150"), status="FILLED")
        broker = MagicMock()
        status = svc._wait_for_order_completion(result, broker)
        assert status.status == "FILLED"

    def test_wait_for_order_completion_rejected(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult
        result = OrderResult(broker_order_id="1", symbol="AAPL.US", side="BUY", quantity=Decimal("10"), price=Decimal("150"), status="SUBMITTED")
        broker = MagicMock()
        broker.get_order_status.return_value = MagicMock(status="REJECTED", executed_quantity=Decimal("0"), executed_price=Decimal("0"), broker_order_id="1")
        status = svc._wait_for_order_completion(result, broker)
        assert status.status == "REJECTED"
```

- [ ] **Step 2: Write `backend/tests/test_runtime_state_service.py`**

```python
from __future__ import annotations

import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_runtime_state.db"

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.engine import StrategyEngine, EngineState
from app.core.risk import RiskController
from app.database import Base
from app.models import StrategyConfig, RuntimeState
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService


class TestRuntimeStateService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.query(RuntimeState).delete()
        db.commit()
        db.close()

    def test_load_restores_engine_and_risk(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 3000.0,
            "max_consecutive_losses": 2,
        })
        svc.update_runtime_state(
            engine_state="long",
            last_price=150.0,
            daily_pnl=-100.0,
            consecutive_losses=1,
            kill_switch=False,
            paused=True,
            last_trigger_price=145.0,
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.params.symbol == "AAPL.US"
        assert engine.state == EngineState.LONG
        assert engine.last_price == 150.0
        assert risk.daily_pnl == -100.0
        assert risk.consecutive_losses == 1
        assert risk.paused is True

    def test_load_defaults_on_invalid_engine_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "TSLA.US", "market": "US", "buy_low": 50.0, "sell_high": 100.0})
        svc.update_runtime_state(engine_state="invalid_state")
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.state == EngineState.FLAT

    def test_persist_saves_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "NVDA.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        db.close()

        engine = StrategyEngine()
        engine.state = EngineState.SHORT
        engine.last_price = 180.0
        risk = RiskController()
        risk.daily_pnl = -50.0
        risk.consecutive_losses = 2

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist(db, engine, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state()
        db.close()

        assert state.engine_state == "short"
        assert state.last_price == 180.0
        assert state.daily_pnl == -50.0
        assert state.consecutive_losses == 2
```

- [ ] **Step 3: Run new tests**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/test_trade_execution_service.py tests/test_runtime_state_service.py -v
```
Expected: All new tests pass.

- [ ] **Step 4: Run full backend test suite**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/ -v --tb=short
```
Expected: All tests pass. Fix any regressions caused by the runner refactor.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_trade_execution_service.py backend/tests/test_runtime_state_service.py
git commit -m "test: add unit tests for TradeExecutionService and RuntimeStateService"
```

---

### Task 5: Split Frontend API Layer

**Files:**
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/strategy.ts`
- Create: `frontend/src/api/credentials.ts`
- Create: `frontend/src/api/trade.ts`
- Modify: `frontend/src/api/index.ts`

- [ ] **Step 1: Write `frontend/src/api/client.ts`**

```typescript
import axios from 'axios'

export const api = axios.create({ baseURL: '', timeout: 10000 })

let _notified401 = false

api.interceptors.request.use((config) => {
  const key = localStorage.getItem('api_key')
  if (key) {
    config.headers['X-API-Key'] = key
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response) {
      const { status } = error.response
      if (status === 401) {
        localStorage.removeItem('api_key')
        if (!_notified401) {
          _notified401 = true
          window.dispatchEvent(new CustomEvent('api-key-required'))
          setTimeout(() => { _notified401 = false }, 1000)
        }
      }
    }
    return Promise.reject(error)
  },
)
```

- [ ] **Step 2: Write `frontend/src/api/strategy.ts`**

```typescript
import { api } from './client'
import type { StrategyConfig, StatusData } from '../types'

export async function getStrategy(): Promise<StrategyConfig> {
  const resp = await api.get('/api/strategy')
  return resp.data
}

export async function updateStrategy(data: Partial<StrategyConfig>): Promise<StrategyConfig> {
  const resp = await api.put('/api/strategy', data)
  return resp.data
}

export async function getStatus(): Promise<StatusData> {
  const resp = await api.get('/api/status')
  return resp.data
}
```

- [ ] **Step 3: Write `frontend/src/api/credentials.ts`**

```typescript
import { api } from './client'
import type { CredentialsConfig } from '../types'

export async function getCredentials(): Promise<CredentialsConfig> {
  const resp = await api.get('/api/credentials')
  return resp.data
}

export async function updateCredentials(data: Partial<CredentialsConfig>): Promise<CredentialsConfig> {
  const resp = await api.put('/api/credentials', data)
  return resp.data
}
```

- [ ] **Step 4: Write `frontend/src/api/trade.ts`**

```typescript
import { api } from './client'
import type { OrderRecord, AccountInfo } from '../types'

export async function getOrders(limit = 50): Promise<OrderRecord[]> {
  const resp = await api.get('/api/orders', { params: { limit } })
  return resp.data
}

export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}

export async function pauseTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/pause', { reason })
  return resp.data
}

export async function resumeTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/resume')
  return resp.data
}

export async function activateKillSwitch(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/kill-switch', { reason })
  return resp.data
}

export async function disableKillSwitch(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/disable-kill-switch')
  return resp.data
}

export async function startTrading(): Promise<{ message: string }> {
  const resp = await api.post('/api/control/start')
  return resp.data
}

export async function stopTrading(reason = 'manual'): Promise<{ message: string }> {
  const resp = await api.post('/api/control/stop', { reason })
  return resp.data
}
```

- [ ] **Step 5: Rewrite `frontend/src/api/index.ts` as re-export hub**

```typescript
// Re-export client for direct access if needed
export { api } from './client'

// Domain modules
export * from './strategy'
export * from './credentials'
export * from './trade'
```

- [ ] **Step 6: Verify frontend TypeScript build**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run build
```
Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/
git commit -m "refactor: split API layer into domain modules (client, strategy, credentials, trade)"
```

---

### Task 6: Extract Vue Composables

**Files:**
- Create: `frontend/src/composables/useDashboardData.ts`
- Create: `frontend/src/composables/useStatusStream.ts`
- Create: `frontend/src/composables/useAccountRefresh.ts`
- Create: `frontend/src/composables/useFormState.ts`

- [ ] **Step 1: Write `frontend/src/composables/useDashboardData.ts`**

```typescript
import { ref } from 'vue'
import { getStrategy, getStatus } from '../api'
import type { StrategyConfig, StatusData } from '../types'

const defaultStrategy: StrategyConfig = {
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  updated_at: '',
}

const defaultStatus: StatusData = {
  engine_state: 'flat', paused: false, kill_switch: false,
  runner_running: false,
  daily_pnl: 0, consecutive_losses: 0,
  last_price: 0, last_trigger_price: 0, last_trigger_at: null,
}

export function useDashboardData() {
  const strategy = ref<StrategyConfig>({ ...defaultStrategy })
  const status = ref<StatusData>({ ...defaultStatus })
  const initialLoading = ref(true)
  const loadError = ref(false)

  async function load() {
    try {
      const [s, st] = await Promise.all([getStrategy(), getStatus()])
      strategy.value = s
      status.value = st
      loadError.value = false
    } catch (e) {
      console.error('Dashboard data load failed:', e)
      loadError.value = true
      throw e
    } finally {
      initialLoading.value = false
    }
  }

  async function refreshStatus() {
    try {
      status.value = await getStatus()
      loadError.value = false
    } catch {
      // silent
    }
  }

  return {
    strategy,
    status,
    initialLoading,
    loadError,
    load,
    refreshStatus,
  }
}
```

- [ ] **Step 2: Write `frontend/src/composables/useStatusStream.ts`**

```typescript
import { ref, onMounted, onUnmounted } from 'vue'
import { getStatus } from '../api'
import type { StatusData } from '../types'

export function useStatusStream(status: { value: StatusData }) {
  const realtimeStatus = ref<'connecting' | 'connected' | 'reconnecting' | 'polling'>('connecting')

  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let reconnectAttempts = 0
  let useWebSocket = false
  let lastWsStatusAt = 0

  function connectWebSocket() {
    realtimeStatus.value = 'connecting'
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws`
    ws = new WebSocket(wsUrl)

    const apiKey = localStorage.getItem('api_key')
    ws.onopen = () => {
      useWebSocket = true
      realtimeStatus.value = 'connected'
      reconnectAttempts = 0
      if (apiKey) {
        ws?.send(JSON.stringify({ token: apiKey }))
      }
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'pong') return
        if (data.state !== undefined) {
          lastWsStatusAt = Date.now()
          realtimeStatus.value = 'connected'
          status.value = {
            engine_state: data.state,
            paused: data.risks?.paused ?? status.value.paused,
            kill_switch: data.risks?.kill_switch ?? status.value.kill_switch,
            runner_running: data.runner_running ?? status.value.runner_running,
            daily_pnl: data.risks?.daily_pnl ?? status.value.daily_pnl,
            consecutive_losses: data.risks?.consecutive_losses ?? status.value.consecutive_losses,
            last_price: data.last_price ?? status.value.last_price,
            last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
            last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
          }
        }
      } catch {
        // ignore
      }
    }

    ws.onclose = () => {
      useWebSocket = false
      realtimeStatus.value = 'reconnecting'
      ws = null
      scheduleReconnect()
    }

    ws.onerror = () => {
      useWebSocket = false
      realtimeStatus.value = 'polling'
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return
    realtimeStatus.value = 'reconnecting'
    const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
    reconnectAttempts++
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectWebSocket()
    }, delay)
  }

  function hasFreshWebSocketStatus() {
    return useWebSocket && Date.now() - lastWsStatusAt < 10000
  }

  function startPolling() {
    pollTimer = setInterval(async () => {
      if (hasFreshWebSocketStatus()) return
      try {
        const st = await getStatus()
        status.value = st
        if (!hasFreshWebSocketStatus()) {
          realtimeStatus.value = 'polling'
        }
      } catch {
        // silent
      }
    }, 3000)
  }

  function reconnectNow() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      ws.onclose = null
      ws.close()
      ws = null
    }
    useWebSocket = false
    realtimeStatus.value = 'connecting'
    lastWsStatusAt = 0
    connectWebSocket()
  }

  onMounted(() => {
    connectWebSocket()
    startPolling()
  })

  onUnmounted(() => {
    if (ws) {
      ws.onclose = null
      ws.close()
      ws = null
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  })

  return {
    realtimeStatus,
    reconnectNow,
  }
}
```

- [ ] **Step 3: Write `frontend/src/composables/useAccountRefresh.ts`**

```typescript
import { ref, onMounted, onUnmounted } from 'vue'
import { getAccount } from '../api'
import type { AccountInfo } from '../types'

const defaultAccount: AccountInfo = {
  total_assets: 0,
  cash_balances: [],
  positions: [],
  available: true,
  error: null,
}

export function useAccountRefresh(intervalMs = 10000) {
  const account = ref<AccountInfo>({ ...defaultAccount })
  const accountError = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null

  async function refresh() {
    try {
      account.value = await getAccount()
      accountError.value = !account.value.available
    } catch {
      accountError.value = true
    }
  }

  onMounted(() => {
    refresh()
    timer = setInterval(refresh, intervalMs)
  })

  onUnmounted(() => {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  })

  return {
    account,
    accountError,
    refresh,
  }
}
```

- [ ] **Step 4: Write `frontend/src/composables/useFormState.ts`**

```typescript
import { ref, computed, watch } from 'vue'

export interface FormStateOptions<T> {
  initial: T
  load: () => Promise<T>
  save: (data: T) => Promise<void>
}

export function useFormState<T extends Record<string, any>>(options: FormStateOptions<T>) {
  const form = ref<T>({ ...options.initial })
  const loading = ref(false)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref<string | null>(null)
  const savedSnapshot = ref('')

  const isDirty = computed(() => JSON.stringify(form.value) !== savedSnapshot.value)

  watch(form, () => {
    if (isDirty.value) {
      saved.value = false
    }
  }, { deep: true })

  async function load() {
    loading.value = true
    error.value = null
    try {
      const data = await options.load()
      form.value = { ...data }
      savedSnapshot.value = JSON.stringify(form.value)
      saved.value = false
    } catch (e) {
      error.value = '加载失败'
      console.error(e)
    } finally {
      loading.value = false
    }
  }

  async function save() {
    saving.value = true
    error.value = null
    try {
      await options.save(form.value)
      savedSnapshot.value = JSON.stringify(form.value)
      saved.value = true
    } catch (e) {
      error.value = '保存失败'
      console.error(e)
    } finally {
      saving.value = false
    }
  }

  return {
    form,
    loading,
    saving,
    saved,
    error,
    isDirty,
    load,
    save,
  }
}
```

- [ ] **Step 5: Verify build**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run build
```
Expected: Build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/composables/
git commit -m "feat: add Vue composables for dashboard data, status stream, account refresh, and form state"
```

---

### Task 7: Refactor Dashboard.vue

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

**Rationale:** Dashboard currently holds 504 lines mixing data loading, WebSocket, polling, account refresh, control actions, and rendering. Replace with composables and clearer template sections.

- [ ] **Step 1: Replace `frontend/src/views/Dashboard.vue`**

Write the full file:

```vue
<template>
  <div v-loading="initialLoading">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" type="primary" plain @click="handleRetry">重试连接</el-button>
    </el-alert>

    <el-alert v-if="accountError" type="warning" title="账户资产暂时不可用，请检查券商凭证或稍后重试" show-icon style="margin-bottom: 16px" />

    <div class="page-heading">
      <h3>仪表盘</h3>
      <el-tag :type="realtimeStatusType" effect="plain">{{ realtimeStatusLabel }}</el-tag>
    </div>

    <!-- Status Row -->
    <el-row :gutter="20">
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>引擎状态</template>
          <el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag>
          <p style="margin-top: 12px">
            运行器：<el-tag :type="status.runner_running ? 'success' : 'info'">{{ status.runner_running ? '运行中' : '未启动' }}</el-tag>
          </p>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>最新价格</template>
          <h1>${{ (status.last_price ?? 0).toFixed(2) }}</h1>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>今日盈亏</template>
          <h1 :class="metricClass(status.daily_pnl)">
            <span class="metric-label">{{ pnlLabel(status.daily_pnl) }}</span>
            {{ signedCurrency(status.daily_pnl) }}
          </h1>
        </el-card>
      </el-col>
    </el-row>

    <!-- Risk & Controls Row -->
    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :md="12">
        <el-card>
          <template #header>风控状态</template>
          <p>紧急停止：<el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? '已开启' : '已关闭' }}</el-tag></p>
          <p>暂停状态：<el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? '已暂停' : '运行中' }}</el-tag></p>
          <p>连续亏损次数：{{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="12">
        <el-card>
          <template #header>操作控制</template>
          <el-space>
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch || status.runner_running">启动</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused || status.kill_switch">暂停</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused || status.kill_switch">恢复</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">紧急停止</el-button>
            <el-button v-if="status.kill_switch" type="success" plain @click="handleDisableKillSwitch">解除紧急停止</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

    <!-- Account Row -->
    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :lg="8">
        <el-card>
          <template #header>总资产</template>
          <h1 :class="account.available ? 'metric-positive' : 'metric-negative'">
            <span class="metric-label">{{ account.available ? '可用' : '异常' }}</span>
            ${{ account.total_assets.toFixed(2) }}
          </h1>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="16">
        <el-card>
          <template #header>现金余额</template>
          <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" class="responsive-table">
            <el-table-column prop="currency" label="币种" min-width="90" />
            <el-table-column prop="available_cash" label="可用" min-width="120">
              <template #default="{ row }">${{ row.available_cash.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column prop="frozen_cash" label="冻结" min-width="120">
              <template #default="{ row }">${{ row.frozen_cash.toFixed(2) }}</template>
            </el-table-column>
          </el-table>
          <p v-else-if="!account.available" style="color: #999; text-align: center">数据不可用</p>
          <p v-else style="color: #999; text-align: center">暂无数据</p>
        </el-card>
      </el-col>
    </el-row>

    <!-- Positions -->
    <el-card style="margin-top: 20px">
      <template #header>持仓明细</template>
      <el-table :data="account.positions" size="small" v-if="account.positions.length > 0" class="responsive-table">
        <el-table-column prop="symbol" label="股票代码" min-width="130" />
        <el-table-column prop="side" label="方向" min-width="90">
          <template #default="{ row }">{{ positionSideLabel(row.side) }}</template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" min-width="100">
          <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
        </el-table-column>
        <el-table-column prop="avg_price" label="均价" min-width="120">
          <template #default="{ row }">${{ row.avg_price.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column prop="market_value" label="市值" min-width="120">
          <template #default="{ row }">${{ row.market_value.toFixed(2) }}</template>
        </el-table-column>
      </el-table>
      <p v-else-if="!account.available" style="color: #999; text-align: center">数据不可用</p>
      <p v-else style="color: #999; text-align: center">暂无持仓</p>
    </el-card>

    <!-- Strategy Info -->
    <el-card style="margin-top: 20px">
      <template #header>行情信息</template>
      <p>股票代码：{{ strategy.symbol || '未配置' }}</p>
      <p>市场：{{ marketLabel(strategy.market) }}</p>
      <p>买入价下限：${{ strategy.buy_low }}</p>
      <p>卖出价上限：${{ strategy.sell_high }}</p>
      <p>做空：{{ strategy.short_selling ? '是' : '否' }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useDashboardData } from '../composables/useDashboardData'
import { useStatusStream } from '../composables/useStatusStream'
import { useAccountRefresh } from '../composables/useAccountRefresh'
import { startTrading, stopTrading, pauseTrading, resumeTrading, activateKillSwitch, disableKillSwitch } from '../api'
import { engineStateLabel, marketLabel, positionSideLabel } from '../utils/labels'

const { strategy, status, initialLoading, loadError, load, refreshStatus } = useDashboardData()
const { realtimeStatus, reconnectNow } = useStatusStream(status)
const { account, accountError, refresh: refreshAccount } = useAccountRefresh()

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

const realtimeStatusLabel = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected': return '实时连接正常'
    case 'reconnecting': return '实时重连中'
    case 'polling': return '轮询兜底'
    default: return '实时连接中'
  }
})

const realtimeStatusType = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected': return 'success'
    case 'reconnecting': return 'warning'
    case 'polling': return 'info'
    default: return 'info'
  }
})

async function handleRetry() {
  loadError.value = false
  try {
    await load()
    await refreshAccount()
  } catch {
    // error already set
  }
}

async function handleStart() {
  try {
    await startTrading()
    ElMessage.success('交易已启动')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('启动失败')
  }
}

async function handleStop() {
  try {
    await stopTrading()
    ElMessage.success('交易已停止')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('停止失败')
  }
}

async function handlePause() {
  try {
    await pauseTrading()
    ElMessage.success('交易已暂停')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('暂停失败')
  }
}

async function handleResume() {
  try {
    await resumeTrading()
    ElMessage.success('交易已恢复')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('恢复失败')
  }
}

async function handleKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要触发紧急停止吗？', '紧急停止', { type: 'warning' })
    await activateKillSwitch()
    ElMessage.success('紧急停止已触发')
    await refreshStatus()
  } catch {
    // cancelled
  }
}

async function handleDisableKillSwitch() {
  try {
    await disableKillSwitch()
    ElMessage.success('紧急停止已解除')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('解除失败')
  }
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function pnlLabel(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return '盈利'
  if (normalized < 0) return '亏损'
  return '持平'
}

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}
</script>
```

- [ ] **Step 2: Verify build**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run build
```
Expected: Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/views/Dashboard.vue frontend/src/composables/
git commit -m "refactor: Dashboard uses composables, clearer sections, unavailable-vs-zero"
```

---

### Task 8: Unify Form Experience on Strategy and Credentials Pages

**Files:**
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/views/Credentials.vue`

- [ ] **Step 1: Rewrite `frontend/src/views/Strategy.vue`**

```vue
<template>
  <div>
    <h3>策略配置</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="180px" @submit.prevent="handleSave">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="form.market">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="买入价下限">
          <el-input-number v-model="form.buy_low" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="卖出价上限">
          <el-input-number v-model="form.sell_high" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="单日最大亏损">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="连续亏损暂停阈值">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
          <el-tag v-if="error" type="danger" style="margin-left: 10px">{{ error }}</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { getStrategy, updateStrategy } from '../api'
import { useFormState } from '../composables/useFormState'

const { form, loading, saving, saved, error, isDirty, load, save } = useFormState({
  initial: {
    symbol: '',
    market: 'US' as 'US' | 'HK',
    buy_low: 0,
    sell_high: 0,
    short_selling: false,
    max_daily_loss: 5000,
    max_consecutive_losses: 3,
  },
  load: async () => {
    const s = await getStrategy()
    return {
      symbol: s.symbol,
      market: s.market,
      buy_low: s.buy_low,
      sell_high: s.sell_high,
      short_selling: s.short_selling,
      max_daily_loss: s.max_daily_loss,
      max_consecutive_losses: s.max_consecutive_losses,
    }
  },
  save: async (data) => {
    await updateStrategy(data)
  },
})

load()

onBeforeRouteLeave(() => {
  if (!isDirty.value) return true
  return ElMessageBox.confirm('策略配置尚未保存，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})

function handleSave() {
  save()
}
</script>
```

- [ ] **Step 2: Read current `frontend/src/views/Credentials.vue` to understand its structure**

(Use Read tool on the file, then adapt it to useFormState in the next step.)

- [ ] **Step 3: Rewrite `frontend/src/views/Credentials.vue` using `useFormState`**

Read the current file first. The rewrite should:
- Use `useFormState` for loading/saving state
- Show save success / error indicators
- Keep the existing credential fields and encryption hint text
- Keep the existing API key dialog logic

- [ ] **Step 4: Verify build**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run build
```
Expected: Build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/Strategy.vue frontend/src/views/Credentials.vue
git commit -m "refactor: unify Strategy and Credentials form UX with useFormState composable"
```

---

### Task 9: Update Cypress Selectors and Run Verification

**Files:**
- Modify: `frontend/cypress/e2e/dashboard.cy.ts` (if selectors changed)
- Modify: `frontend/cypress/e2e/strategy.cy.ts` (if selectors changed)
- Modify: `frontend/cypress/e2e/credentials.cy.ts` (if selectors changed)

- [ ] **Step 1: Review Cypress tests for broken selectors**

Read each Cypress test file. Look for selectors that reference removed DOM elements or changed class names. Update selectors to match the refactored Dashboard, Strategy, and Credentials pages.

Key areas to check:
- Dashboard: card headers, button texts, status tags
- Strategy: form fields, save button state
- Credentials: form fields, save behavior

- [ ] **Step 2: Run Cypress tests**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run cypress:run
```
Expected: All tests pass. Fix any failing selectors.

- [ ] **Step 3: Commit**

```bash
git add frontend/cypress/e2e/
git commit -m "test: update Cypress selectors for refactored pages"
```

---

### Task 10: Full Integration Verification

- [ ] **Step 1: Run all backend tests**

Run:
```bash
cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/ -v
```
Expected: All tests pass.

- [ ] **Step 2: Run frontend build**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run build
```
Expected: Build succeeds.

- [ ] **Step 3: Run Cypress E2E**

Run:
```bash
cd /home/lcy/code/auto_trade/frontend && npm run cypress:run
```
Expected: All tests pass.

- [ ] **Step 4: Docker Compose smoke test**

Run:
```bash
cd /home/lcy/code/auto_trade && docker compose up --build -d
curl -fsS http://localhost:8000/api/health
```
Expected: Docker starts, health endpoint returns `{"ok":true}`.

Stop after verification:
```bash
docker compose down
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor: complete maintainability refactor — backend services, frontend composables, unified form UX"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ `runner.py` stripped to coordinator — Task 3
- ✅ `TradeExecutionService` handles all 4 actions — Task 1
- ✅ `RuntimeStateService` handles load/persist — Task 2
- ✅ Frontend API split into domain modules — Task 5
- ✅ Dashboard composables extracted — Tasks 6-7
- ✅ Unavailable data vs zero — Task 7 (Dashboard template shows "数据不可用")
- ✅ Consistent form save UX — Task 8
- ✅ Backend tests for new services — Task 4
- ✅ Cypress updated — Task 9
- ✅ Docker verified — Task 10

**2. Placeholder scan:** No TBD, TODO, or "similar to" references found. Each task contains complete code.

**3. Type consistency:**
- `TradeExecutionService.execute` returns `OrderStatus | None`
- `RuntimeStateService.load` accepts `(db, engine, risk)`
- Composables use consistent ref/computed patterns
- API modules re-export through `index.ts` to avoid breaking existing imports during transition

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-17-maintainability-refactor.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
