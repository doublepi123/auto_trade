# Dashboard and Configuration Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make dashboard and configuration pages feel responsive while reducing repeated slow account and reload API work.

**Architecture:** Keep the current Vue 3 + Element Plus UI and FastAPI response schemas. Split frontend page loading into independent state channels, add polling in-flight guards, and add a backend account snapshot cache with safe stale fallback. Decouple save persistence from non-critical reload work without changing trading behavior or credential semantics.

**Tech Stack:** Vue 3 Composition API, TypeScript, Element Plus, Cypress, FastAPI, SQLAlchemy, pytest, Python standard-library threading/time/logging.

---

## File Structure

### Backend

- Create `backend/app/services/account_snapshot_service.py`
  - Owns account snapshot cache, TTL, in-flight refresh guard, broker-to-schema mapping, and duration logging for account refresh sub-steps.
  - Exposes `AccountSnapshotService.get_snapshot(broker) -> AccountResponse` and `clear_account_snapshot_cache() -> None` for tests.
- Modify `backend/app/api/trade.py`
  - Replace inline `/api/account` broker logic with `AccountSnapshotService().get_snapshot(get_runner().broker)`.
  - Keep `/api/account` response schema unchanged.
- Modify `backend/app/api/credentials.py`
  - Move runner credential reload into a helper that runs asynchronously after persistence.
  - Preserve `reload_warning` when reload fails synchronously in tests by using an injectable helper return value.
- Modify `backend/tests/test_account_api.py`
  - Add cache, stale fallback, and no-cache failure tests.
  - Clear account snapshot cache between tests.
- Modify `backend/tests/test_credentials_api.py`
  - Add a latency-focused assertion that credential save returns before a slow live reload completes.
  - Keep the existing reload failure warning test compatible.

### Frontend

- Modify `frontend/src/composables/useDashboardData.ts`
  - Split strategy/status loading flags and expose `loadStatus`, `loadStrategy`, and `load`.
  - Preserve current data values on refresh failure.
- Modify `frontend/src/composables/useStatusStream.ts`
  - Add an in-flight guard to fallback status polling.
  - Skip polling while websocket status is fresh.
- Modify `frontend/src/composables/useAccountRefresh.ts`
  - Add `loading`, `refreshing`, `hasLoaded`, and in-flight guard.
  - Preserve account values during refresh and expose clear error state.
- Modify `frontend/src/views/Dashboard.vue`
  - Remove full-page loading overlay dependency.
  - Add card-level loading/stale indicators for account, strategy, and LLM status sections.
  - Keep status/control cards visible as soon as possible.
- Modify `frontend/src/views/Strategy.vue`
  - Disable strategy form while initial load is active.
  - Add form-level loading state and scoped LLM status loading text.
- Modify `frontend/src/views/Credentials.vue`
  - Disable credential inputs while credential flags load.
  - Add card-level loading state.
- Modify `frontend/cypress/e2e/dashboard.cy.ts`
  - Add delayed account tests and request-overlap test.
- Modify `frontend/cypress/e2e/strategy.cy.ts`
  - Add initial-load disabled form test.
- Modify `frontend/cypress/e2e/credentials.cy.ts`
  - Add initial-load disabled form test.

Do not commit during implementation unless the user explicitly asks for commits.

---

## Task 1: Backend Account Snapshot Cache

**Files:**
- Create: `backend/app/services/account_snapshot_service.py`
- Modify: `backend/app/api/trade.py`
- Modify: `backend/tests/test_account_api.py`

- [ ] **Step 1: Add failing tests for cache hit and stale fallback**

Append these tests to `backend/tests/test_account_api.py` and update the imports at the top to include `time` and the cache clear helper.

```python
import time

from app.services.account_snapshot_service import clear_account_snapshot_cache
```

Extend the existing `_reset_runner` fixture so it clears the cache before and after each test:

```python
@pytest.fixture(autouse=True)
def _reset_runner():
    import app.runner as runner_mod
    old = runner_mod._runner
    runner_mod._runner = AppRunner()
    clear_account_snapshot_cache()
    yield
    clear_account_snapshot_cache()
    runner_mod._runner = old
```

Add these tests after `TestGetAccountEndpointSuccess`:

```python
class TestGetAccountEndpointCache:
    def test_account_endpoint_uses_short_lived_cache(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quote.return_value = Quote(symbol="AAPL.US", last_price=160.0, bid=159.5, ask=160.5, timestamp="")
        runner.broker = mock_broker

        first = client.get("/api/account")
        second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert mock_broker.get_account.call_count == 1
        assert mock_broker.get_positions.call_count == 1
        assert mock_broker.get_quote.call_count == 1

    def test_account_endpoint_returns_cached_snapshot_when_refresh_fails(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        first = client.get("/api/account")
        assert first.status_code == 200
        assert first.json()["available"] is True

        time.sleep(5.1)
        mock_broker.get_account.side_effect = RuntimeError("broker unavailable")
        mock_broker.get_positions.side_effect = RuntimeError("positions unavailable")

        second = client.get("/api/account")

        assert second.status_code == 200
        assert second.json() == first.json()
        assert mock_broker.get_account.call_count == 2

    def test_account_endpoint_refreshes_after_cache_ttl(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = [
            AccountInfo(total_assets=Decimal("100"), cash_balances=[], net_assets=[]),
            AccountInfo(total_assets=Decimal("200"), cash_balances=[], net_assets=[]),
        ]
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        first = client.get("/api/account")
        time.sleep(5.1)
        second = client.get("/api/account")

        assert first.json()["total_assets"] == 100.0
        assert second.json()["total_assets"] == 200.0
        assert mock_broker.get_account.call_count == 2
```

- [ ] **Step 2: Run the new backend tests and verify they fail**

Run:

```bash
cd backend && python3 -m pytest tests/test_account_api.py::TestGetAccountEndpointCache -v
```

Expected: FAIL because `app.services.account_snapshot_service` does not exist.

- [ ] **Step 3: Create `AccountSnapshotService`**

Create `backend/app/services/account_snapshot_service.py` with this implementation:

```python
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

from app.core.broker import AccountInfo, BrokerGateway
from app.schemas import AccountResponse, CashBalanceSchema, PositionSchema

logger = logging.getLogger("auto_trade.account")

_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.RLock()
_cached_snapshot: AccountResponse | None = None
_cached_at = 0.0
_refreshing = False


def clear_account_snapshot_cache() -> None:
    global _cached_snapshot, _cached_at, _refreshing
    with _cache_lock:
        _cached_snapshot = None
        _cached_at = 0.0
        _refreshing = False


def _cache_is_fresh(now: float) -> bool:
    return _cached_snapshot is not None and now - _cached_at < _CACHE_TTL_SECONDS


class AccountSnapshotService:
    def get_snapshot(self, broker: BrokerGateway) -> AccountResponse:
        global _cached_snapshot, _cached_at, _refreshing
        now = time.monotonic()
        with _cache_lock:
            if _cache_is_fresh(now):
                return _cached_snapshot.model_copy(deep=True)
            if _refreshing and _cached_snapshot is not None:
                return _cached_snapshot.model_copy(deep=True)
            _refreshing = True

        started_at = time.perf_counter()
        try:
            snapshot = self._fetch_snapshot(broker)
            if not snapshot.available:
                with _cache_lock:
                    if _cached_snapshot is not None:
                        return _cached_snapshot.model_copy(deep=True)
        except Exception:
            logger.exception("failed to refresh account snapshot")
            with _cache_lock:
                if _cached_snapshot is not None:
                    return _cached_snapshot.model_copy(deep=True)
            return AccountResponse(
                total_assets=0.0,
                cash_balances=[],
                positions=[],
                available=False,
                error="Account data unavailable",
            )
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.info("account snapshot refresh completed in %.1fms", elapsed_ms)
            with _cache_lock:
                _refreshing = False

        with _cache_lock:
            _cached_snapshot = snapshot.model_copy(deep=True)
            _cached_at = time.monotonic()
            return snapshot.model_copy(deep=True)

    def _fetch_snapshot(self, broker: BrokerGateway) -> AccountResponse:
        available = True
        total_assets = 0.0
        cash_balances: list[CashBalanceSchema] = []
        positions: list[PositionSchema] = []

        try:
            account_started_at = time.perf_counter()
            account = broker.get_account()
            logger.info("broker account call completed in %.1fms", (time.perf_counter() - account_started_at) * 1000)
            total_assets = float(account.total_assets)
            cash_balances = self._cash_balances(account)
        except Exception:
            logger.exception("failed to get account balance")
            available = False

        try:
            positions_started_at = time.perf_counter()
            broker_positions = broker.get_positions()
            logger.info("broker positions call completed in %.1fms", (time.perf_counter() - positions_started_at) * 1000)
            positions = [self._position_to_schema(broker, position) for position in broker_positions]
        except Exception:
            logger.exception("failed to get positions")
            available = False

        return AccountResponse(
            total_assets=total_assets,
            cash_balances=cash_balances,
            positions=positions,
            available=available,
            error=None if available else "Account data unavailable",
        )

    def _cash_balances(self, account: AccountInfo) -> list[CashBalanceSchema]:
        return [
            CashBalanceSchema(
                currency=balance.currency,
                available_cash=float(balance.available_cash),
                frozen_cash=float(balance.frozen_cash),
            )
            for balance in account.cash_balances
        ]

    def _position_to_schema(self, broker: BrokerGateway, position) -> PositionSchema:
        try:
            quote_started_at = time.perf_counter()
            quote = broker.get_quote(position.symbol)
            logger.info("broker quote call for %s completed in %.1fms", position.symbol, (time.perf_counter() - quote_started_at) * 1000)
            market_value = float(position.quantity * Decimal(str(quote.last_price)))
        except Exception:
            logger.warning("failed to get quote for %s, using avg_price fallback", position.symbol)
            market_value = float(position.quantity * position.avg_price)
        return PositionSchema(
            symbol=position.symbol,
            side=position.side,
            quantity=float(position.quantity),
            avg_price=float(position.avg_price),
            market_value=market_value,
        )
```

- [ ] **Step 4: Wire `/api/account` to the service**

Replace `get_account()` in `backend/app/api/trade.py` with:

```python
@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    runner = get_runner()
    return AccountSnapshotService().get_snapshot(runner.broker)
```

Update imports in `backend/app/api/trade.py`:

```python
from app.services.account_snapshot_service import AccountSnapshotService
```

Remove unused imports that are no longer needed in `trade.py`:

```python
import logging
from decimal import Decimal
from app.schemas import CashBalanceSchema, PositionSchema
```

Keep these schema imports:

```python
from app.schemas import AccountResponse, ControlRequest, MessageResponse, OrderResponse
```

- [ ] **Step 5: Run account API tests**

Run:

```bash
cd backend && python3 -m pytest tests/test_account_api.py -v
```

Expected: PASS for all tests in `test_account_api.py`.

---

## Task 2: Credential Reload Non-Blocking Save

**Files:**
- Modify: `backend/app/api/credentials.py`
- Modify: `backend/tests/test_credentials_api.py`

- [ ] **Step 1: Add a failing latency test**

Append this test to `backend/tests/test_credentials_api.py` inside `TestCredentialsAPI`:

```python
    def test_update_credentials_does_not_wait_for_slow_reload(self, monkeypatch) -> None:
        import time

        class SlowRunner:
            def reload_credentials(self) -> None:
                time.sleep(0.2)

        monkeypatch.setattr(credentials_api, "get_runner", lambda: SlowRunner())
        _clean_credentials()

        started_at = time.perf_counter()
        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": "token",
            "sct_key": "sct",
        })
        elapsed = time.perf_counter() - started_at

        assert resp.status_code == 200
        assert elapsed < 0.15
        assert resp.json()["reload_warning"] is None
```

- [ ] **Step 2: Run the new credential test and verify it fails**

Run:

```bash
cd backend && python3 -m pytest tests/test_credentials_api.py::TestCredentialsAPI::test_update_credentials_does_not_wait_for_slow_reload -v
```

Expected: FAIL because credential save currently waits for `reload_credentials()`.

- [ ] **Step 3: Add async reload helper while preserving synchronous failure warnings**

Modify `backend/app/api/credentials.py` to import threading:

```python
import threading
```

Add this helper above the route functions:

```python
def _reload_credentials_safely(runner: object) -> None:
    try:
        runner.reload_credentials()
    except Exception:
        logger.exception("credential reload failed after save")


def _reload_credentials_after_save() -> str | None:
    runner = get_runner()
    try:
        thread = threading.Thread(target=_reload_credentials_safely, args=(runner,), daemon=True)
        thread.start()
        return None
    except Exception:
        logger.exception("credential reload scheduling failed after save")
        return "Credentials saved but live reload failed. A restart may be required for changes to take effect."
```

Replace the reload block in `update_credentials()` with:

```python
    reload_warning = _reload_credentials_after_save()
```

The route body should still persist first:

```python
    config = svc.update_config(data)
    reload_warning = _reload_credentials_after_save()
    response = svc.to_response(config)
    response["reload_warning"] = reload_warning
    return CredentialResponse.model_validate(response)
```

- [ ] **Step 4: Adjust the existing reload failure test to target scheduling failure**

Replace `test_update_credentials_ignores_reload_failure` in `backend/tests/test_credentials_api.py` with:

```python
    def test_update_credentials_reports_reload_scheduling_failure(self, monkeypatch) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("reload scheduling failed")

        monkeypatch.setattr(credentials_api.threading, "Thread", FailingThread)
        _clean_credentials()

        resp = client.put("/api/credentials", json={
            "longbridge_app_key": "key",
            "longbridge_app_secret": "secret",
            "longbridge_access_token": "token",
            "sct_key": "sct",
        })

        assert resp.status_code == 200
        assert resp.json()["reload_warning"] == "Credentials saved but live reload failed. A restart may be required for changes to take effect."
```

- [ ] **Step 5: Run credential tests**

Run:

```bash
cd backend && python3 -m pytest tests/test_credentials_api.py -v
```

Expected: PASS for all tests in `test_credentials_api.py`.

---

## Task 3: Frontend Dashboard Data and Polling Guards

**Files:**
- Modify: `frontend/src/composables/useDashboardData.ts`
- Modify: `frontend/src/composables/useStatusStream.ts`
- Modify: `frontend/src/composables/useAccountRefresh.ts`

- [ ] **Step 1: Add failing Cypress tests for delayed account rendering and request overlap**

Append these tests to `frontend/cypress/e2e/dashboard.cy.ts`:

```typescript
  it('renders status and controls while account request is delayed', () => {
    cy.intercept('GET', '/api/account', (req) => {
      req.reply({
        delay: 2000,
        body: { total_assets: 12345, cash_balances: [], positions: [], available: true, error: null },
      })
    }).as('slowAccount')

    cy.visit('/#/')
    cy.contains('引擎状态', { timeout: 1000 }).should('be.visible')
    cy.contains('操作控制').should('be.visible')
    cy.contains('账户数据加载中').should('be.visible')
    cy.wait('@slowAccount')
    cy.contains('$12345.00').should('be.visible')
  })

  it('does not overlap account refresh requests when the previous request is still running', () => {
    let accountRequests = 0
    cy.clock()
    cy.intercept('GET', '/api/account', (req) => {
      accountRequests += 1
      req.reply({
        delay: 15000,
        body: { total_assets: 100, cash_balances: [], positions: [], available: true, error: null },
      })
    }).as('slowAccount')

    cy.visit('/#/')
    cy.tick(30000)
    cy.wrap(null).then(() => {
      expect(accountRequests).to.eq(1)
    })
  })
```

- [ ] **Step 2: Run the dashboard Cypress tests and verify they fail**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts
```

Expected: FAIL because `账户数据加载中` is not present and account polling can overlap.

- [ ] **Step 3: Split dashboard data loading flags**

Replace `frontend/src/composables/useDashboardData.ts` with:

```typescript
import { ref, computed } from 'vue'
import { getStrategy, getStatus } from '../api'
import type { StrategyConfig, StatusData } from '../types'

const defaultStrategy: StrategyConfig = {
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  llm_interval_minutes: 240,
  updated_at: '',
}

const defaultStatus: StatusData = {
  engine_state: 'flat', paused: false, kill_switch: false, runner_running: false,
  daily_pnl: 0, consecutive_losses: 0,
  last_price: 0, last_trigger_price: 0, last_trigger_at: null,
}

export function useDashboardData() {
  const strategy = ref<StrategyConfig>({ ...defaultStrategy })
  const status = ref<StatusData>({ ...defaultStatus })
  const strategyLoading = ref(true)
  const statusLoading = ref(true)
  const loadError = ref(false)

  const initialLoading = computed(() => strategyLoading.value && statusLoading.value)

  async function loadStrategy() {
    strategyLoading.value = true
    try {
      strategy.value = await getStrategy()
      loadError.value = false
    } catch (e) {
      console.error('Dashboard strategy load failed:', e)
      loadError.value = true
      throw e
    } finally {
      strategyLoading.value = false
    }
  }

  async function loadStatus() {
    statusLoading.value = true
    try {
      status.value = await getStatus()
      loadError.value = false
    } catch (e) {
      console.error('Dashboard status load failed:', e)
      loadError.value = true
      throw e
    } finally {
      statusLoading.value = false
    }
  }

  async function load() {
    await Promise.allSettled([loadStrategy(), loadStatus()])
    if (loadError.value) {
      throw new Error('Dashboard data load failed')
    }
  }

  async function refreshStatus() {
    try {
      status.value = await getStatus()
      loadError.value = false
    } catch {
      void 0
    }
  }

  return {
    strategy,
    status,
    initialLoading,
    strategyLoading,
    statusLoading,
    loadError,
    load,
    loadStrategy,
    loadStatus,
    refreshStatus,
  }
}
```

- [ ] **Step 4: Add status polling in-flight guard**

In `frontend/src/composables/useStatusStream.ts`, add a local flag near the existing timer variables:

```typescript
  let pollingInFlight = false
```

Replace `startPolling()` with:

```typescript
  function startPolling() {
    pollTimer = setInterval(async () => {
      if (hasFreshWebSocketStatus() || pollingInFlight) return
      pollingInFlight = true
      try {
        const st = await getStatus()
        if (!hasFreshWebSocketStatus()) {
          status.value = st
          realtimeStatus.value = 'polling'
        }
      } catch {
        // silent
      } finally {
        pollingInFlight = false
      }
    }, 3000)
  }
```

- [ ] **Step 5: Add account loading state and in-flight guard**

Replace `frontend/src/composables/useAccountRefresh.ts` with:

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
  const loading = ref(true)
  const refreshing = ref(false)
  const hasLoaded = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let inFlight = false

  async function refresh() {
    if (inFlight) return
    inFlight = true
    refreshing.value = hasLoaded.value
    loading.value = !hasLoaded.value
    try {
      const nextAccount = await getAccount()
      account.value = nextAccount
      accountError.value = !nextAccount.available
      hasLoaded.value = true
    } catch {
      accountError.value = true
    } finally {
      loading.value = false
      refreshing.value = false
      inFlight = false
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
    loading,
    refreshing,
    hasLoaded,
    refresh,
  }
}
```

- [ ] **Step 6: Run TypeScript diagnostics for changed composables**

Run:

```bash
cd frontend && npm run build
```

Expected at this stage: may fail because `Dashboard.vue` has not been updated to use new returned fields. Proceed to Task 4 if errors are only about unused/missing dashboard bindings.

---

## Task 4: Dashboard Progressive UI

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`
- Modify: `frontend/cypress/e2e/dashboard.cy.ts`

- [ ] **Step 1: Update script bindings**

In `frontend/src/views/Dashboard.vue`, replace:

```typescript
const { strategy, status, initialLoading, loadError, load, refreshStatus } = useDashboardData()
const { realtimeStatus, reconnectNow } = useStatusStream(status)
const { account, accountError, refresh: refreshAccount } = useAccountRefresh()
```

with:

```typescript
const { strategy, status, statusLoading, strategyLoading, loadError, load, refreshStatus } = useDashboardData()
const { realtimeStatus, reconnectNow } = useStatusStream(status)
const {
  account,
  accountError,
  loading: accountLoading,
  refreshing: accountRefreshing,
  refresh: refreshAccount,
} = useAccountRefresh()
```

Add LLM status loading state after `llmStatus`:

```typescript
const llmStatusLoading = ref(false)
```

Replace `loadLLMStatus()` with:

```typescript
async function loadLLMStatus() {
  llmStatusLoading.value = true
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    llmStatus.value = null
  } finally {
    llmStatusLoading.value = false
  }
}
```

Remove the debug log from `onMounted()`:

```typescript
  console.log('Dashboard init v2')
```

- [ ] **Step 2: Remove full-page loading and add card loading states**

In the template, replace the root opening tag:

```vue
  <div v-loading="initialLoading">
```

with:

```vue
  <div>
```

Add loading wrappers to the status cards:

```vue
        <el-card v-loading="statusLoading">
```

Apply that to the engine status, latest price, today PnL, risk status, and control cards.

For account cards, use account-specific loading text. Replace the total assets card body with:

```vue
          <template #header>
            <span>总资产</span>
            <el-tag v-if="accountRefreshing" size="small" type="info" effect="plain" style="margin-left: 8px">刷新中</el-tag>
          </template>
          <el-skeleton v-if="accountLoading" :rows="2" animated />
          <p v-else-if="accountError" style="color: #999; text-align: center">账户数据不可用</p>
          <h1 v-else :class="account.available ? 'metric-positive' : 'metric-negative'">
            <span class="metric-label">{{ account.available ? '可用' : '异常' }}</span>
            ${{ account.total_assets.toFixed(2) }}
          </h1>
```

For cash balance and positions cards, show this loading line before table rendering:

```vue
          <p v-if="accountLoading" style="color: #999; text-align: center">账户数据加载中...</p>
```

Then change the table `v-if` conditions to `v-else-if="account.cash_balances.length > 0"` and `v-else-if="account.positions.length > 0"` respectively.

For strategy summary, add loading state:

```vue
      <template #header>行情信息</template>
      <el-skeleton v-if="strategyLoading" :rows="4" animated />
      <template v-else>
        <p>股票代码：{{ strategy.symbol || '未配置' }}</p>
        <p>市场：{{ marketLabel(strategy.market) }}</p>
        <p>买入价下限：${{ strategy.buy_low }}</p>
        <p>卖出价上限：${{ strategy.sell_high }}</p>
        <p>做空：{{ strategy.short_selling ? '是' : '否' }}</p>
      </template>
```

For LLM status, keep the card visible while loading by changing the `v-if`:

```vue
    <el-card v-if="llmStatusLoading || llmStatus?.enabled || llmStatus?.reject_reason" style="margin-top: 20px">
      <template #header>LLM 智能区间</template>
      <p v-if="llmStatusLoading" style="color: #999">LLM 状态加载中...</p>
      <template v-else-if="llmStatus">
        <p>状态：<el-tag :type="llmStatus.enabled ? 'success' : 'info'">{{ llmStatus.enabled ? '已启用' : '已禁用' }}</el-tag></p>
        <p v-if="llmStatus.last_analysis_at">最近刷新：{{ new Date(llmStatus.last_analysis_at).toLocaleString() }}</p>
        <p v-if="llmStatus.next_analysis_at">下次分析：{{ new Date(llmStatus.next_analysis_at).toLocaleString() }}</p>
        <p v-if="llmStatus.reject_reason" style="color: #f56c6c">上次被拒：{{ llmStatus.reject_reason }}</p>
      </template>
    </el-card>
```

- [ ] **Step 3: Run dashboard Cypress tests**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts
```

Expected: PASS.

- [ ] **Step 4: Run frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected: PASS.

---

## Task 5: Strategy and Credentials Loading UX

**Files:**
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/views/Credentials.vue`
- Modify: `frontend/cypress/e2e/strategy.cy.ts`
- Modify: `frontend/cypress/e2e/credentials.cy.ts`

- [ ] **Step 1: Add failing Cypress tests**

Append to `frontend/cypress/e2e/strategy.cy.ts`:

```typescript
  it('disables strategy form while initial strategy load is pending', () => {
    cy.intercept('GET', '/api/strategy', (req) => {
      req.reply({
        delay: 2000,
        body: {
          id: 1, symbol: 'AAPL.US', market: 'US', buy_low: 100, sell_high: 200,
          short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
          llm_interval_minutes: 240, updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('slowStrategy')

    cy.visit('/#/strategy')
    cy.contains('策略配置加载中').should('be.visible')
    cy.get('button').contains('保存').should('be.disabled')
    cy.wait('@slowStrategy')
    cy.contains('策略配置加载中').should('not.exist')
  })
```

Append to `frontend/cypress/e2e/credentials.cy.ts`:

```typescript
  it('disables credentials form while credential status is pending', () => {
    cy.intercept('GET', '/api/credentials', (req) => {
      req.reply({
        delay: 2000,
        body: {
          id: 1, longbridge_app_key: '', longbridge_app_secret: '',
          longbridge_access_token: '', sct_key: '',
          has_longbridge_app_key: false, has_longbridge_app_secret: false,
          has_longbridge_access_token: false, has_sct_key: false,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('slowCredentials')

    cy.visit('/#/credentials')
    cy.contains('凭证状态加载中').should('be.visible')
    cy.get('button').contains('保存').should('be.disabled')
    cy.wait('@slowCredentials')
    cy.contains('凭证状态加载中').should('not.exist')
  })
```

- [ ] **Step 2: Run the new Cypress tests and verify they fail**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy.cy.ts,cypress/e2e/credentials.cy.ts
```

Expected: FAIL because the loading labels are not present.

- [ ] **Step 3: Update Strategy form loading state**

In `frontend/src/views/Strategy.vue`, change the strategy card opening tag:

```vue
    <el-card style="max-width: 600px">
```

To:

```vue
    <el-card style="max-width: 600px" v-loading="loading">
      <p v-if="loading" style="color: #909399; margin-top: 0">策略配置加载中...</p>
```

Add disabled binding to `el-form`:

```vue
      <el-form :model="form" label-width="180px" :disabled="loading" @submit.prevent="save">
```

Keep the existing save button disabled expression:

```vue
<el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
```

Add an LLM status loading ref:

```typescript
const llmStatusLoading = ref(false)
```

Replace `loadLLMStatus` with:

```typescript
const loadLLMStatus = async () => {
  llmStatusLoading.value = true
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    // silent
  } finally {
    llmStatusLoading.value = false
  }
}
```

Add this text near the top of the LLM smart interval card body:

```vue
      <p v-if="llmStatusLoading" style="color: #909399; font-size: 13px">LLM 状态加载中...</p>
```

- [ ] **Step 4: Update Credentials form loading state**

In `frontend/src/views/Credentials.vue`, change the card opening tag:

```vue
    <el-card style="max-width: 600px">
```

To:

```vue
    <el-card style="max-width: 600px" v-loading="loading">
```

Add loading text below the existing info alert:

```vue
      <p v-if="loading" style="color: #909399; margin-top: 0">凭证状态加载中...</p>
```

Add disabled binding to the form:

```vue
      <el-form :model="form" label-width="220px" :disabled="loading" @submit.prevent="handleSave">
```

Keep the existing save button disabled expression:

```vue
<el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
```

- [ ] **Step 5: Run page-specific Cypress tests**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy.cy.ts,cypress/e2e/credentials.cy.ts
```

Expected: PASS.

- [ ] **Step 6: Run frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected: PASS.

---

## Task 6: Full Verification and Regression Sweep

**Files:**
- No new files beyond prior tasks.
- Verify all changed backend and frontend files.

- [ ] **Step 1: Run backend focused tests**

Run:

```bash
cd backend && python3 -m pytest tests/test_account_api.py tests/test_credentials_api.py tests/test_api.py -v
```

Expected: PASS.

- [ ] **Step 2: Run frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected: PASS.

- [ ] **Step 3: Run frontend Cypress specs for changed pages**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts,cypress/e2e/strategy.cy.ts,cypress/e2e/credentials.cy.ts
```

Expected: PASS.

- [ ] **Step 4: Run LSP diagnostics on changed files**

Run diagnostics for these files:

- `backend/app/services/account_snapshot_service.py`
- `backend/app/api/trade.py`
- `backend/app/api/credentials.py`
- `frontend/src/composables/useDashboardData.ts`
- `frontend/src/composables/useStatusStream.ts`
- `frontend/src/composables/useAccountRefresh.ts`
- `frontend/src/views/Dashboard.vue`
- `frontend/src/views/Strategy.vue`
- `frontend/src/views/Credentials.vue`

Expected: no new errors caused by this work.

- [ ] **Step 5: Manual browser verification**

If Playwright Chrome is installed, open the app and verify:

```bash
cd frontend && npm run dev
```

Then check:

- Dashboard status/control cards appear while account data is delayed.
- Account card shows `账户数据加载中...` during first load and `刷新中` on later refresh.
- Strategy page shows `策略配置加载中...` and prevents editing during initial load.
- Credentials page shows `凭证状态加载中...` and prevents editing during initial load.

If Playwright Chrome is unavailable, report that browser verification was blocked by missing Chrome and rely on Cypress/build/backend tests.

---

## Coverage Matrix

- Dashboard section-level loading: Task 3, Task 4
- Account stale data and refresh indicator: Task 3, Task 4
- Polling in-flight guards: Task 3
- Strategy loading UX: Task 5
- Credentials loading UX: Task 5
- Account API cache and stale fallback: Task 1
- Credential save reload latency: Task 2
- Strategy save non-blocking behavior: existing `test_update_strategy_does_not_wait_for_running_runner_reload` in `backend/tests/test_api.py`, verified in Task 6
- Timing instrumentation: Task 1 adds account sub-step logs; Task 2 preserves reload scheduling/failure logging
- API schema compatibility: Task 1 keeps `AccountResponse`; Task 2 keeps `CredentialResponse`
