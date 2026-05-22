<template>
  <section class="chart-panel" data-testid="pnl-chart">
    <div class="chart-heading">
      <div>
        <h4>盈亏曲线</h4>
        <span>{{ points.length }} 个样本</span>
      </div>
      <strong :class="latestPnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(latestPnl) }}</strong>
    </div>
    <svg class="chart-svg" viewBox="0 0 360 170" role="img" aria-label="盈亏曲线图">
      <line x1="36" y1="18" x2="36" y2="132" class="axis" />
      <line x1="36" :y1="zeroY" x2="340" :y2="zeroY" class="zero-line" />
      <path v-if="areaPath" :d="areaPath" class="pnl-area" :class="latestPnl >= 0 ? 'area-positive' : 'area-negative'" />
      <path v-if="linePath" :d="linePath" class="pnl-line" :class="latestPnl >= 0 ? 'line-positive' : 'line-negative'" />
      <text x="8" y="24" class="axis-label">{{ signedCurrency(maxPnl) }}</text>
      <text x="8" y="136" class="axis-label">{{ signedCurrency(minPnl) }}</text>
    </svg>
    <div class="chart-legend">
      <span><i class="legend-line pnl" />实时盈亏</span>
      <span><i class="legend-line zero" />零轴</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StatusHistoryPoint } from '../types'

const props = defineProps<{
  points: StatusHistoryPoint[]
}>()

const chart = {
  left: 36,
  right: 340,
  top: 18,
  bottom: 132,
}

const pnlValues = computed(() => props.points.map((point) => point.daily_pnl))
const latestPnl = computed(() => pnlValues.value.length > 0 ? pnlValues.value[pnlValues.value.length - 1] : 0)
const minPnl = computed(() => {
  if (pnlValues.value.length === 0) return 0
  const min = Math.min(...pnlValues.value, 0)
  const max = Math.max(...pnlValues.value, 0)
  return min - Math.max((max - min) * 0.1, 1)
})
const maxPnl = computed(() => {
  if (pnlValues.value.length === 0) return 1
  const min = Math.min(...pnlValues.value, 0)
  const max = Math.max(...pnlValues.value, 0)
  return max + Math.max((max - min) * 0.1, 1)
})

function xForIndex(index: number, count: number) {
  if (count <= 1) return chart.left
  return chart.left + (index / (count - 1)) * (chart.right - chart.left)
}

function yForPnl(value: number) {
  const range = maxPnl.value - minPnl.value || 1
  return chart.bottom - ((value - minPnl.value) / range) * (chart.bottom - chart.top)
}

const zeroY = computed(() => yForPnl(0))

const linePath = computed(() => {
  if (pnlValues.value.length === 0) return ''
  return pnlValues.value
    .map((value, index) => `${index === 0 ? 'M' : 'L'} ${xForIndex(index, pnlValues.value.length).toFixed(2)} ${yForPnl(value).toFixed(2)}`)
    .join(' ')
})

const areaPath = computed(() => {
  if (!linePath.value || pnlValues.value.length === 0) return ''
  const lastX = xForIndex(pnlValues.value.length - 1, pnlValues.value.length)
  const firstX = xForIndex(0, pnlValues.value.length)
  return `${linePath.value} L ${lastX.toFixed(2)} ${zeroY.value.toFixed(2)} L ${firstX.toFixed(2)} ${zeroY.value.toFixed(2)} Z`
})

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}
</script>

<style scoped>
.chart-panel {
  min-height: 240px;
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  padding: 14px;
  background: #fff;
}

.chart-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.chart-heading h4 {
  margin: 0;
  color: #172033;
  font-size: 15px;
}

.chart-heading span {
  display: block;
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.chart-heading strong {
  font-size: 20px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

.chart-svg {
  width: 100%;
  height: 170px;
  margin-top: 8px;
}

.axis,
.zero-line {
  stroke: #d8e0ec;
  stroke-width: 1;
}

.zero-line {
  stroke-dasharray: 4 4;
}

.axis-label {
  fill: #7a8595;
  font-size: 10px;
}

.pnl-line {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2.4;
}

.line-positive {
  stroke: #14884f;
}

.line-negative {
  stroke: #c43838;
}

.pnl-area {
  opacity: 0.16;
}

.area-positive {
  fill: #14884f;
}

.area-negative {
  fill: #c43838;
}

.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  color: #6b7280;
  font-size: 12px;
}

.chart-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.legend-line {
  display: inline-block;
  width: 18px;
  height: 2px;
  border-radius: 999px;
}

.legend-line.pnl {
  background: #14884f;
}

.legend-line.zero {
  background: #d8e0ec;
}
</style>
