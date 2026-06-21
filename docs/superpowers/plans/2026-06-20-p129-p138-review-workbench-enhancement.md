# P129–P138 Review Workbench Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ten read-only Review page workbench helpers for faster post-trade triage, filtering, summarizing, copying, exporting, and preference persistence.

**Architecture:** Keep all production behavior in `frontend/src/views/Review.vue`, deriving new state from the already-loaded `ReviewResponse`. Add one focused Cypress spec for P129–P138 and reuse `frontend/src/utils/csv.ts`; do not add backend APIs, database tables, order/risk/runner/broker changes, or new dependencies.

**Tech Stack:** Vue 3 `<script setup lang="ts">`, Element Plus, TypeScript strict mode, Cypress E2E, existing CSV helper.

---

## File Map

- Modify: `frontend/src/views/Review.vue`
  - Add local UI state for Review workbench filters, compact mode, keyword, and persisted preferences.
  - Add computed summaries for health score, LLM actions, execution quality, event buckets, snapshots, visible timeline rows, and copy brief text.
  - Add template sections for “复盘洞察” and the timeline workbench toolbar.
  - Change the day timeline `v-for` from `reviewData.days` to `filteredReviewDays` while preserving the existing raw `reviewData` and backend export behavior.
- Add: `frontend/cypress/e2e/review_workbench.cy.ts`
  - Cover all P129–P138 user-visible behaviors using `cy.stubApi()` and targeted `/api/review*` intercepts.
- Modify if needed: `frontend/cypress/support/e2e.ts`
  - Only if the current shared Review stub lacks enough variety for the focused spec. Prefer spec-local intercepts first.
- Reuse: `frontend/src/utils/csv.ts`
  - Use `downloadCsv()` for P137 local CSV export.

Do not modify backend files for this batch.

---

## Shared Implementation Notes

Add the following constants/types near the other local types in `Review.vue`:

```ts
type ReviewDayFilter = 'all' | 'losing' | 'winning' | 'error' | 'traded' | 'no_trade' | 'llm' | 'event'

interface ReviewHealth {
  score: number
  label: '健康' | '需关注' | '高风险'
  tagType: 'success' | 'warning' | 'danger'
  reasons: string[]
}

interface LLMActionSummaryItem {
  action: string
  count: number
}

interface ExecutionQualitySummary {
  filled: number
  partial: number
  open: number
  failed: number
  slippageSamples: number[]
  avgSlippage: number | null
}

interface EventBucketSummary {
  risk: number
  order: number
  session: number
  broker: number
  llm: number
  other: number
}

interface SnapshotSummary {
  count: number
  minPrice: number | null
  maxPrice: number | null
  latestState: string
  maxConsecutiveLosses: number
  maxTriggerDistance: number | null
}

interface VisibleTimelineRow {
  date: string
  source: string
  symbol: string
  type: string
  status: string
  side: string
  message: string
  pnl: number | string
  created_at: string
  broker_order_id: string
}
```

Add the following local refs near the existing `form`/loading state:

```ts
const WORKBENCH_PREFS_KEY = 'auto_trade.review.workbench.v1'
const dayFilter = ref<ReviewDayFilter>('all')
const timelineKeyword = ref('')
const compactReviewMode = ref(false)
```

Use these helper functions in `Review.vue`:

```ts
function normalizeText(value: unknown): string {
  return String(value ?? '').toLowerCase()
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function statusText(value: string | null | undefined): string {
  return String(value ?? '').toUpperCase()
}

function dayMatchesQuickFilter(day: ReviewDay, filter: ReviewDayFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'losing') return day.daily_pnl < 0
  if (filter === 'winning') return day.daily_pnl > 0
  if (filter === 'error') return day.error_tags.length > 0
  if (filter === 'traded') return day.trade_count > 0
  if (filter === 'no_trade') return day.trade_count === 0
  if (filter === 'llm') return day.llm_interactions.length > 0
  return day.events.length > 0
}

function eventSearchText(day: ReviewDay): string {
  return [
    day.date,
    day.symbol,
    ...day.error_tags,
    ...day.llm_interactions.flatMap((item) => [item.symbol, item.order_action, item.order_status, item.interaction_type]),
    ...day.orders.flatMap((item) => [item.broker_order_id, item.symbol, item.side, item.status]),
    ...day.events.flatMap((item) => [item.event_type, item.symbol, item.side, item.status, item.message, item.payload_json]),
    ...day.snapshots.map((item) => item.engine_state),
  ].map(normalizeText).join(' ')
}
```

Import CSV helper when implementing P137:

```ts
import { downloadCsv } from '../utils/csv'
```

---

## Task 1: P129 Review Health Score

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Add: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Write failing Cypress coverage**

Create `frontend/cypress/e2e/review_workbench.cy.ts` with this baseline test:

```ts
describe('Review Workbench Enhancements', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/review*', {
      body: {
        symbol: 'AAPL.US',
        from_date: '2026-06-01',
        to_date: '2026-06-03',
        total_pnl: -125,
        total_trades: 3,
        all_error_tags: ['ORDER_REJECTED'],
        days: [
          {
            date: '2026-06-01', symbol: 'AAPL.US', daily_pnl: 180, trade_count: 2, error_tags: [],
            llm_interactions: [
              { id: 1, interaction_type: 'analyze', symbol: 'AAPL.US', market: 'US', success: true, order_action: 'BUY', order_status: 'FILLED', order_id: 'o1', applied: true, created_at: '2026-06-01T10:00:00Z' },
            ],
            orders: [
              { id: 1, broker_order_id: 'o1', symbol: 'AAPL.US', side: 'BUY', quantity: 10, price: 100, executed_quantity: 10, executed_price: 100.2, status: 'FILLED', created_at: '2026-06-01T10:01:00Z', filled_at: '2026-06-01T10:02:00Z' },
            ],
            events: [
              { id: 1, event_type: 'ORDER_FILLED', symbol: 'AAPL.US', broker_order_id: 'o1', side: 'BUY', status: 'FILLED', message: 'order filled', payload_json: '{}', created_at: '2026-06-01T10:02:00Z' },
            ],
            snapshots: [
              { id: 1, engine_state: 'LONG', daily_pnl: 180, consecutive_losses: 0, last_price: 101, last_trigger_price: 100, created_at: '2026-06-01T10:03:00Z' },
            ],
          },
          {
            date: '2026-06-02', symbol: 'AAPL.US', daily_pnl: -305, trade_count: 1, error_tags: ['ORDER_REJECTED'],
            llm_interactions: [
              { id: 2, interaction_type: 'analyze', symbol: 'AAPL.US', market: 'US', success: false, order_action: 'SELL', order_status: 'FAILED', order_id: null, applied: false, created_at: '2026-06-02T10:00:00Z' },
              { id: 3, interaction_type: 'preview', symbol: 'AAPL.US', market: 'US', success: true, order_action: 'NONE', order_status: null, order_id: null, applied: false, created_at: '2026-06-02T11:00:00Z' },
            ],
            orders: [
              { id: 2, broker_order_id: 'o2', symbol: 'AAPL.US', side: 'SELL', quantity: 10, price: 101, executed_quantity: 4, executed_price: 100.5, status: 'PARTIAL_FILLED', created_at: '2026-06-02T10:01:00Z', filled_at: '2026-06-02T10:02:00Z' },
              { id: 3, broker_order_id: 'o3', symbol: 'AAPL.US', side: 'BUY', quantity: 10, price: 99, executed_quantity: null, executed_price: null, status: 'REJECTED', created_at: '2026-06-02T10:05:00Z', filled_at: null },
            ],
            events: [
              { id: 2, event_type: 'RISK_PAUSED', symbol: 'AAPL.US', broker_order_id: '', side: '', status: 'WARNING', message: 'risk pause', payload_json: '{"reason":"daily loss"}', created_at: '2026-06-02T10:10:00Z' },
              { id: 3, event_type: 'BROKER_RETRY', symbol: 'AAPL.US', broker_order_id: '', side: '', status: 'WARNING', message: 'quote retry', payload_json: '{}', created_at: '2026-06-02T10:11:00Z' },
              { id: 4, event_type: 'TRADING_SESSION_BLOCKED', symbol: 'AAPL.US', broker_order_id: '', side: '', status: 'INFO', message: 'outside RTH', payload_json: '{}', created_at: '2026-06-02T10:12:00Z' },
            ],
            snapshots: [
              { id: 2, engine_state: 'FLAT', daily_pnl: -305, consecutive_losses: 2, last_price: 97, last_trigger_price: 101, created_at: '2026-06-02T10:13:00Z' },
            ],
          },
          {
            date: '2026-06-03', symbol: 'AAPL.US', daily_pnl: 0, trade_count: 0, error_tags: [],
            llm_interactions: [], orders: [], events: [], snapshots: [],
          },
        ],
      },
    }).as('getReviewWorkbench')
    cy.visitApp('/review')
    cy.contains('button', '查询').click()
    cy.wait('@getReviewWorkbench')
  })

  it('shows review health score for current query', () => {
    cy.get('[data-testid="review-health-score"]').should('contain', '复盘健康')
    cy.get('[data-testid="review-health-score"]').should('contain', '需关注')
    cy.get('[data-testid="review-health-score"]').should('contain', '当前查询结果')
    cy.get('[data-testid="review-health-reasons"]').should('contain', '区间亏损')
    cy.get('[data-testid="review-health-reasons"]').should('contain', '存在错误')
  })
})
```

- [ ] **Step 2: Run the spec and verify RED**

Run from `frontend/`:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
```

Expected: FAIL because `[data-testid="review-health-score"]` does not exist.

- [ ] **Step 3: Implement health computed and card**

In `Review.vue`, add `reviewHealth`:

```ts
const reviewHealth = computed<ReviewHealth | null>(() => {
  if (!reviewData.value) return null
  let score = 100
  const reasons: string[] = []
  if (reviewData.value.total_pnl < 0) {
    score -= 25
    reasons.push('区间亏损')
  }
  if (reviewData.value.all_error_tags.length > 0) {
    score -= Math.min(25, reviewData.value.all_error_tags.length * 10)
    reasons.push('存在错误')
  }
  const allOrders = reviewData.value.days.flatMap((day) => day.orders)
  if (allOrders.some((order) => /REJECT|FAIL|CANCEL/i.test(order.status))) {
    score -= 15
    reasons.push('订单异常')
  }
  const failedLlm = reviewData.value.days.flatMap((day) => day.llm_interactions).filter((item) => !item.success).length
  if (failedLlm > 0) {
    score -= Math.min(15, failedLlm * 5)
    reasons.push('LLM 失败')
  }
  const maxLosses = Math.max(0, ...reviewData.value.days.flatMap((day) => day.snapshots.map((snapshot) => snapshot.consecutive_losses)))
  if (maxLosses >= 2) {
    score -= 10
    reasons.push('连亏压力')
  }
  score = Math.max(0, Math.min(100, score))
  const label = score >= 80 ? '健康' : score >= 50 ? '需关注' : '高风险'
  const tagType = score >= 80 ? 'success' : score >= 50 ? 'warning' : 'danger'
  return { score, label, tagType, reasons: reasons.slice(0, 3) }
})
```

Add a card after the existing summary row:

```vue
<el-card v-if="reviewHealth" class="review-workbench-card" data-testid="review-health-score">
  <template #header>
    <div class="runtime-history-header">
      <div>
        <strong>复盘健康</strong>
        <p>当前查询结果 · 诊断分，不是交易建议</p>
      </div>
      <el-tag :type="reviewHealth.tagType">{{ reviewHealth.label }}</el-tag>
    </div>
  </template>
  <div class="health-score-value">{{ reviewHealth.score }}</div>
  <div class="health-reasons" data-testid="review-health-reasons">
    <el-tag v-for="reason in reviewHealth.reasons" :key="reason" size="small" type="warning">{{ reason }}</el-tag>
    <span v-if="reviewHealth.reasons.length === 0" class="muted">无明显风险信号</span>
  </div>
</el-card>
```

- [ ] **Step 4: Run spec and type-check**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
npm run type-check
```

Expected: Cypress test passes for P129; type-check passes.

---

## Task 2: P130 Day Quick Filters

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Extend failing Cypress test**

Append this test to `review_workbench.cy.ts`:

```ts
it('filters review days by quick day filters and resets filters', () => {
  cy.get('[data-testid="review-day-filter-losing"]').click()
  cy.get('[data-testid="review-visible-day-count"]').should('contain', '1')
  cy.contains('.day-card', '2026-06-02').should('be.visible')
  cy.contains('.day-card', '2026-06-01').should('not.exist')

  cy.get('[data-testid="review-day-filter-error"]').click()
  cy.contains('.day-card', '2026-06-02').should('be.visible')

  cy.get('[data-testid="review-day-filter-no-trade"]').click()
  cy.contains('.day-card', '2026-06-03').should('be.visible')

  cy.get('[data-testid="review-reset-workbench-filters"]').click()
  cy.get('[data-testid="review-visible-day-count"]').should('contain', '3')
})
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
```

Expected: FAIL because quick filter controls do not exist.

- [ ] **Step 3: Add computed filtered days and toolbar**

Add:

```ts
const filteredReviewDays = computed(() => {
  const days = reviewData.value?.days ?? []
  const keyword = timelineKeyword.value.trim().toLowerCase()
  return days.filter((day) => {
    if (!dayMatchesQuickFilter(day, dayFilter.value)) return false
    if (!keyword) return true
    return eventSearchText(day).includes(keyword)
  })
})

function setDayFilter(value: ReviewDayFilter) {
  dayFilter.value = value
}

function resetWorkbenchFilters() {
  dayFilter.value = 'all'
  timelineKeyword.value = ''
}
```

Add toolbar before `<div class="timeline-section">`:

```vue
<el-card class="review-workbench-toolbar" data-testid="review-workbench-toolbar">
  <div class="toolbar-row">
    <span data-testid="review-visible-day-count">当前筛选结果：{{ filteredReviewDays.length }} / {{ reviewData.days.length }} 天</span>
    <el-button-group>
      <el-button data-testid="review-day-filter-all" :type="dayFilter === 'all' ? 'primary' : ''" @click="setDayFilter('all')">全部</el-button>
      <el-button data-testid="review-day-filter-losing" :type="dayFilter === 'losing' ? 'primary' : ''" @click="setDayFilter('losing')">亏损日</el-button>
      <el-button data-testid="review-day-filter-winning" :type="dayFilter === 'winning' ? 'primary' : ''" @click="setDayFilter('winning')">盈利日</el-button>
      <el-button data-testid="review-day-filter-error" :type="dayFilter === 'error' ? 'primary' : ''" @click="setDayFilter('error')">有错误</el-button>
      <el-button data-testid="review-day-filter-traded" :type="dayFilter === 'traded' ? 'primary' : ''" @click="setDayFilter('traded')">有交易</el-button>
      <el-button data-testid="review-day-filter-no-trade" :type="dayFilter === 'no_trade' ? 'primary' : ''" @click="setDayFilter('no_trade')">无交易</el-button>
      <el-button data-testid="review-day-filter-llm" :type="dayFilter === 'llm' ? 'primary' : ''" @click="setDayFilter('llm')">有 LLM</el-button>
      <el-button data-testid="review-day-filter-event" :type="dayFilter === 'event' ? 'primary' : ''" @click="setDayFilter('event')">有事件</el-button>
    </el-button-group>
    <el-button data-testid="review-reset-workbench-filters" @click="resetWorkbenchFilters">重置</el-button>
  </div>
</el-card>
```

Change day rendering:

```vue
<div v-for="day in filteredReviewDays" :key="day.date" class="day-card">
```

Add filtered empty state before the day `v-for`:

```vue
<el-empty v-if="filteredReviewDays.length === 0" data-testid="review-filtered-empty" description="当前筛选条件下无复盘日">
  <el-button @click="resetWorkbenchFilters">清除筛选</el-button>
</el-empty>
```

- [ ] **Step 4: Run spec and type-check**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
npm run type-check
```

Expected: P129 and P130 tests pass; type-check passes.

---

## Task 3: P131 Timeline Keyword Filter

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('filters current review timeline by keyword and shows filtered empty state', () => {
  cy.get('[data-testid="review-keyword-filter"]').type('BROKER_RETRY')
  cy.contains('.day-card', '2026-06-02').should('be.visible')
  cy.contains('.day-card', '2026-06-01').should('not.exist')

  cy.get('[data-testid="review-keyword-filter"]').clear().type('not-found-keyword')
  cy.get('[data-testid="review-filtered-empty"]').should('contain', '当前筛选条件下无复盘日')
})
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
```

Expected: FAIL because keyword input does not exist.

- [ ] **Step 3: Add keyword input**

In the workbench toolbar, add:

```vue
<el-input
  v-model="timelineKeyword"
  data-testid="review-keyword-filter"
  placeholder="搜索当前复盘：订单/事件/LLM/快照"
  clearable
  style="width: 260px"
/>
```

The `filteredReviewDays` computed from Task 2 already applies this keyword.

- [ ] **Step 4: Run spec**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
```

Expected: P131 test passes.

---

## Task 4: P132 LLM Action Summary

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('summarizes LLM actions for visible review days', () => {
  cy.get('[data-testid="review-llm-action-summary"]').should('contain', 'BUY 1')
  cy.get('[data-testid="review-llm-action-summary"]').should('contain', 'SELL 1')
  cy.get('[data-testid="review-llm-action-summary"]').should('contain', 'NONE 1')
  cy.get('[data-testid="review-llm-action-summary"]').should('contain', '成功 2/3')
  cy.get('[data-testid="review-llm-action-summary"]').should('contain', '已应用 1')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because LLM summary does not exist.

- [ ] **Step 3: Implement computed and template**

Add:

```ts
const visibleLlmInteractions = computed(() => filteredReviewDays.value.flatMap((day) => day.llm_interactions))

const llmActionSummary = computed(() => {
  const counts = new Map<string, number>()
  for (const item of visibleLlmInteractions.value) {
    const action = item.order_action || 'NONE'
    counts.set(action, (counts.get(action) ?? 0) + 1)
  }
  const actions = Array.from(counts.entries()).map(([action, count]) => ({ action, count }))
  const success = visibleLlmInteractions.value.filter((item) => item.success).length
  const applied = visibleLlmInteractions.value.filter((item) => item.applied).length
  const linked = visibleLlmInteractions.value.filter((item) => Boolean(item.order_id)).length
  return { actions, total: visibleLlmInteractions.value.length, success, applied, linked }
})
```

Add inside “复盘洞察” area:

```vue
<div class="section-block" data-testid="review-llm-action-summary">
  <div class="section-title">LLM 动作摘要（当前筛选结果）</div>
  <el-tag v-for="item in llmActionSummary.actions" :key="item.action" size="small" type="info">
    {{ item.action }} {{ item.count }}
  </el-tag>
  <span>成功 {{ llmActionSummary.success }}/{{ llmActionSummary.total }}</span>
  <span>已应用 {{ llmActionSummary.applied }}</span>
  <span>关联订单 {{ llmActionSummary.linked }}</span>
</div>
```

- [ ] **Step 4: Run spec and type-check**

Run Cypress spec and `npm run type-check`. Expected: pass.

---

## Task 5: P133 Order Execution Quality Summary

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('summarizes order execution quality for visible review days', () => {
  cy.get('[data-testid="review-execution-quality"]').should('contain', '成交 1')
  cy.get('[data-testid="review-execution-quality"]').should('contain', '部分 1')
  cy.get('[data-testid="review-execution-quality"]').should('contain', '异常 1')
  cy.get('[data-testid="review-execution-quality"]').should('contain', '平均滑点')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because execution summary does not exist.

- [ ] **Step 3: Implement computed and template**

Add:

```ts
const visibleOrders = computed(() => filteredReviewDays.value.flatMap((day) => day.orders))

const executionQuality = computed<ExecutionQualitySummary>(() => {
  const summary: ExecutionQualitySummary = { filled: 0, partial: 0, open: 0, failed: 0, slippageSamples: [], avgSlippage: null }
  for (const order of visibleOrders.value) {
    const status = statusText(order.status)
    const quantity = order.quantity
    const executed = order.executed_quantity ?? 0
    if (/REJECT|FAIL|CANCEL/.test(status)) summary.failed += 1
    else if (/PARTIAL/.test(status) || (executed > 0 && executed < quantity)) summary.partial += 1
    else if (/FILLED/.test(status) || (executed > 0 && executed >= quantity)) summary.filled += 1
    else summary.open += 1

    if (isFiniteNumber(order.executed_price) && isFiniteNumber(order.price)) {
      summary.slippageSamples.push(order.executed_price - order.price)
    }
  }
  if (summary.slippageSamples.length > 0) {
    summary.avgSlippage = summary.slippageSamples.reduce((sum, value) => sum + value, 0) / summary.slippageSamples.length
  }
  return summary
})
```

Add:

```vue
<div class="section-block" data-testid="review-execution-quality">
  <div class="section-title">订单执行质量（当前筛选结果）</div>
  <span>成交 {{ executionQuality.filled }}</span>
  <span>部分 {{ executionQuality.partial }}</span>
  <span>挂起 {{ executionQuality.open }}</span>
  <span>异常 {{ executionQuality.failed }}</span>
  <span>平均滑点 {{ executionQuality.avgSlippage === null ? '-' : executionQuality.avgSlippage.toFixed(2) }}</span>
</div>
```

- [ ] **Step 4: Run spec**

Run focused Cypress. Expected: pass.

---

## Task 6: P134 Event Severity Buckets

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('groups visible events into deterministic triage buckets', () => {
  cy.get('[data-testid="review-event-buckets"]').should('contain', '风险 1')
  cy.get('[data-testid="review-event-buckets"]').should('contain', '订单 1')
  cy.get('[data-testid="review-event-buckets"]').should('contain', '时段 1')
  cy.get('[data-testid="review-event-buckets"]').should('contain', '券商 1')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because event buckets do not exist.

- [ ] **Step 3: Implement bucketing**

Add:

```ts
const visibleEvents = computed(() => filteredReviewDays.value.flatMap((day) => day.events))

function classifyReviewEvent(text: string): keyof EventBucketSummary {
  const value = text.toUpperCase()
  if (/RISK|LOSS|KILL|PAUSE/.test(value)) return 'risk'
  if (/ORDER|FILLED|REJECT|CANCEL|TIMEOUT/.test(value)) return 'order'
  if (/SESSION|RTH|MARKET/.test(value)) return 'session'
  if (/BROKER|QUOTE|LONGPORT|STREAM/.test(value)) return 'broker'
  if (/LLM|ADVISOR|INTERVAL/.test(value)) return 'llm'
  return 'other'
}

const eventBuckets = computed<EventBucketSummary>(() => {
  const buckets: EventBucketSummary = { risk: 0, order: 0, session: 0, broker: 0, llm: 0, other: 0 }
  for (const event of visibleEvents.value) {
    const key = classifyReviewEvent(`${event.event_type} ${event.message} ${event.payload_json}`)
    buckets[key] += 1
  }
  return buckets
})
```

Add:

```vue
<div class="section-block" data-testid="review-event-buckets">
  <div class="section-title">事件分桶（当前筛选结果）</div>
  <span>风险 {{ eventBuckets.risk }}</span>
  <span>订单 {{ eventBuckets.order }}</span>
  <span>时段 {{ eventBuckets.session }}</span>
  <span>券商 {{ eventBuckets.broker }}</span>
  <span>LLM {{ eventBuckets.llm }}</span>
  <span>其他 {{ eventBuckets.other }}</span>
</div>
```

- [ ] **Step 4: Run spec and type-check**

Expected: pass.

---

## Task 7: P135 Snapshot Volatility Strip

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('shows snapshot volatility strip for visible days', () => {
  cy.get('[data-testid="review-snapshot-strip"]').should('contain', '样本 2')
  cy.get('[data-testid="review-snapshot-strip"]').should('contain', '最低 97')
  cy.get('[data-testid="review-snapshot-strip"]').should('contain', '最高 101')
  cy.get('[data-testid="review-snapshot-strip"]').should('contain', '最大连亏 2')
  cy.get('[data-testid="review-snapshot-strip"]').should('contain', '触发距离 4')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because snapshot strip does not exist.

- [ ] **Step 3: Implement snapshot summary**

Add:

```ts
const visibleSnapshots = computed(() => filteredReviewDays.value.flatMap((day) => day.snapshots))

const snapshotSummary = computed<SnapshotSummary>(() => {
  const snapshots = visibleSnapshots.value
  const prices = snapshots.map((item) => item.last_price).filter(isFiniteNumber)
  const triggerDistances = snapshots
    .filter((item) => isFiniteNumber(item.last_price) && isFiniteNumber(item.last_trigger_price) && item.last_trigger_price > 0)
    .map((item) => Math.abs(item.last_price - item.last_trigger_price))
  const latest = [...snapshots].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
  return {
    count: snapshots.length,
    minPrice: prices.length ? Math.min(...prices) : null,
    maxPrice: prices.length ? Math.max(...prices) : null,
    latestState: latest?.engine_state ?? '-',
    maxConsecutiveLosses: snapshots.length ? Math.max(...snapshots.map((item) => item.consecutive_losses ?? 0)) : 0,
    maxTriggerDistance: triggerDistances.length ? Math.max(...triggerDistances) : null,
  }
})
```

Add:

```vue
<div class="section-block" data-testid="review-snapshot-strip">
  <div class="section-title">快照波动（当前筛选结果）</div>
  <template v-if="snapshotSummary.count > 0">
    <span>样本 {{ snapshotSummary.count }}</span>
    <span>最低 {{ snapshotSummary.minPrice === null ? '-' : snapshotSummary.minPrice.toFixed(0) }}</span>
    <span>最高 {{ snapshotSummary.maxPrice === null ? '-' : snapshotSummary.maxPrice.toFixed(0) }}</span>
    <span>最新状态 {{ snapshotSummary.latestState }}</span>
    <span>最大连亏 {{ snapshotSummary.maxConsecutiveLosses }}</span>
    <span>触发距离 {{ snapshotSummary.maxTriggerDistance === null ? '-' : snapshotSummary.maxTriggerDistance.toFixed(0) }}</span>
  </template>
  <span v-else>无快照样本</span>
</div>
```

- [ ] **Step 4: Run spec**

Expected: pass.

---

## Task 8: P136 Copy Review Brief

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('copies a concise review brief', () => {
  cy.window().then((win) => {
    cy.stub(win.navigator.clipboard, 'writeText').as('writeText').resolves()
  })
  cy.get('[data-testid="review-copy-brief"]').click()
  cy.get('@writeText').should('have.been.calledWithMatch', /AAPL\.US/)
  cy.get('@writeText').should('have.been.calledWithMatch', /复盘健康/)
  cy.document().its('body').should('contain', '复盘摘要已复制')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because copy button does not exist.

- [ ] **Step 3: Implement brief computed and copy action**

Add:

```ts
const reviewBriefText = computed(() => {
  if (!reviewData.value || !reviewHealth.value) return ''
  const actionText = llmActionSummary.value.actions.map((item) => `${item.action}:${item.count}`).join(', ') || '无'
  return [
    `复盘摘要 ${reviewData.value.symbol} ${reviewData.value.from_date}~${reviewData.value.to_date}`,
    `复盘健康 ${reviewHealth.value.label} ${reviewHealth.value.score}`,
    `总盈亏 ${reviewData.value.total_pnl.toFixed(2)}，交易 ${reviewData.value.total_trades}，当前筛选 ${filteredReviewDays.value.length} 天`,
    `错误标签 ${reviewData.value.all_error_tags.join(', ') || '无'}`,
    `LLM 动作 ${actionText}`,
    `订单质量 成交${executionQuality.value.filled}/部分${executionQuality.value.partial}/异常${executionQuality.value.failed}`,
  ].join('\n')
})

async function copyReviewBrief() {
  if (!reviewBriefText.value) return
  try {
    await navigator.clipboard.writeText(reviewBriefText.value)
    ElMessage.success('复盘摘要已复制')
  } catch {
    ElMessage.error('复制失败，请检查浏览器剪贴板权限')
  }
}
```

Add button to toolbar:

```vue
<el-button data-testid="review-copy-brief" :disabled="!reviewBriefText" @click="copyReviewBrief">复制摘要</el-button>
```

- [ ] **Step 4: Run spec**

Expected: pass.

---

## Task 9: P137 Local CSV Export for Visible Timeline

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('exports visible review timeline rows as local CSV', () => {
  cy.get('[data-testid="review-day-filter-losing"]').click()
  cy.get('[data-testid="review-export-visible-csv"]').click()
  cy.document().its('body').should('contain', '已导出当前筛选复盘 CSV')
})
```

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because visible CSV export button does not exist.

- [ ] **Step 3: Implement visible rows and export**

Add import:

```ts
import { downloadCsv } from '../utils/csv'
```

Add computed/action:

```ts
const visibleTimelineRows = computed<VisibleTimelineRow[]>(() => {
  return filteredReviewDays.value.flatMap((day) => [
    ...day.llm_interactions.map((item) => ({ date: day.date, source: 'llm', symbol: item.symbol, type: item.interaction_type, status: item.success ? 'SUCCESS' : 'FAILED', side: item.order_action, message: item.applied ? 'applied' : 'not_applied', pnl: '', created_at: item.created_at, broker_order_id: item.order_id ?? '' })),
    ...day.orders.map((item) => ({ date: day.date, source: 'order', symbol: item.symbol, type: 'order', status: item.status, side: item.side, message: item.filled_at ?? '', pnl: '', created_at: item.created_at, broker_order_id: item.broker_order_id })),
    ...day.events.map((item) => ({ date: day.date, source: 'event', symbol: item.symbol, type: item.event_type, status: item.status, side: item.side, message: item.message, pnl: '', created_at: item.created_at, broker_order_id: item.broker_order_id })),
    ...day.snapshots.map((item) => ({ date: day.date, source: 'snapshot', symbol: day.symbol, type: item.engine_state, status: '', side: '', message: `price=${item.last_price}`, pnl: item.daily_pnl, created_at: item.created_at, broker_order_id: '' })),
  ])
})

function exportVisibleTimelineCsv() {
  if (visibleTimelineRows.value.length === 0) {
    ElMessage.warning('当前筛选结果无可导出的复盘行')
    return
  }
  downloadCsv(`review-visible-${form.symbol || 'all'}-${form.from_date || 'from'}-${form.to_date || 'to'}.csv`, [
    { key: 'date', label: '日期' },
    { key: 'source', label: '来源' },
    { key: 'symbol', label: '标的' },
    { key: 'type', label: '类型' },
    { key: 'status', label: '状态' },
    { key: 'side', label: '方向' },
    { key: 'message', label: '消息' },
    { key: 'pnl', label: '盈亏' },
    { key: 'created_at', label: '时间' },
    { key: 'broker_order_id', label: '订单号' },
  ], visibleTimelineRows.value as unknown as Record<string, unknown>[])
  ElMessage.success('已导出当前筛选复盘 CSV')
}
```

Add button:

```vue
<el-button data-testid="review-export-visible-csv" :disabled="visibleTimelineRows.length === 0" @click="exportVisibleTimelineCsv">导出当前筛选 CSV</el-button>
```

- [ ] **Step 4: Run spec and type-check**

Expected: pass.

---

## Task 10: P138 Preference Persistence and Final Polish

**Files:**
- Modify: `frontend/src/views/Review.vue`
- Modify: `frontend/cypress/e2e/review_workbench.cy.ts`

- [ ] **Step 1: Add failing Cypress test**

```ts
it('persists review workbench preferences across reload', () => {
  cy.get('[data-testid="review-day-filter-losing"]').click()
  cy.get('[data-testid="review-keyword-filter"]').type('RISK')
  cy.get('[data-testid="review-compact-mode"]').click()
  cy.reload()
  cy.contains('button', '查询').click()
  cy.wait('@getReviewWorkbench')
  cy.get('[data-testid="review-day-filter-losing"]').should('have.class', 'el-button--primary')
  cy.get('[data-testid="review-keyword-filter"]').find('input').should('have.value', 'RISK')
  cy.get('[data-testid="review-page-root"]').should('have.class', 'review-compact')
})
```

Before this passes, add `data-testid="review-page-root"` to the root `<div class="review-page">`.

- [ ] **Step 2: Run and verify RED**

Expected: FAIL because compact mode and persistence do not exist.

- [ ] **Step 3: Implement persistence**

Update root template:

```vue
<div class="review-page" :class="{ 'review-compact': compactReviewMode }" data-testid="review-page-root">
```

Add compact button to toolbar:

```vue
<el-button data-testid="review-compact-mode" :type="compactReviewMode ? 'primary' : ''" @click="compactReviewMode = !compactReviewMode">紧凑模式</el-button>
```

Add persistence code:

```ts
function loadWorkbenchPrefs() {
  try {
    const raw = localStorage.getItem(WORKBENCH_PREFS_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as { dayFilter?: ReviewDayFilter; keyword?: string; compact?: boolean }
    const validFilters: ReviewDayFilter[] = ['all', 'losing', 'winning', 'error', 'traded', 'no_trade', 'llm', 'event']
    if (parsed.dayFilter && validFilters.includes(parsed.dayFilter)) dayFilter.value = parsed.dayFilter
    if (typeof parsed.keyword === 'string') timelineKeyword.value = parsed.keyword
    if (typeof parsed.compact === 'boolean') compactReviewMode.value = parsed.compact
  } catch {
    // localStorage is a weak dependency; invalid user/browser data should not block Review.
  }
}

function saveWorkbenchPrefs() {
  try {
    localStorage.setItem(WORKBENCH_PREFS_KEY, JSON.stringify({
      dayFilter: dayFilter.value,
      keyword: timelineKeyword.value,
      compact: compactReviewMode.value,
    }))
  } catch {
    // localStorage persistence is optional; UI remains usable without it.
  }
}
```

Call in lifecycle/watch section:

```ts
onMounted(() => {
  loadWorkbenchPrefs()
  // keep existing mounted logic here
})

watch([dayFilter, timelineKeyword, compactReviewMode], saveWorkbenchPrefs)
```

If `Review.vue` already has an `onMounted`, merge `loadWorkbenchPrefs()` into it instead of adding a duplicate import block.

Add compact CSS:

```css
.review-compact .item-row,
.review-compact .day-meta-row {
  gap: 6px;
  margin-bottom: 4px;
}

.review-compact .section-block {
  padding: 8px;
}
```

- [ ] **Step 4: Run focused Cypress and frontend checks**

Run:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
npm run type-check
npm run build
```

Expected: all pass.

- [ ] **Step 5: Inspect diff and verify scope**

Run:

```bash
git diff -- frontend/src/views/Review.vue frontend/cypress/e2e/review_workbench.cy.ts frontend/cypress/support/e2e.ts
```

Expected: only Review workbench UI/computed/test changes. No backend, broker, runner, risk, or order write-path changes.

---

## Final Verification

Run from `frontend/`:

```bash
npm run cypress:run -- --spec cypress/e2e/review_workbench.cy.ts
npm run type-check
npm run build
```

If any shared Cypress support is modified, also run the nearest existing Review specs:

```bash
npm run cypress:run -- --spec cypress/e2e/review_*.cy.ts,cypress/e2e/reports.cy.ts
```

Backend validation is not required if no backend files are changed. If backend files are touched accidentally, revert them or run:

```bash
cd ../backend
.venv/bin/python -m pytest tests/ -q
```

## Self-Review Checklist

- [ ] P129 health score card implemented and tested.
- [ ] P130 day quick filters implemented and tested.
- [ ] P131 keyword filter implemented and tested.
- [ ] P132 LLM action summary implemented and tested.
- [ ] P133 execution quality summary implemented and tested.
- [ ] P134 event buckets implemented and tested.
- [ ] P135 snapshot strip implemented and tested.
- [ ] P136 copy brief implemented and tested.
- [ ] P137 local visible CSV export implemented and tested.
- [ ] P138 preferences persistence implemented and tested.
- [ ] No new backend endpoints, tables, or trading write-path changes.
- [ ] All derived labels say current query/current filter where applicable.
- [ ] `npm run type-check` passes.
- [ ] `npm run build` passes.
