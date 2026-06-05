# P27 Read-only Multi-symbol Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only multi-symbol watchlist snapshots to the Dashboard without changing the existing single-symbol trading path.

**Architecture:** Keep `StrategyConfig`, `/api/status`, WebSocket, `AppRunner`, and automatic trading single-symbol. Reuse watchlist as the multi-symbol source of truth and add an ephemeral `GET /api/watchlist/snapshots` endpoint that combines watchlist rows, current strategy symbol, and broker quotes. Frontend adds a typed API wrapper, a small polling composable, and a Dashboard table below the existing cockpit.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy sync ORM, Pydantic v2, pytest, Vue 3 `<script setup>`, TypeScript strict, Element Plus, Cypress.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `backend/app/schemas.py` | Add `WatchlistSnapshot` response schema next to `WatchlistQuote`. | Modify |
| `backend/app/api/watchlist.py` | Add `GET /api/watchlist/snapshots`; keep `/quotes` unchanged. | Modify |
| `backend/tests/test_watchlist.py` | Cover empty snapshots, successful aggregation, and broker failure. | Modify |
| `frontend/src/types/index.ts` | Add `WatchlistSnapshot` interface. | Modify |
| `frontend/src/api/watchlist.ts` | Add `getWatchlistSnapshots()`. | Modify |
| `frontend/src/composables/useMultiSymbolSnapshots.ts` | New polling composable for Dashboard only. | Create |
| `frontend/src/views/Dashboard.vue` | Render read-only multi-symbol table below existing cockpit. | Modify |
| `frontend/cypress/e2e/dashboard.cy.ts` | Cover populated and empty multi-symbol snapshot states. | Modify |
| `frontend/cypress/support/e2e.ts` | Stub `/api/watchlist/snapshots`. | Modify |

---

## Task 1: Backend schema and API endpoint

**Files:**
- Modify: `backend/app/schemas.py:733-739`
- Modify: `backend/app/api/watchlist.py:1-102`
- Test: `backend/tests/test_watchlist.py`

- [x] **Step 1: Write failing tests for snapshot API**

Append these tests inside `class TestWatchlistApi` in `backend/tests/test_watchlist.py`:

```python
    def test_get_watchlist_snapshots_empty(self, clean_db):
        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_watchlist_snapshots_marks_current_trading_symbol(self, clean_db, monkeypatch):
        from datetime import datetime, timezone
        from decimal import Decimal
        from types import SimpleNamespace

        from app.api import watchlist as watchlist_api

        db = SessionLocal()
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add_all([
            WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=True),
            WatchlistItem(symbol="AAPL.US", market="US", alias="Apple", is_active=False),
        ])
        db.commit()
        db.close()

        class FakeBroker:
            def get_quotes(self, symbols):
                assert symbols == ["NVDA.US", "AAPL.US"]
                return [
                    SimpleNamespace(symbol="NVDA.US", last_price=Decimal("180.5"), bid=Decimal("180.4"), ask=Decimal("180.6"), timestamp="2026-06-04T10:00:00Z"),
                    SimpleNamespace(symbol="AAPL.US", last_price=Decimal("199.5"), bid=Decimal("199.4"), ask=Decimal("199.6"), timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)),
                ]

        monkeypatch.setattr(watchlist_api, "BrokerGateway", lambda: FakeBroker())

        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 200
        data = resp.json()
        assert data == [
            {
                "symbol": "NVDA.US",
                "market": "US",
                "alias": "Nvidia",
                "is_trading_target": True,
                "last_price": 180.5,
                "bid": 180.4,
                "ask": 180.6,
                "timestamp": "2026-06-04T10:00:00Z",
            },
            {
                "symbol": "AAPL.US",
                "market": "US",
                "alias": "Apple",
                "is_trading_target": False,
                "last_price": 199.5,
                "bid": 199.4,
                "ask": 199.6,
                "timestamp": "2026-06-04T10:00:00+00:00",
            },
        ]

    def test_get_watchlist_snapshots_returns_503_when_broker_fails(self, clean_db, monkeypatch):
        from app.api import watchlist as watchlist_api

        db = SessionLocal()
        db.add(WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=False))
        db.commit()
        db.close()

        class FakeBroker:
            def get_quotes(self, symbols):
                raise RuntimeError("broker down")

        monkeypatch.setattr(watchlist_api, "BrokerGateway", lambda: FakeBroker())

        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "broker quote unavailable"
```

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_empty tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_marks_current_trading_symbol tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_returns_503_when_broker_fails -v
```

Observed RED: failures with HTTP 405 for `/api/watchlist/snapshots` because the dynamic `/{item_id}` route existed but no matching GET snapshot route existed yet.

- [x] **Step 3: Add `WatchlistSnapshot` schema**

In `backend/app/schemas.py`, insert after `class WatchlistQuote`:

```python
class WatchlistSnapshot(BaseModel):
    symbol: str
    market: str
    alias: str = ""
    is_trading_target: bool = False
    last_price: float
    bid: float
    ask: float
    timestamp: str
```

- [x] **Step 4: Implement snapshot endpoint**

In `backend/app/api/watchlist.py`, update imports:

```python
from app.models import StrategyConfig
from app.schemas import WatchlistItemResponse, WatchlistItemSchema, MessageResponse, WatchlistQuote, WatchlistSnapshot
```

Then insert after `get_watchlist_quotes()`:

```python
@router.get("/snapshots", response_model=List[WatchlistSnapshot])
def get_watchlist_snapshots(
    db: Session = Depends(get_db),
) -> List[WatchlistSnapshot]:
    svc = WatchlistService(db)
    items = svc.list_items()
    if not items:
        return []

    symbols = [item.symbol for item in items]
    try:
        broker = BrokerGateway()
        quotes = broker.get_quotes(symbols)
    except Exception:
        logger.exception("failed to fetch watchlist snapshots")
        raise HTTPException(status_code=503, detail="broker quote unavailable") from None

    quote_by_symbol = {quote.symbol: quote for quote in quotes}
    strategy = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    trading_symbol = strategy.symbol if strategy is not None else ""
    snapshots: list[WatchlistSnapshot] = []
    for item in items:
        quote = quote_by_symbol.get(item.symbol)
        if quote is None:
            continue
        timestamp = quote.timestamp
        snapshots.append(
            WatchlistSnapshot(
                symbol=item.symbol,
                market=item.market,
                alias=item.alias,
                is_trading_target=item.symbol == trading_symbol,
                last_price=float(quote.last_price),
                bid=float(quote.bid),
                ask=float(quote.ask),
                timestamp=timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
            )
        )
    return snapshots
```

- [x] **Step 5: Run backend snapshot tests and verify GREEN**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_empty tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_marks_current_trading_symbol tests/test_watchlist.py::TestWatchlistApi::test_get_watchlist_snapshots_returns_503_when_broker_fails -v
```

Expected: 3 passed.

---

## Task 2: Frontend API and polling composable

**Files:**
- Modify: `frontend/src/types/index.ts:391-397`
- Modify: `frontend/src/api/watchlist.ts:1-27`
- Create: `frontend/src/composables/useMultiSymbolSnapshots.ts`

- [x] **Step 1: Add `WatchlistSnapshot` type**

In `frontend/src/types/index.ts`, insert after `WatchlistQuote`:

```ts
export interface WatchlistSnapshot {
  symbol: string
  market: 'US' | 'HK'
  alias: string
  is_trading_target: boolean
  last_price: number
  bid: number
  ask: number
  timestamp: string
}
```

- [x] **Step 2: Add API wrapper**

Update `frontend/src/api/watchlist.ts` import:

```ts
import type { WatchlistItem, WatchlistQuote, WatchlistSnapshot } from '../types'
```

Append:

```ts
export async function getWatchlistSnapshots(): Promise<WatchlistSnapshot[]> {
  const resp = await api.get('/api/watchlist/snapshots')
  return resp.data
}
```

- [x] **Step 3: Create polling composable**

Create `frontend/src/composables/useMultiSymbolSnapshots.ts`:

```ts
import { onBeforeUnmount, ref } from 'vue'
import { getWatchlistSnapshots } from '../api/watchlist'
import type { WatchlistSnapshot } from '../types'

const snapshots = ref<WatchlistSnapshot[]>([])
const loading = ref(false)
const error = ref('')
let timer: number | null = null

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    snapshots.value = await getWatchlistSnapshots()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载多标的快照失败'
  } finally {
    loading.value = false
  }
}

function start(intervalMs = 15000) {
  if (timer !== null) return
  void refresh()
  timer = window.setInterval(() => {
    void refresh()
  }, intervalMs)
}

function stop() {
  if (timer === null) return
  window.clearInterval(timer)
  timer = null
}

export function useMultiSymbolSnapshots() {
  onBeforeUnmount(stop)
  return {
    snapshots,
    loading,
    error,
    refresh,
    start,
    stop,
  }
}
```

- [x] **Step 4: Run frontend type-check for RED/GREEN sanity**

Run:

```bash
cd frontend && npm run type-check
```

Expected: pass.

---

## Task 3: Dashboard read-only multi-symbol table

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`
- Test: `frontend/cypress/support/e2e.ts`
- Test: `frontend/cypress/e2e/dashboard.cy.ts`

- [x] **Step 1: Stub snapshots in Cypress support**

In `frontend/cypress/support/e2e.ts`, add an intercept alongside existing API stubs:

```ts
cy.intercept('GET', '/api/watchlist/snapshots', {
  body: [
    {
      symbol: 'NVDA.US',
      market: 'US',
      alias: 'Nvidia',
      is_trading_target: true,
      last_price: 180.5,
      bid: 180.4,
      ask: 180.6,
      timestamp: '2026-06-04T10:00:00Z',
    },
    {
      symbol: 'AAPL.US',
      market: 'US',
      alias: 'Apple',
      is_trading_target: false,
      last_price: 199.5,
      bid: 199.4,
      ask: 199.6,
      timestamp: '2026-06-04T10:00:00Z',
    },
  ],
})
```

- [x] **Step 2: Write failing Dashboard Cypress assertions**

In `frontend/cypress/e2e/dashboard.cy.ts`, append:

```ts
it('shows read-only multi-symbol snapshots', () => {
  cy.visitApp('/')

  cy.contains('多标的观察').should('be.visible')
  cy.contains('NVDA.US').should('be.visible')
  cy.contains('Nvidia').should('be.visible')
  cy.contains('当前交易').should('be.visible')
  cy.contains('AAPL.US').should('be.visible')
})

it('shows empty multi-symbol snapshot state', () => {
  cy.intercept('GET', '/api/watchlist/snapshots', { body: [] })
  cy.visitApp('/')

  cy.contains('多标的观察').should('be.visible')
  cy.contains('暂无观察标的').should('be.visible')
})
```

- [x] **Step 3: Run Cypress spec and verify RED**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts
```

Expected: new tests fail because Dashboard does not render the multi-symbol section yet.

- [x] **Step 4: Wire composable into Dashboard script**

In `frontend/src/views/Dashboard.vue`, import the composable:

```ts
import { useMultiSymbolSnapshots } from '../composables/useMultiSymbolSnapshots'
```

Inside setup code, create state and start polling in existing mount flow:

```ts
const multiSymbols = useMultiSymbolSnapshots()

onMounted(() => {
  multiSymbols.start()
})
```

If `Dashboard.vue` already has an `onMounted`, add only `multiSymbols.start()` inside the existing callback. Do not create a second lifecycle block if the existing style keeps one lifecycle block.

- [x] **Step 5: Render multi-symbol table**

Insert below the existing cockpit/status cards and above charts/history blocks:

```vue
<el-card class="card" data-testid="multi-symbol-snapshots">
  <template #header>
    <div class="card-header">
      <span>多标的观察</span>
      <el-button size="small" :loading="multiSymbols.loading.value" @click="multiSymbols.refresh">刷新</el-button>
    </div>
  </template>

  <el-alert
    v-if="multiSymbols.error.value"
    type="warning"
    :closable="false"
    :title="multiSymbols.error.value"
    style="margin-bottom: 12px"
  />

  <el-empty v-if="!multiSymbols.loading.value && multiSymbols.snapshots.value.length === 0" description="暂无观察标的" />
  <el-table v-else :data="multiSymbols.snapshots.value" size="small">
    <el-table-column prop="symbol" label="标的" min-width="120" />
    <el-table-column prop="alias" label="别名" min-width="120" />
    <el-table-column prop="market" label="市场" width="80" />
    <el-table-column label="最新价" width="110">
      <template #default="scope">{{ scope.row.last_price.toFixed(2) }}</template>
    </el-table-column>
    <el-table-column label="买一" width="110">
      <template #default="scope">{{ scope.row.bid.toFixed(2) }}</template>
    </el-table-column>
    <el-table-column label="卖一" width="110">
      <template #default="scope">{{ scope.row.ask.toFixed(2) }}</template>
    </el-table-column>
    <el-table-column label="状态" width="110">
      <template #default="scope">
        <el-tag v-if="scope.row.is_trading_target" type="success" size="small">当前交易</el-tag>
        <span v-else>观察</span>
      </template>
    </el-table-column>
  </el-table>
</el-card>
```

- [x] **Step 6: Run Dashboard Cypress and verify GREEN**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts
```

Expected: dashboard spec passes.

---

## Task 4: Final verification and documentation

**Files:**
- Modify: `docs/Roadmap.md`

- [x] **Step 1: Run backend watchlist tests**

Run:

```bash
cd backend && ./.venv/bin/python -m pytest tests/test_watchlist.py -v
```

Expected: all tests in `test_watchlist.py` pass.

- [x] **Step 2: Run backend type-check**

Run:

```bash
cd backend && ./.venv/bin/basedpyright
```

Expected: 0 errors / 0 warnings / 0 notes.

- [x] **Step 3: Run frontend type-check and build**

Run:

```bash
cd frontend && npm run type-check && npm run build
```

Expected: type-check and build pass. Existing Vite chunk-size warning is acceptable.

- [x] **Step 4: Run Dashboard Cypress spec**

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/dashboard.cy.ts
```

Expected: dashboard spec passes.

- [x] **Step 5: Update roadmap**

In `docs/Roadmap.md`, mark P27 complete only after all checks pass:

```markdown
| 已完成 | **P27 多标的配置与只读监控 MVP** | ✅ 2026-06-04 | 新增 `/api/watchlist/snapshots` 只读快照 API；Dashboard 新增多标的观察表；保持 `/api/status`、WebSocket、runner 自动交易路径单标的不变。验证：`test_watchlist.py` passed / basedpyright 0/0/0 / frontend type-check + build / Dashboard Cypress 通过。 |
```

Do not commit unless the user explicitly requests a commit.

---

## Self-review

### Spec coverage

- P26 要求 P27 只读多标的监控：Task 1-3 覆盖。
- P26 禁止改交易路径：本计划不触碰 `AppRunner`、`StrategyEngine`、`RuntimeState`、`TradeExecutionService`。
- P26 要求测试：Task 1 后端 API；Task 3 Cypress；Task 4 verification。

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
- 所有代码步骤包含具体代码块。
- 所有验证步骤包含精确命令和预期。

### Type consistency

- Backend schema 名称：`WatchlistSnapshot`。
- Frontend interface 名称：`WatchlistSnapshot`。
- API 函数名称：`getWatchlistSnapshots()`。
- Endpoint：`GET /api/watchlist/snapshots`。
