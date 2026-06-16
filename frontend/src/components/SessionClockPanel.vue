<template>
  <section class="session-panel" data-testid="session-panel">
    <div class="panel-heading"><h4>交易时段</h4></div>
    <div v-if="data" class="session-body">
      <el-tag :type="statusType" effect="dark" size="large" data-testid="session-status">{{ statusLabel }}</el-tag>
      <div class="session-time">{{ data.local_time }} <small>{{ data.market }}</small></div>
      <div class="session-next">下次开盘：{{ formatTime(data.next_open) }}</div>
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

onMounted(() => {
  load()
  timer = window.setInterval(load, 60000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
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

.muted {
  color: #909399;
  font-size: 13px;
}
</style>
