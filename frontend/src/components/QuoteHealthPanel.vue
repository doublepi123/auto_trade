<template>
  <div class="quote-health" data-testid="dashboard-quote-health">
    <div class="quote-health-header">
      <h5>行情流健康</h5>
      <div class="quote-health-header-meta">
        <el-tag
          v-if="result"
          size="small"
          :type="statusTagType"
          effect="plain"
          data-testid="quote-health-status"
        >{{ statusLabel }}</el-tag>
        <span v-if="result" class="quote-health-checked" data-testid="quote-health-checked-at">
          快照 {{ snapshotAsOfLabel }}
        </span>
      </div>
    </div>

    <div v-if="loading && !result" class="quote-health-state" data-testid="quote-health-loading">加载中…</div>
    <div v-else-if="error" class="quote-health-state quote-health-error" data-testid="quote-health-error">{{ error }}</div>

    <!-- HTTP 503: schema-validated unavailable body. The runtime has not
         created a tracker yet — this is NOT a known-disconnected stream, so
         the zeroed counters in the body are not presented as real metrics. -->
    <div
      v-else-if="result && result.http_status === 503"
      class="quote-health-state quote-health-unavailable"
      data-testid="quote-health-unavailable"
    >
      运行器尚未就绪，暂无行情流快照（HTTP 503）；当前不是断线证据，刷新页面数据不会发起订阅或重连。
    </div>

    <template v-else-if="health">
      <div class="quote-health-lead">
        <strong class="quote-health-symbol" data-testid="quote-health-symbol">{{ health.symbol || '—' }}</strong>
        <el-tag
          size="small"
          :type="health.quotes_subscribed ? 'success' : 'info'"
          effect="plain"
          data-testid="quote-health-subscription"
        >{{ health.quotes_subscribed ? '已订阅' : '未订阅' }}</el-tag>
        <span v-if="health.status === 'waiting'" class="quote-health-note" data-testid="quote-health-waiting-note">
          已订阅，等待当前窗口首条推送报价
        </span>
      </div>

      <div class="quote-health-grid">
        <div class="quote-health-item quote-health-item-primary">
          <span>最近报价</span>
          <strong data-testid="quote-health-age">{{ quoteAgeLabel }}</strong>
          <small data-testid="quote-health-last-timestamp">{{ lastTimestampLabel }}</small>
        </div>
        <div class="quote-health-item">
          <span>已收报价</span>
          <strong data-testid="quote-health-received">{{ health.quotes_received }}</strong>
          <small>当前订阅窗口</small>
        </div>
        <div class="quote-health-item">
          <span>最大报价间隔</span>
          <strong data-testid="quote-health-max-gap">{{ maxGapLabel }}</strong>
          <small>当前订阅窗口</small>
        </div>
        <div class="quote-health-item">
          <span>断线次数</span>
          <strong data-testid="quote-health-disconnects">{{ health.disconnect_count }}</strong>
          <small>进程累计</small>
        </div>
        <div class="quote-health-item">
          <span>重订阅次数</span>
          <strong data-testid="quote-health-resubscribes">{{ health.resubscribe_count }}</strong>
          <small>进程累计</small>
        </div>
        <div class="quote-health-item">
          <span>断线重试</span>
          <strong data-testid="quote-health-retries">{{ health.disconnect_retry_count }}</strong>
          <small>进程累计</small>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { QuoteStreamHealthResult } from '../api'
import { relativeAgeLabel } from '../utils/time'

/**
 * Read-only quote-stream health surface inside the Dashboard diagnostics
 * panel. Augments the simple push-age blocks above it with the dedicated
 * tracker snapshot: symbol, subscription state, server verdict, window
 * metrics (quotes received / max gap) and process-lifetime counters. The
 * reported quote age advances locally between fetches via the shared 1s
 * ticker — the server verdict (`status`) itself is never reclassified
 * client-side. No subscribe/reconnect/reset controls exist here by design.
 */
const props = withDefaults(
  defineProps<{
    result: QuoteStreamHealthResult | null
    loading?: boolean
    error?: string
    /** 1s ticker from the parent so the age/freshness labels advance live. */
    nowTick?: number
    /** Local receive time (ms) of the current snapshot; base for age advance. */
    fetchedAt?: number | null
  }>(),
  {
    loading: false,
    error: '',
    nowTick: 0,
    fetchedAt: null,
  },
)

const health = computed(() => props.result?.health ?? null)

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康',
  stale: '过期',
  waiting: '等待首条报价',
  unavailable: '未订阅',
}

const statusLabel = computed(() => {
  if (!props.result) return ''
  if (props.result.http_status === 503) return '不可用'
  const status = health.value?.status ?? ''
  if (status === 'unavailable') return '未订阅'
  return STATUS_LABELS[status] ?? status
})

const statusTagType = computed(() => {
  if (!props.result) return 'info'
  if (props.result.http_status === 503) return 'warning'
  switch (health.value?.status) {
    case 'healthy': return 'success'
    case 'stale': return 'warning'
    case 'waiting': return 'info'
    default: return 'info'
  }
})

/** Quote age advanced from the server snapshot by the shared 1s ticker. */
const advancedAgeSeconds = computed(() => {
  const base = health.value?.last_quote_age_seconds
  if (base === null || base === undefined) return null
  if (!props.nowTick || !props.fetchedAt) return base
  return base + Math.max(0, (props.nowTick - props.fetchedAt) / 1000)
})

const quoteAgeLabel = computed(() => {
  const age = advancedAgeSeconds.value
  if (age === null) return '—'
  if (age < 60) return `${age.toFixed(1)}s`
  const minutes = Math.floor(age / 60)
  const seconds = Math.round(age % 60)
  return `${minutes}m ${seconds}s`
})

/** The raw source timestamp can be epoch seconds/millis or ISO 8601. */
const lastTimestampLabel = computed(() => {
  const raw = health.value?.last_quote_timestamp
  if (!raw) return '暂无报价时间'
  const parsed = parseSourceTimestamp(raw)
  if (!parsed) return `原始时间 ${raw}`
  return `时间 ${parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
})

function parseSourceTimestamp(raw: string): Date | null {
  const value = raw.trim()
  if (!value) return null
  if (/^\d+(\.\d+)?$/.test(value)) {
    let numeric = Number(value)
    if (!Number.isFinite(numeric)) return null
    if (numeric > 10_000_000_000) numeric /= 1000
    const date = new Date(numeric * 1000)
    return Number.isNaN(date.getTime()) ? null : date
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

const maxGapLabel = computed(() => {
  const gap = health.value?.max_gap_seconds
  if (gap === null || gap === undefined) return '—'
  return `${gap.toFixed(1)}s`
})

const snapshotAsOfLabel = computed(() => {
  const at = health.value?.as_of
  if (!at) return ''
  const time = new Date(at)
  if (Number.isNaN(time.getTime())) return at
  const clock = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  if (!props.nowTick) return clock
  const age = Math.max(0, Math.floor((props.nowTick - time.getTime()) / 1000))
  return `${clock} · ${relativeAgeLabel(age)}`
})
</script>

<style scoped>
.quote-health {
  margin-bottom: 14px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  background: #fbfcfe;
}

.quote-health-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.quote-health-header h5 {
  margin: 0;
  color: #334155;
  font-size: 13px;
  font-weight: 600;
}

.quote-health-header-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.quote-health-checked {
  color: #909399;
  font-size: 12px;
}

.quote-health-state {
  padding: 8px 0;
  color: #909399;
  font-size: 12px;
}

.quote-health-error {
  color: var(--el-color-danger);
}

.quote-health-unavailable {
  color: #b45309;
}

.quote-health-lead {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.quote-health-symbol {
  color: #172033;
  font-size: 14px;
}

.quote-health-note {
  color: #6b7280;
  font-size: 12px;
}

.quote-health-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
}

.quote-health-item {
  border-radius: 6px;
  padding: 8px 10px;
  background: #f4f6fa;
  min-width: 0;
}

.quote-health-item-primary {
  background: #eef4fd;
}

.quote-health-item span,
.quote-health-item small {
  display: block;
  color: #6b7280;
  font-size: 11px;
}

.quote-health-item strong {
  display: block;
  margin-top: 2px;
  color: #172033;
  font-size: 16px;
  font-weight: 700;
  word-break: break-all;
}

.quote-health-item small {
  margin-top: 2px;
}
</style>
