<template>
  <section class="equity-panel" data-testid="equity-curve-panel">
    <div class="panel-heading">
      <h4>权益曲线（累计已实现盈亏 · 全标的）</h4>
      <div class="heading-controls">
        <el-select v-model="days" size="small" style="width: 110px" @change="load">
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
          <el-option :value="180" label="近 180 天" />
          <el-option :value="365" label="近 1 年" />
        </el-select>
        <el-button size="small" plain :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <StatisticsQualityAlert v-if="result" :quality="result.statistics_quality" />

    <div v-if="result" class="equity-summary">
      <div class="equity-stat">
        <span>累计已实现（净）</span>
        <strong :class="pnlClass(result.total_realized_pnl)">{{ signed(result.total_realized_pnl) }}</strong>
      </div>
      <div class="equity-stat">
        <span>最大回撤</span>
        <strong class="pnl-negative">-{{ abs(result.max_drawdown) }}</strong>
      </div>
      <div class="equity-stat">
        <span>样本天数</span>
        <strong>{{ result.points.length }}</strong>
      </div>
    </div>

    <div v-if="derivedStats" class="equity-derived" data-testid="equity-derived">
      <el-tag size="small" type="success">峰值 +{{ derivedStats.peak.toFixed(2) }}</el-tag>
      <el-tag size="small" type="danger">谷值 {{ derivedStats.trough.toFixed(2) }}</el-tag>
      <el-tag size="small" :type="derivedStats.periodReturn >= 0 ? 'success' : 'danger'">
        区间回报 {{ signed(derivedStats.periodReturn) }}
      </el-tag>
      <el-tag v-if="derivedStats.bestDayDelta !== null" size="small" type="success">
        最佳日 +{{ derivedStats.bestDayDelta.toFixed(2) }}
      </el-tag>
      <el-tag v-if="derivedStats.worstDayDelta !== null" size="small" type="danger">
        最差日 {{ derivedStats.worstDayDelta.toFixed(2) }}
      </el-tag>
    </div>

    <svg
      v-if="result && result.points.length > 1"
      class="equity-chart"
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="none"
      data-testid="equity-curve-chart"
    >
      <line :x1="0" :y1="zeroY" :x2="W" :y2="zeroY" class="zero" />
      <polygon :points="downArea" class="down-area" />
      <polyline :points="curvePath" class="curve" />
    </svg>
    <p v-else-if="result && result.points.length <= 1" class="empty-note">已实现成交不足，无法绘制曲线。</p>
    <p v-else class="empty-note">暂无已实现成交。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getEquityCurve } from '../api'
import type { EquityCurveResponse } from '../types'
import { resolveErrorMessage } from '../utils/error'
import StatisticsQualityAlert from './StatisticsQualityAlert.vue'

const result = ref<EquityCurveResponse | null>(null)
const loading = ref(false)
const error = ref('')
const days = ref(90)
const W = 480
const H = 120

async function load() {
  loading.value = true
  error.value = ''
  try {
    result.value = await getEquityCurve({ days: days.value })
  } catch (e) {
    error.value = resolveErrorMessage(e, '加载权益曲线失败')
  } finally {
    loading.value = false
  }
}

const cumValues = computed(() => (result.value?.points ?? []).map(p => p.cumulative_pnl))

/** Derived peak/trough/period-return/best-worst-day from loaded points. */
const derivedStats = computed(() => {
  const pts = result.value?.points ?? []
  if (pts.length === 0) return null
  const vals = pts.map((p) => p.cumulative_pnl)
  const peak = Math.max(...vals)
  const trough = Math.min(...vals)
  const periodReturn = vals[vals.length - 1] - vals[0]
  // Per-day delta = current cumulative - previous cumulative.
  let bestDayDelta: number | null = null
  let worstDayDelta: number | null = null
  for (let i = 1; i < pts.length; i++) {
    const delta = pts[i].cumulative_pnl - pts[i - 1].cumulative_pnl
    if (bestDayDelta === null || delta > bestDayDelta) bestDayDelta = delta
    if (worstDayDelta === null || delta < worstDayDelta) worstDayDelta = delta
  }
  return { peak, trough, periodReturn, bestDayDelta, worstDayDelta }
})

// Scale across cumulative values AND 0 so the zero baseline is always visible.
const scale = computed(() => {
  const vs = cumValues.value
  if (!vs.length) return { min: 0, max: 0, range: 1 }
  const mn = Math.min(...vs, 0)
  const mx = Math.max(...vs, 0)
  const range = mx - mn || 1
  return { min: mn, max: mx, range }
})

const zeroY = computed(() => H - ((0 - scale.value.min) / scale.value.range) * H)

function yFor(v: number): number {
  return H - ((v - scale.value.min) / scale.value.range) * H
}

const curvePath = computed(() => {
  const vs = cumValues.value
  if (vs.length < 2) return ''
  const stepX = W / (vs.length - 1)
  return vs.map((v, i) => `${(i * stepX).toFixed(1)},${yFor(v).toFixed(1)}`).join(' ')
})

// Drawdown area: shade from the curve down to the peak line (worst excursion).
// Approximated by plotting cumulative vs running peak; shaded region height
// equals the drawdown at each point.
const downArea = computed(() => {
  const pts = result.value?.points ?? []
  if (pts.length < 2) return ''
  const stepX = W / (pts.length - 1)
  let peak = Number.NEGATIVE_INFINITY
  const top: string[] = []
  pts.forEach((p, i) => {
    peak = Math.max(peak, p.cumulative_pnl)
    top.push(`${(i * stepX).toFixed(1)},${yFor(peak).toFixed(1)}`)
  })
  const bottom = pts
    .map((p, i) => `${(i * stepX).toFixed(1)},${yFor(p.cumulative_pnl).toFixed(1)}`)
    .reverse()
  return [...top, ...bottom].join(' ')
})

function signed(v: number): string {
  const a = Math.abs(v).toFixed(2)
  return v > 0 ? `+$${a}` : v < 0 ? `-$${a}` : `$${a}`
}

function abs(v: number): string {
  return Math.abs(v).toFixed(2)
}

function pnlClass(v: number): string {
  return v > 0 ? 'pnl-positive' : v < 0 ? 'pnl-negative' : ''
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.equity-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.panel-heading h4 {
  margin: 0;
  font-size: 15px;
  color: var(--chart-heading);
}

.heading-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.equity-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}

.equity-derived {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.equity-stat span {
  display: block;
  color: var(--chart-muted);
  font-size: 12px;
}

.equity-stat strong {
  font-size: 16px;
  color: var(--chart-heading);
}

.equity-chart {
  width: 100%;
  height: 120px;
}

.equity-chart .zero {
  stroke: var(--chart-zero);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.equity-chart .curve {
  fill: none;
  stroke: #2563eb;
  stroke-width: 2;
}

.equity-chart .down-area {
  fill: rgba(220, 38, 38, 0.12);
  stroke: none;
}

.empty-note {
  color: var(--chart-muted);
  font-size: 13px;
  margin: 0;
}

.pnl-positive {
  color: #16a34a;
  font-weight: 600;
}

.pnl-negative {
  color: #dc2626;
  font-weight: 600;
}

@media (max-width: 520px) {
  .panel-heading {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
