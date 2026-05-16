# Maintainability and Frontend Experience Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split backend orchestration and frontend page state into clearer modules while improving Dashboard and form UX.

**Architecture:** Extract backend trade execution, runtime state, and account read behavior into focused services so `AppRunner` coordinates rather than implements every detail. Split frontend API calls by domain and move Dashboard streaming/loading/form mechanics into composables while preserving Vue 3 + Element Plus.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, pytest, Vue 3, TypeScript, Element Plus, Vite, Cypress, Docker Compose.

---

## Working Directory

All implementation work happens in:

`/home/lcy/code/auto_trade/.worktrees/maintainability-frontend`

Baseline already verified there:

- Backend: `.venv/bin/python -m pytest tests/ -q` -> `134 passed`
- Frontend: `npm run build` -> pass with existing Vite chunk-size warning

Do not commit unless the user explicitly asks for a commit. This overrides the usual frequent-commit plan pattern for this environment.

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `.gitignore` | Ignore `.superpowers/` and `.worktrees/` local artifacts |
| Create | `docs/superpowers/specs/2026-05-16-maintainability-frontend-refactor-design.md` | Approved design record in the implementation branch |
| Create | `backend/app/services/trade_execution_service.py` | Execute strategy actions and encapsulate order persistence/notification |
| Create | `backend/app/services/runtime_state_service.py` | Load and persist engine/risk runtime state |
| Create | `backend/app/services/account_service.py` | Build account response data outside route handlers |
| Modify | `backend/app/runner.py` | Delegate execution and persistence to services |
| Modify | `backend/app/api/trade.py` | Keep routes thin and delegate account response building |
| Modify | `backend/app/main.py` | Fix undefined lifespan logger and keep startup failure visible |
| Create | `backend/tests/test_trade_execution_service.py` | Unit tests for action execution behavior |
| Create | `backend/tests/test_runtime_state_service.py` | Unit tests for load/persist service behavior |
| Create | `backend/tests/test_account_service.py` | Unit tests for account response composition |
| Modify | `backend/tests/test_runner.py` | Verify runner delegates to services and preserves rollback behavior |
| Create | `frontend/src/api/client.ts` | Shared axios client, API key injection, 401 event handling |
| Create | `frontend/src/api/strategy.ts` | Strategy and status API calls |
| Create | `frontend/src/api/credentials.ts` | Credential API calls |
| Create | `frontend/src/api/trade.ts` | Account, orders, and control API calls |
| Modify | `frontend/src/api/index.ts` | Re-export domain API modules |
| Create | `frontend/src/composables/useDashboardData.ts` | Initial Dashboard loading and refresh orchestration |
| Create | `frontend/src/composables/useStatusStream.ts` | WebSocket plus polling fallback state |
| Create | `frontend/src/composables/useFormSaveState.ts` | Shared form loading/saving/saved/dirty/error state |
| Modify | `frontend/src/views/Dashboard.vue` | Render clearer sections and explicit unavailable states |
| Modify | `frontend/src/views/Strategy.vue` | Use shared save state and clearer form behavior |
| Modify | `frontend/src/views/Credentials.vue` | Make preserve vs clear credential behavior explicit |
| Modify | `frontend/cypress/e2e/*.cy.ts` | Update key flow assertions after UI structure changes |
| Modify | `README.md` | Document data reset and updated verification commands |

---

### Task 1: Backend Trade Execution Service

**Files:**
- Create: `backend/tests/test_trade_execution_service.py`
- Create: `backend/app/services/trade_execution_service.py`
- Modify: `backend/app/services/__init__.py`

- [ ] **Step 1: Write failing service tests**

Create `backend/tests/test_trade_execution_service.py`:

```python
from __future__ import annotations

from decimal import Decimal

from app.core.broker import OrderResult, Position, Quote
from app.core.risk import RiskController
from app.services.trade_execution_service import TradeExecutionService


class FakeBroker:
    def __init__(self) -> None:
        self.cash = Decimal("1000")
        self.positions: list[Position] = []
        self.orders: list[tuple[str, str, Decimal, Decimal]] = []

    def get_cash(self) -> Decimal:
        return self.cash

    def get_positions(self) -> list[Position]:
        return self.positions

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        self.orders.append((symbol, side, quantity, price))
        return OrderResult("order-1", symbol, side, quantity, price, "SUBMITTED")


class FakeNotifier:
    def __init__(self) -> None:
        self.orders: list[tuple[str, str, str, str, str]] = []
        self.risks: list[tuple[str, str]] = []

    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool:
        self.orders.append((side, symbol, quantity, price, order_id))
        return True

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        self.risks.append((event_type, reason))
        return True


def test_buy_uses_available_cash_and_records_order() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    recorded: list[tuple[str, str, str, float, float]] = []
    service = TradeExecutionService(record_order=lambda *args: recorded.append(args), record_risk_event=lambda reason: None)

    executed = service.execute("BUY", "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""), broker, RiskController(), notifier)

    assert executed is True
    assert broker.orders == [("AAPL.US", "BUY", Decimal("9"), Decimal("100.0"))]
    assert recorded == [("order-1", "AAPL.US", "BUY", 9.0, 100.0)]
    assert notifier.orders[0][0] == "BUY"


def test_sell_requires_matching_long_position() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=lambda reason: None)

    executed = service.execute("SELL", "AAPL.US", Quote("AAPL.US", 200.0, 199.5, 200.5, ""), broker, RiskController(), notifier)

    assert executed is False
    assert broker.orders == []


def test_sell_records_realized_pnl_for_matching_position() -> None:
    broker = FakeBroker()
    broker.positions = [Position("AAPL.US", "LONG", Decimal("3"), Decimal("150"))]
    risk = RiskController()
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=lambda reason: None)

    executed = service.execute("SELL", "AAPL.US", Quote("AAPL.US", 200.0, 199.5, 200.5, ""), broker, risk, FakeNotifier())

    assert executed is True
    assert broker.orders == [("AAPL.US", "SELL", Decimal("3"), Decimal("200.0"))]
    assert risk.daily_pnl == 150.0


def test_risk_rejection_records_event_without_order() -> None:
    broker = FakeBroker()
    notifier = FakeNotifier()
    risk = RiskController()
    risk.pause("maintenance")
    reasons: list[str] = []
    service = TradeExecutionService(record_order=lambda *args: None, record_risk_event=reasons.append)

    executed = service.execute("BUY", "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, ""), broker, risk, notifier)

    assert executed is False
    assert reasons == ["trading is paused"]
    assert notifier.risks == [("REJECTED", "trading is paused")]
    assert broker.orders == []
```

- [ ] **Step 2: Run test to verify failure**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/test_trade_execution_service.py -q`

Expected: fail with `ModuleNotFoundError: No module named 'app.services.trade_execution_service'`.

- [ ] **Step 3: Implement minimal service**

Create `backend/app/services/trade_execution_service.py` by moving the existing `_execute_buy`, `_execute_sell`, `_execute_sell_short`, `_execute_buy_to_cover`, `_safe_record_order`, and `_safe_notify_order` logic out of `AppRunner`. Keep method names private inside the service and expose `execute(action, symbol, quote, broker, risk, notifier) -> bool`.

The public constructor should be:

```python
class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float], None],
        record_risk_event: Callable[[str], None],
    ) -> None:
        self._record_order = record_order
        self._record_risk_event = record_risk_event
```

The public method should dispatch exactly these actions: `BUY`, `SELL`, `SELL_SHORT`, and `BUY_TO_COVER`. Unknown actions return `False` and log a warning.

- [ ] **Step 4: Run service tests**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/test_trade_execution_service.py -q`

Expected: all tests pass.

---

### Task 2: Backend Runtime State Service

**Files:**
- Create: `backend/tests/test_runtime_state_service.py`
- Create: `backend/app/services/runtime_state_service.py`
- Modify: `backend/app/runner.py`

- [ ] **Step 1: Write failing runtime state tests**

Create `backend/tests/test_runtime_state_service.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from app.core.engine import EngineState, StrategyEngine
from app.core.risk import RiskController
from app.services.runtime_state_service import RuntimeStateService


class FakeStrategyService:
    def __init__(self) -> None:
        self.persisted: dict[str, object] | None = None

    def get_config(self) -> object:
        return SimpleNamespace(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=200.0, short_selling=True, max_daily_loss=1234.0, max_consecutive_losses=2)

    def get_runtime_state(self) -> object:
        return SimpleNamespace(engine_state="short", last_price=150.0, last_trigger_price=201.0, last_trigger_at=None, daily_pnl=-50.0, consecutive_losses=1, kill_switch=True, paused=True)

    def update_runtime_state(self, **kwargs: object) -> object:
        self.persisted = kwargs
        return SimpleNamespace(**kwargs)


def test_load_applies_config_and_runtime_state() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    engine = StrategyEngine()
    risk = RiskController()

    service.load(strategy_service, engine, risk)

    assert engine.params.symbol == "AAPL.US"
    assert engine.params.short_selling is True
    assert engine.state == EngineState.SHORT
    assert engine.last_price == 150.0
    assert risk.config.max_daily_loss == 1234.0
    assert risk.consecutive_losses == 1
    assert risk.kill_switch is True
    assert risk.paused is True


def test_load_invalid_engine_state_defaults_to_flat() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    strategy_service.get_runtime_state = lambda: SimpleNamespace(engine_state="broken", last_price=1.0, last_trigger_price=0.0, last_trigger_at=None, daily_pnl=0.0, consecutive_losses=0, kill_switch=False, paused=False)
    engine = StrategyEngine()

    service.load(strategy_service, engine, RiskController())

    assert engine.state == EngineState.FLAT


def test_persist_writes_current_snapshot() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    engine = StrategyEngine()
    risk = RiskController()
    engine.state = EngineState.LONG
    engine.last_price = 188.0
    risk.daily_pnl = 42.0
    risk.consecutive_losses = 0
    risk.paused = False

    service.persist(strategy_service, engine, risk)

    assert strategy_service.persisted is not None
    assert strategy_service.persisted["engine_state"] == "long"
    assert strategy_service.persisted["last_price"] == 188.0
    assert strategy_service.persisted["daily_pnl"] == 42.0
```

- [ ] **Step 2: Run test to verify failure**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/test_runtime_state_service.py -q`

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement runtime state service**

Create `backend/app/services/runtime_state_service.py` with `RuntimeStateService.load(strategy_service, engine, risk)` and `RuntimeStateService.persist(strategy_service, engine, risk)`. Move the state assignment from `AppRunner._initialize_runner` and the snapshot write from `AppRunner._persist_state` into this service.

- [ ] **Step 4: Run runtime tests**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/test_runtime_state_service.py -q`

Expected: all tests pass.

---

### Task 3: Runner Integration and Thin Account API

**Files:**
- Create: `backend/tests/test_account_service.py`
- Create: `backend/app/services/account_service.py`
- Modify: `backend/app/runner.py`
- Modify: `backend/app/api/trade.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_runner.py`
- Modify: `backend/tests/test_account_api.py`

- [ ] **Step 1: Add account service tests**

Create `backend/tests/test_account_service.py`:

```python
from __future__ import annotations

from decimal import Decimal

from app.core.broker import AccountInfo, CashBalance, NetAsset, Position, Quote
from app.services.account_service import AccountService


class FakeBroker:
    def get_account(self) -> AccountInfo:
        return AccountInfo(Decimal("1000"), [CashBalance("USD", Decimal("900"), Decimal("100"))], [NetAsset("USD", Decimal("1000"))])

    def get_positions(self) -> list[Position]:
        return [Position("AAPL.US", "LONG", Decimal("2"), Decimal("150"))]

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol, 200.0, 199.0, 201.0, "")


def test_account_service_builds_account_response() -> None:
    response = AccountService(FakeBroker()).get_account_response()

    assert response.total_assets == 1000.0
    assert response.cash_balances[0].available_cash == 900.0
    assert response.positions[0].market_value == 400.0
```

- [ ] **Step 2: Implement account service and thin route**

Create `backend/app/services/account_service.py`. Move the account and positions response-building logic from `backend/app/api/trade.py` into `AccountService.get_account_response()`. Update `trade.py` so `/api/account` only does:

```python
broker = get_runner().broker
return AccountService(broker).get_account_response()
```

- [ ] **Step 3: Integrate services into runner**

Modify `backend/app/runner.py`:

- Instantiate `RuntimeStateService` and `TradeExecutionService` in `AppRunner.__init__`.
- Replace direct config/runtime state assignment in `_initialize_runner` with `self.runtime_state.load(...)`.
- Replace `_handle_trigger` action-specific method calls with `self.trade_execution.execute(...)`.
- Keep `_record_order` and `_record_risk_event` in runner as persistence callbacks.
- Remove action-specific execution methods from runner after tests pass.
- Keep rollback behavior in `_on_quote` unchanged.

- [ ] **Step 4: Fix lifespan logger**

Modify `backend/app/main.py` to define:

```python
logger = logging.getLogger("auto_trade.main")
```

near the existing logging setup.

- [ ] **Step 5: Run focused backend tests**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/test_trade_execution_service.py tests/test_runtime_state_service.py tests/test_account_service.py tests/test_runner.py tests/test_runner_credentials.py tests/test_account_api.py -q`

Expected: all tests pass.

---

### Task 4: Frontend API Module Split

**Files:**
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/strategy.ts`
- Create: `frontend/src/api/credentials.ts`
- Create: `frontend/src/api/trade.ts`
- Modify: `frontend/src/api/index.ts`

- [ ] **Step 1: Split API client without changing imports**

Create `client.ts` with the current axios instance, request interceptor, response interceptor, and 401 dispatch logic. Export the axios instance as `api`.

Create domain modules with these exact exported functions:

```ts
// strategy.ts
export async function getStrategy(): Promise<StrategyConfig>
export async function updateStrategy(data: Partial<StrategyConfig>): Promise<StrategyConfig>
export async function getStatus(): Promise<StatusData>

// credentials.ts
export async function getCredentials(): Promise<CredentialsConfig>
export async function updateCredentials(data: Partial<CredentialsConfig>): Promise<CredentialsConfig>

// trade.ts
export async function getOrders(limit = 50): Promise<OrderRecord[]>
export async function pauseTrading(reason = 'manual'): Promise<{ message: string }>
export async function resumeTrading(): Promise<{ message: string }>
export async function activateKillSwitch(reason = 'manual'): Promise<{ message: string }>
export async function startTrading(): Promise<{ message: string }>
export async function stopTrading(reason = 'manual'): Promise<{ message: string }>
export async function getAccount(): Promise<AccountInfo>
```

Replace `index.ts` with re-exports:

```ts
export * from './strategy'
export * from './credentials'
export * from './trade'
```

- [ ] **Step 2: Run frontend build**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/frontend && npm run build`

Expected: build passes with the existing Vite chunk-size warning only.

---

### Task 5: Dashboard Composables and UI Sections

**Files:**
- Create: `frontend/src/composables/useStatusStream.ts`
- Create: `frontend/src/composables/useDashboardData.ts`
- Modify: `frontend/src/views/Dashboard.vue`

- [ ] **Step 1: Extract status stream**

Create `useStatusStream.ts` with:

```ts
export type ConnectionMode = 'connecting' | 'websocket' | 'polling' | 'disconnected'

export function useStatusStream(status: Ref<StatusData>) {
  const connectionMode = ref<ConnectionMode>('connecting')
  function connectWebSocket(): void
  function startPolling(): void
  function stop(): void
  return { connectionMode, connectWebSocket, startPolling, stop }
}
```

Move existing WebSocket, reconnect, and polling logic from `Dashboard.vue` into the composable. Preserve API key auth message behavior and exponential reconnect delay.

- [ ] **Step 2: Extract dashboard data loading**

Create `useDashboardData.ts` with refs for `strategy`, `status`, `account`, `initialLoading`, `loadError`, `accountLoading`, and `accountError`. Export `loadInitial()`, `refresh()`, `refreshAccount()`, `startAccountRefresh()`, and `stopAccountRefresh()`.

Account failures must set `account.value = null` and `accountError.value = true` rather than silently rendering zero assets.

- [ ] **Step 3: Rework Dashboard template**

Update `Dashboard.vue` to render sections with visible headings:

- `连接状态`
- `策略状态`
- `账户摘要`
- `风控状态`
- `操作控制`
- `持仓明细`

For account unavailable state, render: `账户数据暂不可用` and a retry button that calls `refreshAccount()`.

- [ ] **Step 4: Run frontend build**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/frontend && npm run build`

Expected: build passes with the existing Vite chunk-size warning only.

---

### Task 6: Shared Form Save State and Credentials Clear UX

**Files:**
- Create: `frontend/src/composables/useFormSaveState.ts`
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/views/Credentials.vue`

- [ ] **Step 1: Create form save state composable**

Create `useFormSaveState.ts`:

```ts
import { ref } from 'vue'

export function useFormSaveState() {
  const loading = ref(true)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref<string | null>(null)
  function markDirty() { saved.value = false; error.value = null }
  function beginSave() { saving.value = true; saved.value = false; error.value = null }
  function saveSucceeded() { saving.value = false; saved.value = true }
  function saveFailed(message: string) { saving.value = false; saved.value = false; error.value = message }
  return { loading, saving, saved, error, markDirty, beginSave, saveSucceeded, saveFailed }
}
```

- [ ] **Step 2: Update Strategy form**

Use the composable in `Strategy.vue`. Preserve existing validation messages, but display `error` in an `el-alert` under the card when save fails.

- [ ] **Step 3: Update Credentials form**

Use the composable in `Credentials.vue`. Add explanatory text: `留空表示保留当前凭证；如需清除，请使用清除按钮。` Add per-field clear buttons for configured credentials that submit an empty string for that field only.

- [ ] **Step 4: Run frontend build**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/frontend && npm run build`

Expected: build passes with the existing Vite chunk-size warning only.

---

### Task 7: Cypress and Documentation Updates

**Files:**
- Modify: `frontend/cypress/e2e/dashboard.cy.ts`
- Modify: `frontend/cypress/e2e/credentials.cy.ts`
- Modify: `frontend/cypress/e2e/controls.cy.ts`
- Modify: `README.md`

- [ ] **Step 1: Update Cypress text assertions**

Update Dashboard tests to assert the new section headings: `连接状态`, `策略状态`, `账户摘要`, `风控状态`, `操作控制`, and `持仓明细`.

Update Credentials tests to assert the preserve/clear helper text and save button remain visible.

- [ ] **Step 2: Update README verification and data reset docs**

Add a short local reset note under development docs:

```md
### 重置本地开发数据

本项目当前不维护 SQLite 迁移。重构或模型变更后，如遇到旧数据导致的异常，可以停止服务并删除 `backend/data/auto_trade.db` 或 `.env` 中 `AUTO_TRADE_DATABASE_URL` 指向的 SQLite 文件，然后重新启动服务。
```

Update backend test command to use the project venv where appropriate:

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
```

- [ ] **Step 3: Run Cypress when services are available**

Run from `frontend` after starting the app stack: `npm run cypress:run`

Expected: dashboard, strategy, credentials, controls, history, and navigation specs pass.

---

### Task 8: Full Verification

**Files:**
- No new files unless verification reveals defects.

- [ ] **Step 1: Run backend suite**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/backend && .venv/bin/python -m pytest tests/ -q`

Expected: all tests pass.

- [ ] **Step 2: Run frontend build**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend/frontend && npm run build`

Expected: build passes with only existing Vite/Rollup warnings.

- [ ] **Step 3: Run Docker Compose build and startup**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend && docker compose up --build -d`

Expected: backend and frontend containers start.

- [ ] **Step 4: Check health endpoint**

Run: `curl -f http://localhost:8000/api/health`

Expected response contains `"ok":true`.

- [ ] **Step 5: Stop stack after verification**

Run: `cd /home/lcy/code/auto_trade/.worktrees/maintainability-frontend && docker compose down`

Expected: containers stop cleanly.

## Self-Review Notes

- Spec coverage: backend service boundaries, frontend API/composables, Dashboard unavailable data, form consistency, tests, Cypress, Docker verification are covered.
- Red-flag scan: no deferred implementation language is used.
- Type consistency: frontend API function names match current imports and planned re-export names; backend service names are consistent across tasks.
