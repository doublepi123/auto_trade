# Account Balance & Positions Display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display the current Longbridge account's cash balances and stock positions on the Dashboard page.

**Architecture:** Add a `GET /api/account` backend endpoint that queries `BrokerGateway` for account balance and positions, then return structured JSON. The frontend Dashboard fetches this data periodically and renders two new card rows: account summary (total assets + cash balances) and a positions table.

**Tech Stack:** Python/FastAPI (backend), Vue 3 + Element Plus + TypeScript (frontend), Longbridge OpenAPI SDK

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `backend/app/core/broker.py` | Add `AccountInfo`, `CashBalance`, `NetAsset` dataclasses; add `get_account()` method |
| Modify | `backend/app/schemas.py` | Add `CashBalanceSchema`, `PositionSchema`, `AccountResponse` |
| Modify | `backend/app/api/trade.py` | Add `GET /api/account` endpoint |
| Create | `backend/tests/test_account_api.py` | Tests for the new account endpoint and broker method |
| Modify | `frontend/src/types/index.ts` | Add `CashBalance`, `Position`, `AccountInfo` types |
| Modify | `frontend/src/api/index.ts` | Add `getAccount()` function |
| Modify | `frontend/src/utils/labels.ts` | Add `positionSideLabel()` helper |
| Modify | `frontend/src/views/Dashboard.vue` | Add account info + positions cards |

---

### Task 1: Add backend dataclasses and `get_account()` to broker

**Files:**
- Modify: `backend/app/core/broker.py`

- [ ] **Step 1: Add `CashBalance` and `AccountInfo` dataclasses to `broker.py`**

Add these dataclasses after the existing `Position` dataclass (around line 55):

```python
@dataclass
class CashBalance:
    currency: str
    available_cash: Decimal
    frozen_cash: Decimal


@dataclass
class NetAsset:
    currency: str
    amount: Decimal


@dataclass
class AccountInfo:
    total_assets: Decimal
    cash_balances: list[CashBalance]
    net_assets: list[NetAsset]
```

- [ ] **Step 2: Add `get_account()` method to `BrokerGateway`**

Add the method after the existing `get_cash()` method (after line 275):

```python
def get_account(self) -> AccountInfo:
    with self._lock:
        self._init_clients()
        try:
            response = self._trade_ctx.account_balance()
            cash_balances: list[CashBalance] = []
            net_assets: list[NetAsset] = []
            total_assets = Decimal("0")
            items = response if isinstance(response, list) else [response]
            primary_currency = ""
            primary_total = Decimal("0")

            for item in items:
                currency = str(getattr(item, "currency", ""))
                available = Decimal(str(getattr(item, "available_cash", getattr(item, "cash", "0"))))
                frozen = Decimal(str(getattr(item, "frozen_cash", getattr(item, "frozen_amounts", "0"))))
                net_amount = Decimal(str(getattr(item, "net_assets", "0")))
                cash_balances.append(CashBalance(
                    currency=currency,
                    available_cash=available,
                    frozen_cash=frozen,
                ))
                net_assets.append(NetAsset(
                    currency=currency,
                    amount=net_amount,
                ))
                if currency in ("USD", "HKD") and not primary_currency:
                    primary_currency = currency
                    primary_total = net_amount
                total_assets += net_amount

            if primary_currency:
                total_assets = primary_total

            return AccountInfo(
                total_assets=total_assets,
                cash_balances=cash_balances,
                net_assets=net_assets,
            )
        except Exception:
            logger.exception("failed to get account balance")
            raise
```

- [ ] **Step 3: Run existing broker tests to verify nothing is broken**

Run: `cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/test_broker.py -v`
Expected: All existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/broker.py
git commit -m "feat: add AccountInfo dataclass and get_account() to BrokerGateway"
```

---

### Task 2: Add schemas and API endpoint

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/trade.py`

- [ ] **Step 1: Add new response schemas to `schemas.py`**

Add after the `MessageResponse` class (around line 132):

```python
class CashBalanceSchema(BaseModel):
    currency: str
    available_cash: float
    frozen_cash: float


class PositionSchema(BaseModel):
    symbol: str
    side: str
    quantity: float
    avg_price: float
    market_value: float


class AccountResponse(BaseModel):
    total_assets: float
    cash_balances: list[CashBalanceSchema]
    positions: list[PositionSchema]
```

- [ ] **Step 2: Add `GET /api/account` endpoint to `trade.py`**

Add the import of new schemas at top of `backend/app/api/trade.py`:

```python
from app.schemas import AccountResponse, CashBalanceSchema, ControlRequest, MessageResponse, OrderResponse, PositionSchema
```

Add new imports after existing imports:

```python
from decimal import Decimal
```

Add the endpoint after the `get_orders` function (around line 23):

```python
@router.get("/account", response_model=AccountResponse, dependencies=[Depends(require_api_key())])
def get_account() -> AccountResponse:
    runner = get_runner()
    broker = runner.broker
    try:
        account = broker.get_account()
        total_assets = float(account.total_assets)
        cash_balances = [
            CashBalanceSchema(
                currency=cb.currency,
                available_cash=float(cb.available_cash),
                frozen_cash=float(cb.frozen_cash),
            )
            for cb in account.cash_balances
        ]
    except Exception:
        total_assets = 0.0
        cash_balances = []

    try:
        broker_positions = broker.get_positions()
        positions: list[PositionSchema] = []
        for pos in broker_positions:
            try:
                quote = broker.get_quote(pos.symbol)
                market_value = float(pos.quantity * Decimal(str(quote.last_price)))
            except Exception:
                market_value = float(pos.quantity * pos.avg_price)
            positions.append(PositionSchema(
                symbol=pos.symbol,
                side=pos.side,
                quantity=float(pos.quantity),
                avg_price=float(pos.avg_price),
                market_value=market_value,
            ))
    except Exception:
        positions = []

    return AccountResponse(
        total_assets=total_assets,
        cash_balances=cash_balances,
        positions=positions,
    )
```

- [ ] **Step 3: Run existing API tests to verify nothing is broken**

Run: `cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/test_api.py -v`
Expected: All existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas.py backend/app/api/trade.py
git commit -m "feat: add account balance & positions API endpoint"
```

---

### Task 3: Add backend unit tests

**Files:**
- Create: `backend/tests/test_account_api.py`

- [ ] **Step 1: Write tests for the account endpoint and broker `get_account()` method**

Create `backend/tests/test_account_api.py`:

```python
import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_account_api.db"


from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.broker import AccountInfo, BrokerGateway, CashBalance, NetAsset, Position
from app.database import engine as db_engine
from app.models import Base
from app.main import app

Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


class TestAccountDataclasses:
    def test_cash_balance(self) -> None:
        cb = CashBalance(currency="USD", available_cash=Decimal("10000"), frozen_cash=Decimal("500"))
        assert cb.currency == "USD"
        assert cb.available_cash == Decimal("10000")
        assert cb.frozen_cash == Decimal("500")

    def test_net_asset(self) -> None:
        na = NetAsset(currency="USD", amount=Decimal("15000"))
        assert na.currency == "USD"
        assert na.amount == Decimal("15000")

    def test_account_info(self) -> None:
        info = AccountInfo(
            total_assets=Decimal("15000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("10000"), frozen_cash=Decimal("500"))],
            net_assets=[NetAsset(currency="USD", amount=Decimal("15000"))],
        )
        assert info.total_assets == Decimal("15000")
        assert len(info.cash_balances) == 1
        assert len(info.net_assets) == 1


class TestBrokerGetAccount:
    def test_get_account_with_mock(self, monkeypatch) -> None:
        class FakeBalanceItem:
            currency = "USD"
            available_cash = "10000.50"
            frozen_cash = "500.00"
            net_assets = "15000.00"

        class FakeTradeContext:
            def account_balance(self):
                return [FakeBalanceItem()]

        gw = BrokerGateway()
        gw._quote_ctx = MagicMock()
        gw._trade_ctx = FakeTradeContext()

        result = gw.get_account()
        assert result.total_assets == Decimal("15000.00")
        assert len(result.cash_balances) == 1
        assert result.cash_balances[0].currency == "USD"
        assert result.cash_balances[0].available_cash == Decimal("10000.50")
        assert result.cash_balances[0].frozen_cash == Decimal("500.00")

    def test_get_account_prefers_usd(self, monkeypatch) -> None:
        class FakeBalanceItemHKD:
            currency = "HKD"
            available_cash = "5000"
            frozen_cash = "200"
            net_assets = "30000"

        class FakeBalanceItemUSD:
            currency = "USD"
            available_cash = "10000"
            frozen_cash = "500"
            net_assets = "15000"

        class FakeTradeContext:
            def account_balance(self):
                return [FakeBalanceItemHKD(), FakeBalanceItemUSD()]

        gw = BrokerGateway()
        gw._quote_ctx = MagicMock()
        gw._trade_ctx = FakeTradeContext()

        result = gw.get_account()
        assert result.total_assets == Decimal("15000")


class TestAccountAPIEndpoint:
    def test_get_account_endpoint_success(self, monkeypatch) -> None:
        from app import runner as runner_module

        fake_account = AccountInfo(
            total_assets=Decimal("25000"),
            cash_balances=[
                CashBalance(currency="USD", available_cash=Decimal("10000"), frozen_cash=Decimal("500")),
            ],
            net_assets=[NetAsset(currency="USD", amount=Decimal("25000"))],
        )

        fake_positions = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("100"), avg_price=Decimal("150")),
        ]

        mock_broker = MagicMock()
        mock_broker.get_account.return_value = fake_account
        mock_broker.get_positions.return_value = fake_positions
        mock_broker.get_quote.return_value = MagicMock(last_price=155.0)

        original_runner = runner_module.get_runner()
        original_runner.broker = mock_broker

        try:
            resp = client.get("/api/account")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_assets"] == 25000.0
            assert len(data["cash_balances"]) == 1
            assert data["cash_balances"][0]["currency"] == "USD"
            assert data["cash_balances"][0]["available_cash"] == 10000.0
            assert len(data["positions"]) == 1
            assert data["positions"][0]["symbol"] == "AAPL.US"
            assert data["positions"][0]["side"] == "LONG"
            assert data["positions"][0]["quantity"] == 100.0
            assert data["positions"][0]["market_value"] == 15500.0
        finally:
            original_runner.broker = BrokerGateway()

    def test_get_account_endpoint_broker_failure(self, monkeypatch) -> None:
        from app import runner as runner_module

        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = Exception("broker not connected")
        mock_broker.get_positions.side_effect = Exception("broker not connected")

        original_runner = runner_module.get_runner()
        original_runner.broker = mock_broker

        try:
            resp = client.get("/api/account")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_assets"] == 0.0
            assert data["cash_balances"] == []
            assert data["positions"] == []
        finally:
            original_runner.broker = BrokerGateway()

    def test_get_account_endpoint_position_quote_fallback(self, monkeypatch) -> None:
        from app import runner as runner_module

        fake_account = AccountInfo(
            total_assets=Decimal("10000"),
            cash_balances=[],
            net_assets=[NetAsset(currency="USD", amount=Decimal("10000"))],
        )

        fake_positions = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("100"), avg_price=Decimal("150")),
        ]

        mock_broker = MagicMock()
        mock_broker.get_account.return_value = fake_account
        mock_broker.get_positions.return_value = fake_positions
        mock_broker.get_quote.side_effect = Exception("quote unavailable")

        original_runner = runner_module.get_runner()
        original_runner.broker = mock_broker

        try:
            resp = client.get("/api/account")
            assert resp.status_code == 200
            data = resp.json()
            assert data["positions"][0]["market_value"] == 15000.0
        finally:
            original_runner.broker = BrokerGateway()
```

- [ ] **Step 2: Run the tests**

Run: `cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/test_account_api.py -v`
Expected: All tests pass.

- [ ] **Step 3: Run all tests to verify no regressions**

Run: `cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/ -v`
Expected: All tests pass (any broker-related test failures that existed before should still pass).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_account_api.py
git commit -m "test: add account endpoint and broker get_account() tests"
```

---

### Task 4: Add frontend types and API function

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/index.ts`
- Modify: `frontend/src/utils/labels.ts`

- [ ] **Step 1: Add TypeScript types to `frontend/src/types/index.ts`**

Append after the `OrderRecord` interface (after line 48):

```typescript
export interface CashBalance {
  currency: string
  available_cash: number
  frozen_cash: number
}

export interface Position {
  symbol: string
  side: string
  quantity: number
  avg_price: number
  market_value: number
}

export interface AccountInfo {
  total_assets: number
  cash_balances: CashBalance[]
  positions: Position[]
}
```

- [ ] **Step 2: Add `getAccount()` API function to `frontend/src/api/index.ts`**

Add import for `AccountInfo` in the import line at top (modify line 2):

```typescript
import type { AccountInfo, CredentialsConfig, StrategyConfig, StatusData, OrderRecord } from '../types'
```

Add at end of file:

```typescript
export async function getAccount(): Promise<AccountInfo> {
  const resp = await api.get('/api/account')
  return resp.data
}
```

- [ ] **Step 3: Add `positionSideLabel()` to `frontend/src/utils/labels.ts`**

Append at end of file:

```typescript
export function positionSideLabel(side?: string | null): string {
  switch (side) {
    case 'LONG':
      return '多头'
    case 'SHORT':
      return '空头'
    default:
      return '未知'
  }
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/index.ts frontend/src/utils/labels.ts
git commit -m "feat: add AccountInfo types, API function, and position side label"
```

---

### Task 5: Update Dashboard to display account info

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

- [ ] **Step 1: Add account data state and refresh logic to Dashboard.vue**

In the `<script setup>` section, add imports:

```typescript
import { getAccount } from '../api'
import type { AccountInfo } from '../types'
import { positionSideLabel } from '../utils/labels'
```

Add account state after the `status` ref (after line 79):

```typescript
const account = ref<AccountInfo>({
  total_assets: 0,
  cash_balances: [],
  positions: [],
})
const accountLoading = ref(false)
```

Add `accountRefreshTimer` variable after `reconnectAttempts` (after line 96):

```typescript
let accountRefreshTimer: ReturnType<typeof setInterval> | null = null
```

Modify the `onMounted` callback. Replace the existing `onMounted` block with:

```typescript
onMounted(async () => {
  try {
    const [s, st, acc] = await Promise.all([getStrategy(), getStatus(), getAccount().catch(() => ({ total_assets: 0, cash_balances: [], positions: [] }))])
    strategy.value = s
    status.value = st
    account.value = acc
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
    loadError.value = true
    ElMessage.error('刷新仪表盘数据失败')
  } finally {
    initialLoading.value = false
  }
  connectWebSocket()
  startPolling()
  startAccountRefresh()
})
```

Add the `startAccountRefresh` function after `startPolling`:

```typescript
function startAccountRefresh() {
  accountRefreshTimer = setInterval(async () => {
    try {
      account.value = await getAccount()
    } catch {
      // silent — account data will retry on next interval
    }
  }, 10000)
}
```

Modify the `refresh` function to include account data:

```typescript
async function refresh() {
  try {
    const [s, st, acc] = await Promise.all([getStrategy(), getStatus(), getAccount().catch(() => ({ total_assets: 0, cash_balances: [], positions: [] }))])
    strategy.value = s
    status.value = st
    account.value = acc
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
  }
}
```

Modify the `onUnmounted` block to clean up the account refresh timer. Add before the closing of `onUnmounted`:

```typescript
if (accountRefreshTimer) {
  clearInterval(accountRefreshTimer)
  accountRefreshTimer = null
}
```

- [ ] **Step 2: Add account display cards to the Dashboard template**

In the `<template>` section, add after the `el-row` that contains 风控状态 and 操作控制 (after the second `el-row`, before the 行情信息 card):

```html
<el-row :gutter="20" style="margin-top: 20px">
  <el-col :span="8">
    <el-card>
      <template #header>总资产</template>
      <h1 :style="{ color: account.total_assets >= 0 ? 'green' : 'red' }">
        ${{ account.total_assets.toFixed(2) }}
      </h1>
    </el-card>
  </el-col>
  <el-col :span="16">
    <el-card>
      <template #header>现金余额</template>
      <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" style="width: 100%">
        <el-table-column prop="currency" label="币种" width="100" />
        <el-table-column prop="available_cash" label="可用" width="150">
          <template #default="{ row }">${{ row.available_cash.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column prop="frozen_cash" label="冻结" width="150">
          <template #default="{ row }">${{ row.frozen_cash.toFixed(2) }}</template>
        </el-table-column>
      </el-table>
      <p v-else style="color: #999; text-align: center">暂无数据</p>
    </el-card>
  </el-col>
</el-row>

<el-card style="margin-top: 20px">
  <template #header>持仓明细</template>
  <el-table :data="account.positions" size="small" v-if="account.positions.length > 0" style="width: 100%">
    <el-table-column prop="symbol" label="股票代码" width="150" />
    <el-table-column prop="side" label="方向" width="100">
      <template #default="{ row }">{{ positionSideLabel(row.side) }}</template>
    </el-table-column>
    <el-table-column prop="quantity" label="数量" width="120">
      <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
    </el-table-column>
    <el-table-column prop="avg_price" label="均价" width="150">
      <template #default="{ row }">${{ row.avg_price.toFixed(2) }}</template>
    </el-table-column>
    <el-table-column prop="market_value" label="市值" width="150">
      <template #default="{ row }">${{ row.market_value.toFixed(2) }}</template>
    </el-table-column>
  </el-table>
  <p v-else style="color: #999; text-align: center">暂无持仓</p>
</el-card>
```

- [ ] **Step 3: Verify the frontend builds**

Run: `cd /home/lcy/code/auto_trade/frontend && npm run build`
Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/Dashboard.vue
git commit -m "feat: display account balance and positions on Dashboard"
```

---

### Task 6: Full integration verification

- [ ] **Step 1: Run all backend tests**

Run: `cd /home/lcy/code/auto_trade/backend && python3 -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 2: Run frontend build**

Run: `cd /home/lcy/code/auto_trade/frontend && npm run build`
Expected: Build succeeds.

- [ ] **Step 3: Manual smoke test checklist**

- [ ] Start docker-compose (`docker compose up --build -d`)
- [ ] Open Dashboard at http://localhost:8080
- [ ] Verify account section shows total assets, cash balances, and positions table
- [ ] Verify the account data refreshes every 10 seconds
- [ ] Stop docker-compose (`docker compose down`)

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: integration test fixes for account display feature"
```