<template>
  <div class="review-page">
    <div class="review-header">
      <div>
        <h3>复盘工作台</h3>
        <p>分析 LLM 建议与真实交易结果，优化策略决策</p>
      </div>
    </div>

    <el-card class="filter-card">
      <el-form :inline="true" @submit.prevent="handleSearch">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" style="width: 160px" />
        </el-form-item>
        <el-form-item label="开始日期">
          <el-date-picker v-model="form.from_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item label="结束日期">
          <el-date-picker v-model="form.to_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleSearch">查询</el-button>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('json')">导出 JSON</el-button>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('csv')">导出 CSV</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="reviewData && reviewData.days.length > 0">
      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="pnlClass">{{ signedCurrency(reviewData.total_pnl) }}</div>
            <div class="summary-label">总盈亏</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.total_trades }}</div>
            <div class="summary-label">总交易次数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.days.length }}</div>
            <div class="summary-label">复盘天数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value error-tags">
              <el-tag v-for="tag in reviewData.all_error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
              <span v-if="reviewData.all_error_tags.length === 0" class="no-tag">无</span>
            </div>
            <div class="summary-label">错误标签</div>
          </el-card>
        </el-col>
      </el-row>

      <el-card class="runtime-history-card" data-testid="review-runtime-history">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>运行时状态历史</strong>
              <p>{{ reviewSymbolLabel }}</p>
            </div>
            <el-tag size="small" type="info">{{ runtimeHistory.points.length }} 个样本</el-tag>
          </div>
        </template>

        <el-alert
          v-if="runtimeHistoryError"
          :title="runtimeHistoryError"
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
        />

        <el-empty
          v-if="!runtimeHistoryLoading && runtimeHistory.points.length === 0"
          description="当前条件下暂无运行时状态历史"
        />
        <div v-else class="chart-grid">
          <PriceChart :points="runtimeHistory.points" :markers="runtimeHistory.markers" :buy-low="0" :sell-high="0" />
          <PnLChart :points="runtimeHistory.points" />
        </div>
      </el-card>

      <el-card class="runtime-history-card" data-testid="review-diagnostics">
        <template #header>
          <div class="runtime-history-header">
            <div>
              <strong>运行诊断快照</strong>
              <p>{{ diagnosticsSymbolLabel }}</p>
            </div>
            <el-tag size="small" :type="selectedRuntimeDiagnostics?.has_pending_order ? 'warning' : 'success'">
              {{ selectedRuntimeDiagnostics?.has_pending_order ? '存在挂单' : '无挂单' }}
            </el-tag>
          </div>
        </template>

        <el-alert
          v-if="diagnosticsError"
          :title="diagnosticsError"
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
        />

        <template v-else-if="diagnostics && selectedRuntimeDiagnostics">
          <div class="chart-grid diagnostics-summary-grid">
            <div class="section-block">
              <div class="section-title">运行态</div>
              <div class="item-row">
                <span>{{ selectedRuntimeDiagnostics.symbol }} · {{ selectedRuntimeDiagnostics.engine_state }}</span>
                <el-tag :type="selectedRuntimeDiagnostics.is_primary ? 'success' : 'info'" size="small">
                  {{ selectedRuntimeDiagnostics.is_primary ? '主标的' : '观察标的' }}
                </el-tag>
              </div>
              <div class="item-row">
                <span>最近价格 {{ formatCurrency(selectedRuntimeDiagnostics.last_price ?? null, diagnosticsMarket) }}</span>
                <span>触发价 {{ selectedRuntimeDiagnostics.last_trigger_price > 0 ? formatCurrency(selectedRuntimeDiagnostics.last_trigger_price ?? null, diagnosticsMarket) : '-' }}</span>
              </div>
            </div>

            <div class="section-block">
              <div class="section-title">流与线程</div>
              <div class="item-row">
                <span>线程存活</span>
                <strong>{{ diagnostics.thread_alive ? '是' : '否' }}</strong>
              </div>
              <div class="item-row">
                <span>最近推送 {{ formatAgeSeconds(diagnostics.quote_stream.last_push_age_seconds) }}</span>
                <span>最近报价 {{ formatAgeSeconds(diagnostics.quote_stream.last_quote_age_seconds) }}</span>
              </div>
            </div>
          </div>
        </template>
        <p v-else class="empty-note">当前 symbol 暂无运行诊断快照</p>
      </el-card>

      <div class="timeline-section">
        <div v-for="day in reviewData.days" :key="day.date" class="day-card">
          <div class="day-header">
            <strong>{{ day.date }}</strong>
            <el-tag :type="day.daily_pnl >= 0 ? 'success' : 'danger'" effect="plain">{{ signedCurrency(day.daily_pnl) }}</el-tag>
            <el-tag v-if="day.trade_count > 0" type="info" effect="plain">{{ day.trade_count }} 笔交易</el-tag>
            <div class="day-tags">
              <el-tag v-for="tag in day.error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
            </div>
          </div>

          <div class="day-meta">
            <div data-testid="review-day-composition" class="day-meta-row">
              <span>LLM {{ day.llm_interactions.length }}</span>
              <span>订单 {{ day.orders.length }}</span>
              <span>事件 {{ day.events.length }}</span>
              <span>快照 {{ day.snapshots.length }}</span>
              <span>错误 {{ day.error_tags.length }}</span>
            </div>
            <div data-testid="review-day-state" class="day-meta-row">
              <el-tag :type="day.daily_pnl > 0 ? 'success' : day.daily_pnl < 0 ? 'danger' : 'info'" size="small" effect="plain">
                {{ day.daily_pnl > 0 ? '盈利' : day.daily_pnl < 0 ? '亏损' : '打平' }}
              </el-tag>
              <el-tag :type="day.trade_count > 0 ? 'success' : 'info'" size="small" effect="plain">
                {{ day.trade_count > 0 ? '有交易' : '无交易' }}
              </el-tag>
              <el-tag :type="day.error_tags.length > 0 ? 'warning' : 'success'" size="small" effect="plain">
                {{ day.error_tags.length > 0 ? '有错误' : '无错误' }}
              </el-tag>
            </div>
          </div>

          <div class="day-body">
            <div v-if="day.llm_interactions.length > 0" class="section-block">
              <div class="section-title">LLM 建议</div>
              <div v-for="llm in day.llm_interactions" :key="llm.id" class="item-row">
                <el-tag :type="llm.applied ? 'success' : 'info'" size="small">{{ llm.applied ? '已采纳' : '未采纳' }}</el-tag>
                <span>{{ llm.order_action }} {{ llm.symbol }}</span>
                <span class="muted">{{ formatTime(llm.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.orders.length > 0" class="section-block">
              <div class="section-title">订单执行</div>
              <div v-for="order in day.orders" :key="order.id" class="item-row" data-testid="review-order-detail">
                <el-tag :type="order.side === 'BUY' || order.side === 'BUY_TO_COVER' ? 'success' : 'danger'" size="small">{{ order.side }}</el-tag>
                <span>{{ order.broker_order_id }}</span>
                <span>{{ formatOrderIntent(order.quantity, order.price, diagnosticsMarket || 'US') }}</span>
                <span>{{ formatFilledQuantity(order.executed_quantity) }}</span>
                <span>{{ formatExecutedPrice(order.executed_price, diagnosticsMarket || 'US') }}</span>
                <el-tag :type="order.status === ORDER_STATUS.FILLED ? 'success' : 'warning'" size="small">{{ order.status }}</el-tag>
                <span class="muted">成交时间 {{ formatTime(order.filled_at) }}</span>
                <span class="muted">{{ formatTime(order.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.events.length > 0" class="section-block">
              <div class="section-title">交易事件</div>
              <div v-for="event in day.events" :key="event.id" class="item-row" data-testid="review-event-payload">
                <el-tag :type="eventType(event.event_type)" size="small">{{ event.event_type }}</el-tag>
                <span>{{ event.message || '-' }}</span>
                <span>{{ payloadPreview(event.payload_json) }}</span>
                <span class="muted">{{ formatTime(event.created_at) }}</span>
              </div>
            </div>

            <div v-if="day.snapshots.length > 0" class="section-block">
              <div class="section-title">行情快照</div>
              <div v-for="snap in day.snapshots" :key="snap.id" class="item-row" data-testid="review-snapshot-detail">
                <span>价格 {{ formatCurrency(snap.last_price, diagnosticsMarket) }}</span>
                <span>{{ triggerDeltaText(snap.last_price, snap.last_trigger_price, diagnosticsMarket || 'US') }}</span>
                <span>连亏 {{ snap.consecutive_losses }}</span>
                <span v-if="snap.daily_pnl !== 0" :class="snap.daily_pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(snap.daily_pnl) }}</span>
                <span class="muted">{{ formatTime(snap.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <el-empty v-else-if="searched && !loading" description="该时间段无数据，请调整筛选条件" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import PriceChart from '../components/PriceChart.vue'
import PnLChart from '../components/PnLChart.vue'
import { getReview, exportReview } from '../api/review'
import { useStatusHistorySeries } from '../composables/useStatusHistorySeries'
import { useDiagnosticsSnapshot } from '../composables/useDiagnosticsSnapshot'
import type { ReviewResponse } from '../types'
import { ORDER_STATUS } from '../utils/constants'
import { formatCurrency, marketFromSymbol } from '../utils/format'

const form = ref({
  symbol: 'AAPL.US',
  from_date: '',
  to_date: '',
})

const loading = ref(false)
const searched = ref(false)
const reviewData = ref<ReviewResponse | null>(null)
const {
  history: runtimeHistory,
  loading: runtimeHistoryLoading,
  error: runtimeHistoryError,
  load: loadRuntimeHistory,
  reset: resetRuntimeHistory,
} = useStatusHistorySeries()
const {
  diagnostics,
  error: diagnosticsError,
  selectedRuntime: selectedRuntimeDiagnostics,
  load: loadDiagnostics,
  reset: resetDiagnostics,
} = useDiagnosticsSnapshot(computed(() => form.value.symbol))

const pnlClass = computed(() => {
  if (!reviewData.value) return ''
  return reviewData.value.total_pnl >= 0 ? 'positive' : 'negative'
})

const reviewSymbolLabel = computed(() => {
  if (runtimeHistory.value.points.length === 0) return form.value.symbol || '未选择标的'
  return `${form.value.symbol} · ${runtimeHistory.value.points.length} 个样本`
})

const diagnosticsSymbolLabel = computed(() => {
  if (selectedRuntimeDiagnostics.value) {
    return `${selectedRuntimeDiagnostics.value.symbol} · ${selectedRuntimeDiagnostics.value.engine_state}`
  }
  return form.value.symbol || '未选择标的'
})

const diagnosticsMarket = computed(() => marketFromSymbol(form.value.symbol))

async function handleSearch() {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) {
    ElMessage.warning('请填写完整的查询条件')
    return
  }
  loading.value = true
  searched.value = true

  try {
    const [reviewResult, historyResult, diagnosticsResult] = await Promise.allSettled([
      getReview({
        symbol: form.value.symbol,
        from_date: form.value.from_date,
        to_date: form.value.to_date,
      }),
      loadRuntimeHistory({
        symbol: form.value.symbol,
        from: `${form.value.from_date}T00:00:00Z`,
        to: `${form.value.to_date}T23:59:59Z`,
        limit: 200,
      }),
      loadDiagnostics(),
    ])

    if (reviewResult.status === 'fulfilled') {
      reviewData.value = reviewResult.value
    } else {
      reviewData.value = null
      ElMessage.error('复盘数据加载失败')
    }

    if (historyResult.status === 'fulfilled') {
      runtimeHistory.value = historyResult.value
    } else {
      resetRuntimeHistory()
    }

    if (diagnosticsResult.status === 'fulfilled') {
      diagnostics.value = diagnosticsResult.value
    } else {
      resetDiagnostics()
    }
  } catch {
    ElMessage.error('查询复盘数据失败')
    reviewData.value = null
    resetRuntimeHistory()
    resetDiagnostics()
  } finally {
    loading.value = false
  }
}

function handleExport(fmt: 'json' | 'csv') {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) return
  exportReview({
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
    format: fmt,
  })
    .then((res) => {
      const blob: Blob = res instanceof Blob ? res : new Blob([JSON.stringify(res)], { type: fmt === 'json' ? 'application/json' : 'text/csv' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `review_${form.value.symbol.split('.').join('_')}_${form.value.from_date}_${form.value.to_date}.${fmt}`
      document.body.appendChild(link)
      // Revoke the object URL as soon as the browser has had a chance to
      // process the click — listening for `click` is more deterministic than
      // setTimeout and avoids races when the user fires several exports in
      // quick succession (each link was previously holding its URL alive for
      // a full second, which leaks memory and can collide with later URLs).
      const cleanup = () => {
        URL.revokeObjectURL(url)
        link.removeEventListener('click', cleanup)
        if (link.parentNode) link.parentNode.removeChild(link)
      }
      link.addEventListener('click', cleanup)
      link.click()
      // Fallback: if the click event has already fired (synchronous dispatch
      // path), still schedule a short revoke to ensure the URL is freed.
      setTimeout(cleanup, 1000)
      ElMessage.success(`导出 ${fmt.toUpperCase()} 成功`)
    })
    .catch(() => {
      ElMessage.error('导出失败')
    })
}

function eventType(eventTypeValue: string): string {
  switch (eventTypeValue) {
    case 'ORDER_FILLED': return 'success'
    case 'ORDER_CANCELLED': return 'info'
    case 'ORDER_REJECTED': return 'danger'
    case 'ORDER_SKIPPED': return 'warning'
    case 'RISK_PAUSED': return 'danger'
    case 'LLM_ANALYSIS': return 'primary'
    default: return ''
  }
}

function signedCurrency(value: number): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '-'
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatAgeSeconds(value: number | null | undefined): string {
  if (value == null) return '-'
  return `${value.toFixed(1)}s`
}

function formatFilledQuantity(value: number | null): string {
  return value != null ? `成交 ${value.toFixed(0)}` : '成交 -'
}

function formatOrderIntent(quantity: number, price: number, market: string): string {
  return `委托 ${quantity.toFixed(0)} 股 @ ${formatCurrency(price, market)}`
}

function formatExecutedPrice(value: number | null, market: string): string {
  return value != null ? `成交价 ${formatCurrency(value, market)}` : '成交价 -'
}

function payloadPreview(payloadJson: string | null | undefined): string {
  if (!payloadJson?.trim()) return 'payload -'
  try {
    const parsed: unknown = JSON.parse(payloadJson)
    if (parsed == null) return 'payload -'
    if (typeof parsed === 'string') return `payload ${truncateText(parsed)}`
    if (typeof parsed === 'number' || typeof parsed === 'boolean') return `payload ${String(parsed)}`
    if (Array.isArray(parsed)) return `payload [${parsed.slice(0, 3).map((item) => stringifyPreviewValue(item)).join(', ')}${parsed.length > 3 ? ', …' : ''}]`
    if (typeof parsed === 'object') {
      const entries = Object.entries(parsed as Record<string, unknown>).slice(0, 3)
      const preview = entries.map(([key, value]) => `${key}: ${stringifyPreviewValue(value)}`).join(', ')
      return preview ? `payload {${preview}${Object.keys(parsed as Record<string, unknown>).length > 3 ? ', …' : ''}}` : 'payload -'
    }
    return 'payload -'
  } catch {
    return 'payload -'
  }
}

function stringifyPreviewValue(value: unknown): string {
  if (value == null) return 'null'
  if (typeof value === 'string') return truncateText(value)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return Array.isArray(value) ? '[…]' : '{…}'
}

function truncateText(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

function triggerDeltaText(lastPrice: number, lastTriggerPrice: number, market: string): string {
  if (lastTriggerPrice <= 0) return 'Δ触发 -'
  const delta = lastPrice - lastTriggerPrice
  const sign = delta > 0 ? '+' : delta < 0 ? '-' : ''
  return `触发价 ${formatCurrency(lastTriggerPrice, market)} · Δ触发 ${sign}${Math.abs(delta).toFixed(2)}`
}
</script>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.review-header h3 {
  margin: 0;
}

.review-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.filter-card {
  margin-bottom: 0;
}

.summary-row {
  margin-bottom: 8px;
}

.summary-card {
  text-align: center;
}

.summary-value {
  color: #172033;
  font-size: 24px;
  font-weight: 800;
  line-height: 1.2;
}

.summary-value.positive {
  color: #14884f;
}

.summary-value.negative {
  color: #c43838;
}

.summary-value.error-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 400;
}

.no-tag {
  color: #909399;
}

.summary-label {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.runtime-history-card {
  margin-bottom: 8px;
}

.runtime-history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.runtime-history-header p {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
}

.chart-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}

.diagnostics-summary-grid .section-block {
  height: 100%;
}

.timeline-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day-card {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  padding: 14px;
  background: #f7f9fc;
}

.day-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e1e7f0;
}

.day-header strong {
  color: #172033;
  font-size: 15px;
}

.day-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-left: auto;
}

.day-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day-meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}

.day-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  color: #4b5563;
  font-size: 12px;
}

.section-block {
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}

.section-title {
  margin-bottom: 6px;
  color: #4b5563;
  font-size: 12px;
  font-weight: 700;
}

.item-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid #f1f5f9;
}

.item-row:last-child {
  border-bottom: none;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 520px) {
  .review-page {
    padding: 8px;
    gap: 12px;
  }

  .day-header {
    flex-direction: column;
    align-items: flex-start;
  }

  .day-tags {
    margin-left: 0;
  }
}
</style>
