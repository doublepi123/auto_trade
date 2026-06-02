# P15 Dashboard Performance Implementation Plan

> **状态：✅ 已交付 2026-05-31**（commit `e6279c1`）
>
> 本计划与实际交付一致，无需偏差修正。完整交付摘要见 [主 Roadmap.md](../../Roadmap.md) 中 P15 行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve Dashboard/Strategy/Credentials perceived responsiveness and reduce repeated broker work on `/api/account` without changing trading behavior or public response schemas.

**Architecture:** Implement a small in-process account snapshot cache behind `GET /api/account`, add frontend in-flight guards for account/status refreshes, and replace page-level blocking with card/form-level loading. Keep response contracts unchanged and preserve stale data during refresh.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest, Vue 3 Composition API, TypeScript strict, Element Plus, Cypress.

---

## File Structure

### Backend

| File | Responsibility |
|---|---|
| `backend/app/api/trade.py` | Add account snapshot cache helpers and use them from `get_account()` while keeping `AccountResponse` unchanged. |
| `backend/tests/test_account_api.py` | Add cache/TTL/fallback regression tests for `/api/account`. |

### Frontend

| File | Responsibility |
|---|---|
| `frontend/src/composables/useAccountRefresh.ts` | Add `accountLoading`, `accountRefreshing`, and an in-flight guard. |
| `frontend/src/composables/useDashboardData.ts` | Split strategy/status loading state; add status refresh in-flight guard. |
| `frontend/src/views/Dashboard.vue` | Remove broad page blocking, render status/controls promptly, add card-level account/LLM/recent-data loading indicators. |
| `frontend/src/views/Strategy.vue` | Disable form while initial strategy load is pending; keep LLM status independent. |
| `frontend/src/views/Credentials.vue` | Disable credential form until saved flags load; add clear initial loading state. |
| `frontend/cypress/e2e/dashboard_performance.cy.ts` | Cover slow account API not blocking status/control rendering and no overlapping refreshes. |
| `frontend/cypress/e2e/strategy_credentials_loading.cy.ts` | Cover Strategy/Credentials initial loading guards. |

---

## Task 1: Backend account snapshot cache

**Files:**
- Modify: `backend/app/api/trade.py`
- Test: `backend/tests/test_account_api.py`

- [ ] **Step 1: Write failing tests for account cache**

Append to `backend/tests/test_account_api.py`:

```python
class TestGetAccountEndpointCache:
    def test_account_endpoint_uses_cached_snapshot_within_ttl(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        trade_api._account_snapshot_cache = None
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: 1000.0)

        first = client.get("/api/account")
        second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["total_assets"] == 50000.0
        assert mock_broker.get_account.call_count == 1
        assert mock_broker.get_positions.call_count == 1

    def test_account_endpoint_refreshes_after_ttl(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"), cash_balances=[], net_assets=[]
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None

        now = {"value": 1000.0}
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: now["value"])
        client.get("/api/account")
        now["value"] += trade_api.ACCOUNT_CACHE_TTL_SECONDS + 0.1
        client.get("/api/account")

        assert mock_broker.get_account.call_count == 2
        assert mock_broker.get_positions.call_count == 2

    def test_account_endpoint_returns_cache_when_refresh_fails(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None

        now = {"value": 1000.0}
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: now["value"])
        client.get("/api/account")
        now["value"] += trade_api.ACCOUNT_CACHE_TTL_SECONDS + 0.1
        mock_broker.get_account.side_effect = RuntimeError("broker down")
        mock_broker.get_positions.side_effect = RuntimeError("broker down")

        resp = client.get("/api/account")
        data = resp.json()
        assert resp.status_code == 200
        assert data["available"] is True
        assert data["total_assets"] == 50000.0
        assert data["error"] is None

    def test_account_endpoint_reports_unavailable_without_cache(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = RuntimeError("broker down")
        mock_broker.get_positions.side_effect = RuntimeError("broker down")
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: 1000.0)

        resp = client.get("/api/account")
        data = resp.json()
        assert resp.status_code == 200
        assert data["available"] is False
        assert data["error"] == "Account data unavailable"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_account_api.py::TestGetAccountEndpointCache -v
```

Expected: FAIL with missing `_account_snapshot_cache`, `_account_cache_now`, or `ACCOUNT_CACHE_TTL_SECONDS`.

- [ ] **Step 3: Implement cache helpers in `trade.py`**

Add imports near the top of `backend/app/api/trade.py`:

```python
import threading
import time
```

Add module-level cache definitions before `get_account()`:

```python
ACCOUNT_CACHE_TTL_SECONDS = 5.0
_account_cache_lock = threading.Lock()
_account_snapshot_cache: tuple[float, AccountResponse] | None = None
_account_refresh_lock = threading.Lock()


def _account_cache_now() -> float:
    return time.monotonic()


def _cached_account_response(now: float, *, allow_stale: bool) -> AccountResponse | None:
    with _account_cache_lock:
        if _account_snapshot_cache is None:
            return None
        cached_at, response = _account_snapshot_cache
        if allow_stale or now - cached_at <= ACCOUNT_CACHE_TTL_SECONDS:
            return response.model_copy(deep=True)
    return None


def _store_account_response(now: float, response: AccountResponse) -> None:
    with _account_cache_lock:
        global _account_snapshot_cache
        _account_snapshot_cache = (now, response.model_copy(deep=True))
```

Refactor existing `get_account()` body into `_fetch_account_response()` returning `AccountResponse`. Then implement `get_account()`:

```python
@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    now = _account_cache_now()
    cached = _cached_account_response(now, allow_stale=False)
    if cached is not None:
        return cached

    if not _account_refresh_lock.acquire(blocking=False):
        cached = _cached_account_response(now, allow_stale=True)
        if cached is not None:
            return cached
        _account_refresh_lock.acquire()

    try:
        response = _fetch_account_response()
    except Exception:
        logging.getLogger("auto_trade.trade").exception("failed to refresh account snapshot")
        cached = _cached_account_response(now, allow_stale=True)
        if cached is not None:
            return cached
        return AccountResponse(total_assets=0.0, cash_balances=[], positions=[], available=False, error="Account data unavailable")
    finally:
        _account_refresh_lock.release()

    if response.available:
        _store_account_response(now, response)
    return response
```

Keep `_fetch_account_response()` behavior equivalent to the old endpoint: partial account/position failures still return 200 with `available=False` and the same schema.

- [ ] **Step 4: Run backend account tests**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_account_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit backend cache**

```bash
git add backend/app/api/trade.py backend/tests/test_account_api.py
git commit -m "perf(api): cache account snapshots for dashboard refresh"
```

---

## Task 2: Frontend account/status in-flight guards

**Files:**
- Modify: `frontend/src/composables/useAccountRefresh.ts`
- Modify: `frontend/src/composables/useDashboardData.ts`

- [ ] **Step 1: Add account refresh in-flight guard**

Replace `useAccountRefresh()` in `frontend/src/composables/useAccountRefresh.ts` with:

```ts
export function useAccountRefresh(intervalMs = 10000) {
  const account = ref<AccountInfo>({ ...defaultAccount })
  const accountError = ref(false)
  const accountLoading = ref(true)
  const accountRefreshing = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let refreshInFlight = false

  async function refresh() {
    if (refreshInFlight) return
    refreshInFlight = true
    accountRefreshing.value = true
    try {
      account.value = await getAccount()
      accountError.value = !account.value.available
    } catch {
      accountError.value = true
    } finally {
      accountLoading.value = false
      accountRefreshing.value = false
      refreshInFlight = false
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
    accountLoading,
    accountRefreshing,
    refresh,
  }
}
```

- [ ] **Step 2: Split dashboard strategy/status loading state**

In `frontend/src/composables/useDashboardData.ts`, add refs:

```ts
const strategyLoading = ref(true)
const statusLoading = ref(true)
let statusRefreshInFlight = false
```

Replace `load()` with independent requests:

```ts
async function load() {
  initialLoading.value = true
  loadError.value = false
  const strategyPromise = getStrategy()
    .then((s) => { strategy.value = s })
    .finally(() => { strategyLoading.value = false })
  const statusPromise = getStatus()
    .then((st) => { status.value = st })
    .finally(() => { statusLoading.value = false })

  const results = await Promise.allSettled([strategyPromise, statusPromise])
  if (results.some((result) => result.status === 'rejected')) {
    loadError.value = true
    throw new Error('Dashboard data load failed')
  }
  initialLoading.value = false
}
```

Replace `refreshStatus()` with guarded refresh:

```ts
async function refreshStatus() {
  if (statusRefreshInFlight) return
  statusRefreshInFlight = true
  try {
    status.value = await getStatus()
    loadError.value = false
  } catch {
    void 0
  } finally {
    statusRefreshInFlight = false
    statusLoading.value = false
  }
}
```

Return `strategyLoading` and `statusLoading`.

- [ ] **Step 3: Run TypeScript check**

Run:

```bash
cd frontend && npm run type-check
```

Expected: PASS.

- [ ] **Step 4: Commit composable guards**

```bash
git add frontend/src/composables/useAccountRefresh.ts frontend/src/composables/useDashboardData.ts
git commit -m "perf(frontend): guard dashboard refresh requests"
```

---

## Task 3: Dashboard section-level loading

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

- [ ] **Step 1: Wire new loading refs**

Update destructuring:

```ts
const { strategy, status, initialLoading, strategyLoading, statusLoading, loadError, load, refreshStatus } = useDashboardData()
const { account, accountError, accountLoading, accountRefreshing, refresh: refreshAccount } = useAccountRefresh()
const llmStatusLoading = ref(true)
const recentOrdersLoading = ref(true)
const recentEventsLoading = ref(true)
```

Update loaders:

```ts
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

async function loadRecentOrders() {
  recentOrdersLoading.value = true
  try {
    recentOrders.value = (await getOrders({ scope: 'today', page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentOrders.value = []
  } finally {
    recentOrdersLoading.value = false
  }
}

async function loadRecentEvents() {
  recentEventsLoading.value = true
  try {
    recentEvents.value = (await getTradeEvents({ page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentEvents.value = []
  } finally {
    recentEventsLoading.value = false
  }
}
```

- [ ] **Step 2: Remove broad blocking**

Change the root template from:

```vue
<div class="dashboard-page" v-loading="initialLoading">
```

to:

```vue
<div class="dashboard-page">
```

Apply `v-loading` to individual cards/sections:

- status/controls cards: `v-loading="statusLoading"`
- strategy/range cards: `v-loading="strategyLoading"`
- account/position cards: `v-loading="accountLoading || accountRefreshing"`
- LLM card: `v-loading="llmStatusLoading"`
- recent orders/events lists: their own loading refs

Use existing Element Plus card wrappers; do not redesign the layout.

- [ ] **Step 3: Preserve stale data while refreshing**

Where account error/loading is displayed, show existing data and add a small tag when `accountRefreshing` is true:

```vue
<el-tag v-if="accountRefreshing && !accountLoading" size="small" type="info">刷新中</el-tag>
```

- [ ] **Step 4: Run TypeScript check**

Run:

```bash
cd frontend && npm run type-check
```

Expected: PASS.

- [ ] **Step 5: Commit dashboard loading UI**

```bash
git add frontend/src/views/Dashboard.vue
git commit -m "perf(dashboard): render cards with section loading states"
```

---

## Task 4: Strategy and Credentials initial loading guards

**Files:**
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/views/Credentials.vue`

- [ ] **Step 1: Strategy form guard**

In `frontend/src/views/Strategy.vue`, ensure form inputs are disabled while `loading` is true. Wrap the existing form with card/form loading:

```vue
<el-card v-loading="loading">
```

Add `:disabled="loading"` to the main `el-form` if not already present:

```vue
<el-form :model="form" label-width="180px" @submit.prevent="save" :disabled="loading">
```

Keep LLM status/action button loading separate from initial strategy load.

- [ ] **Step 2: Credentials form guard**

In `frontend/src/views/Credentials.vue`, use the existing loading state or add:

```ts
const credentialLoading = ref(true)
```

Set it to false after initial credential fetch completes. Disable the main credentials form while `credentialLoading` is true:

```vue
<el-form :model="form" label-width="180px" :disabled="credentialLoading || saving">
```

Add card loading:

```vue
<el-card v-loading="credentialLoading">
```

- [ ] **Step 3: Run TypeScript check**

Run:

```bash
cd frontend && npm run type-check
```

Expected: PASS.

- [ ] **Step 4: Commit loading guards**

```bash
git add frontend/src/views/Strategy.vue frontend/src/views/Credentials.vue
git commit -m "perf(config): add initial loading guards"
```

---

## Task 5: Cypress coverage for loading behavior

**Files:**
- Create: `frontend/cypress/e2e/dashboard_performance.cy.ts`
- Create: `frontend/cypress/e2e/strategy_credentials_loading.cy.ts`

- [ ] **Step 1: Add Dashboard slow account test**

Create `frontend/cypress/e2e/dashboard_performance.cy.ts`:

```ts
describe('Dashboard performance behavior', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('renders status and controls while account request is delayed', () => {
    cy.intercept('GET', '/api/account', (req) => {
      req.reply((res) => {
        res.delay = 1200
        res.send({ total_assets: 10000, cash_balances: [], positions: [], available: true, error: null })
      })
    }).as('slowAccount')

    cy.visitApp('/')
    cy.contains('启动交易').should('be.visible')
    cy.contains('实时连接').should('be.visible')
    cy.wait('@slowAccount')
  })

  it('does not create overlapping account refreshes', () => {
    let active = 0
    let maxActive = 0
    cy.intercept('GET', '/api/account', (req) => {
      active += 1
      maxActive = Math.max(maxActive, active)
      req.reply((res) => {
        res.delay = 800
        active -= 1
        res.send({ total_assets: 10000, cash_balances: [], positions: [], available: true, error: null })
      })
    })

    cy.visitApp('/')
    cy.wait(2500).then(() => {
      expect(maxActive).to.eq(1)
    })
  })
})
```

- [ ] **Step 2: Add Strategy/Credentials loading guard tests**

Create `frontend/cypress/e2e/strategy_credentials_loading.cy.ts`:

```ts
describe('Configuration loading guards', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('does not expose editable strategy defaults before strategy loads', () => {
    cy.intercept('GET', '/api/strategy', (req) => {
      req.reply((res) => {
        res.delay = 1000
        res.send({
          id: 1,
          symbol: 'AAPL.US',
          market: 'US',
          buy_low: 100,
          sell_high: 200,
          short_selling: false,
          min_profit_amount: 0,
          auto_resume_minutes: 3,
          max_daily_loss: 5000,
          max_consecutive_losses: 3,
          llm_interval_minutes: 2,
          fee_rate_us: 0.0005,
          fee_rate_hk: 0.003,
          min_repricing_pct: 0.003,
          llm_action_cooldown_seconds: 60,
          trading_session_mode: 'ANY',
          margin_safety_factor: 0.9,
          updated_at: new Date().toISOString(),
        })
      })
    })

    cy.visitApp('/strategy')
    cy.contains('保存').should('be.disabled')
    cy.contains('AAPL.US').should('be.visible')
  })

  it('keeps credentials inputs disabled while credential state loads', () => {
    cy.intercept('GET', '/api/credentials', (req) => {
      req.reply((res) => {
        res.delay = 1000
        res.send({
          id: 1,
          longbridge_app_key_set: true,
          longbridge_app_secret_set: true,
          longbridge_access_token_set: true,
          sct_key_set: false,
          notification_channels: [],
          updated_at: new Date().toISOString(),
        })
      })
    })

    cy.visitApp('/credentials')
    cy.get('input').first().should('be.disabled')
    cy.contains('保存').should('be.disabled')
  })
})
```

- [ ] **Step 3: Run Cypress specs**

Run:

```bash
cd frontend && npm run cypress:run -- --spec "cypress/e2e/dashboard_performance.cy.ts,cypress/e2e/strategy_credentials_loading.cy.ts"
```

Expected: PASS.

- [ ] **Step 4: Commit Cypress tests**

```bash
git add frontend/cypress/e2e/dashboard_performance.cy.ts frontend/cypress/e2e/strategy_credentials_loading.cy.ts
git commit -m "test(e2e): cover dashboard and config loading performance"
```

---

## Task 6: Final verification and roadmap update

**Files:**
- Modify: `docs/Roadmap.md`

- [ ] **Step 1: Run backend tests**

```bash
cd backend && .venv/bin/python -m pytest tests/ -v --tb=short
```

Expected: all tests pass.

- [ ] **Step 2: Run backend type check**

```bash
cd backend && .venv/bin/python -m basedpyright
```

Expected: `0 errors, 0 warnings, 0 notes`.

- [ ] **Step 3: Run frontend checks**

```bash
cd frontend && npm run type-check && npm run build
```

Expected: type-check passes and Vite build succeeds. Existing chunk-size warning is acceptable.

- [ ] **Step 4: Update `docs/Roadmap.md`**

In the execution table, add:

```markdown
| 已完成 | **P15 Dashboard & 配置性能优化** | ✅ 2026-05-31 | `/api/account` 短 TTL 缓存 + Dashboard 分区加载 + 配置页初始 loading guard；pytest / basedpyright / frontend build / Cypress 通过。 |
```

Update “下一步建议” to:

```markdown
**P15 Dashboard & 配置性能优化已完成交付。** 后续建议推进 P16（策略实验平台 Phase 1：批量回测 + 排行榜），P18 技术债可穿插处理。
```

- [ ] **Step 5: Commit roadmap and verification**

```bash
git add docs/Roadmap.md
git commit -m "docs: mark dashboard performance iteration complete"
```

---

## Self-Review

### Spec coverage

- Dashboard section-level loading: Task 3
- Dashboard polling/in-flight guards: Task 2
- Strategy loading guard: Task 4
- Credentials loading guard: Task 4
- Account API latency/cache: Task 1
- Backend cache tests: Task 1
- Frontend loading Cypress coverage: Task 5
- Final verification and roadmap: Task 6

### Explicit non-goals preserved

- No trading logic changes.
- No public response schema changes.
- No auth or credential storage changes.
- No queue/background job framework.

### Known deferrals

- Async save reload decoupling is deferred because current P15 value is dominated by `/api/account` and dashboard blocking. Treat it as part of P18/P19 if profiling still shows save latency after this iteration.
