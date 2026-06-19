<template>
  <section class="session-panel" data-testid="session-panel">
    <div class="panel-heading"><h4>交易时段</h4></div>
    <div v-if="data" class="session-body">
      <el-tag :type="statusType" effect="dark" size="large" data-testid="session-status">{{ statusLabel }}</el-tag>
      <div class="session-time">{{ data.local_time }} <small>{{ data.market }}</small></div>
      <div class="session-next">下次开盘：{{ formatTime(data.next_open) }}</div>
      <div v-if="countdownText" class="session-countdown" data-testid="session-countdown">距开盘 {{ countdownText }}</div>
    </div>
    <p v-else-if="!error" class="muted">加载中…</p>
    <p v-else class="muted">交易时段不可用</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getMarketSession } from '../api'
import type { MarketSessionStatus } from '../types'

const props = defineProps<{ symbol: string }>()
const data = ref<MarketSessionStatus | null>(null)
const error = ref('')
let timer: number | undefined
// Live 1s ticking "now" so the next-open countdown updates without re-fetching
// the session endpoint (which only polls every 60s).
const now = ref(Date.now())
let countdownTimer: number | undefined

async function load() {
  try {
    data.value = await getMarketSession(props.symbol || '')
    error.value = ''
  } catch {
    error.value = 'failed'
  }
}

const statusLabel = computed(() => {
  switch (data.value?.status) {
    case 'rth': return '交易中 (RTH)'
    case 'pre': return '盘前'
    case 'post': return '盘后'
    case 'lunch': return '午休'
    case 'closed': return '休市'
    default: return '-'
  }
})

const statusType = computed(() => {
  switch (data.value?.status) {
    case 'rth': return 'success'
    case 'pre': case 'post': case 'lunch': return 'warning'
    default: return 'info'
  }
})

function formatTime(v: string): string {
  return new Date(v).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

/** HH:MM:SS until next_open, or '' when market is already open / no next_open /
 * next_open already passed. Derived purely from the loaded `data` + ticking now. */
const countdownText = computed(() => {
  const d = data.value
  if (!d || !d.next_open || d.status === 'rth') return ''
  const ms = new Date(d.next_open).getTime() - now.value
  if (!Number.isFinite(ms) || ms <= 0) return ''
  const totalSec = Math.floor(ms / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(h)}:${pad(m)}:${pad(s)}`
})

onMounted(() => {
  load()
  timer = window.setInterval(load, 60000)
  countdownTimer = window.setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (countdownTimer) clearInterval(countdownTimer)
})
watch(() => props.symbol, load)
</script>

<style scoped>
.session-panel {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
  padding: 12px 14px;
}

.panel-heading {
  margin-bottom: 8px;
}

.panel-heading h4 {
  margin: 0;
  font-size: 15px;
  color: #172033;
}

.session-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.session-time {
  color: #172033;
  font-size: 14px;
}

.session-time small {
  color: #909399;
  font-size: 12px;
}

.session-next {
  color: #6b7280;
  font-size: 12px;
}

.session-countdown {
  color: var(--el-color-primary);
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.muted {
  color: #909399;
  font-size: 13px;
}
</style>
