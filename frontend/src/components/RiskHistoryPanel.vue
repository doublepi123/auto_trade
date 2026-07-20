<template>
  <section class="risk-history-panel" data-testid="risk-history-panel">
    <div class="panel-heading">
      <h4>风险历史（日内盈亏趋势）</h4>
      <el-button size="small" plain :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

    <div v-if="result && result.latest" class="risk-summary">
      <div class="risk-stat">
        <span>最新日内盈亏</span>
        <strong :class="pnlClass(result.latest.daily_pnl)">{{ signed(result.latest.daily_pnl) }}</strong>
      </div>
      <div class="risk-stat">
        <span>连续亏损</span>
        <strong>{{ result.latest.consecutive_losses }}</strong>
      </div>
      <div class="risk-stat">
        <span>状态</span>
        <strong>
          {{ result.latest.engine_state }}
          <el-tag v-if="result.latest.paused" size="small" type="warning" style="margin-left: 6px">已暂停</el-tag>
          <el-tag v-if="result.latest.kill_switch" size="small" type="danger" style="margin-left: 6px">熔断</el-tag>
        </strong>
      </div>
    </div>

    <svg
      v-if="result && result.points.length > 1"
      class="sparkline"
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="none"
      data-testid="risk-sparkline"
    >
      <line :x1="0" :y1="zeroY" :x2="W" :y2="zeroY" class="zero" />
      <polyline :points="sparkPath" class="line" />
    </svg>
    <p v-else-if="result && result.points.length <= 1" class="empty-note">快照不足，无法绘制趋势。</p>
    <p v-else class="empty-note">暂无风险快照。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getRiskHistory } from '../api'
import type { RiskHistoryResponse } from '../types'
import { resolveErrorMessage } from '../utils/error'

const result = ref<RiskHistoryResponse | null>(null)
const loading = ref(false)
const error = ref('')
const W = 320
const H = 80

async function load() {
  loading.value = true
  error.value = ''
  try {
    result.value = await getRiskHistory({ limit: 100 })
  } catch (e) {
    error.value = resolveErrorMessage(e, '加载风险历史失败')
  } finally {
    loading.value = false
  }
}

const values = computed(() => (result.value?.points ?? []).map(p => p.daily_pnl))

const zeroY = computed(() => {
  const vs = values.value
  if (!vs.length) return H / 2
  const mn = Math.min(...vs, 0)
  const mx = Math.max(...vs, 0)
  const range = mx - mn || 1
  return H - ((0 - mn) / range) * H
})

const sparkPath = computed(() => {
  const vs = values.value
  if (vs.length < 2) return ''
  const mn = Math.min(...vs, 0)
  const mx = Math.max(...vs, 0)
  const range = mx - mn || 1
  const stepX = W / (vs.length - 1)
  return vs
    .map((v, i) => `${(i * stepX).toFixed(1)},${(H - ((v - mn) / range) * H).toFixed(1)}`)
    .join(' ')
})

function signed(v: number): string {
  const a = Math.abs(v).toFixed(2)
  return v > 0 ? `+$${a}` : v < 0 ? `-$${a}` : `$${a}`
}

function pnlClass(v: number): string {
  return v > 0 ? 'pnl-positive' : v < 0 ? 'pnl-negative' : ''
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.risk-history-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-heading h4 {
  margin: 0;
  font-size: 15px;
  color: var(--chart-heading);
}

.risk-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}

.risk-stat span {
  display: block;
  color: var(--chart-muted);
  font-size: 12px;
}

.risk-stat strong {
  font-size: 16px;
  color: var(--chart-heading);
}

.sparkline {
  width: 100%;
  height: 80px;
}

.sparkline .zero {
  stroke: var(--chart-zero);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.sparkline .line {
  fill: none;
  stroke: #3b82f6;
  stroke-width: 2;
}

.empty-note {
  margin: 12px 0;
  color: var(--chart-muted);
  text-align: center;
}

.pnl-positive {
  color: #14884f;
}

.pnl-negative {
  color: #c43838;
}
</style>
