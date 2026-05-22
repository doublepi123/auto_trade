<template>
  <section class="chart-panel" data-testid="price-chart">
    <div class="chart-heading">
      <div>
        <h4>价格走势</h4>
        <span>{{ points.length }} 个样本</span>
      </div>
      <strong>${{ formatNumber(latestPrice) }}</strong>
    </div>
    <svg class="chart-svg" viewBox="0 0 360 170" role="img" aria-label="价格走势图">
      <line x1="36" y1="18" x2="36" y2="132" class="axis" />
      <line x1="36" y1="132" x2="340" y2="132" class="axis" />
      <line
        v-if="buyLineY !== null"
        x1="36"
        :y1="buyLineY"
        x2="340"
        :y2="buyLineY"
        class="threshold threshold-buy"
      />
      <line
        v-if="sellLineY !== null"
        x1="36"
        :y1="sellLineY"
        x2="340"
        :y2="sellLineY"
        class="threshold threshold-sell"
      />
      <path v-if="pricePath" :d="pricePath" class="price-line" />
      <g v-for="marker in markerPositions" :key="marker.id" data-testid="trade-signal-marker">
        <circle :cx="marker.x" :cy="marker.y" r="4.5" :class="marker.kind" />
        <text :x="marker.x" :y="marker.y - 8" text-anchor="middle" class="marker-label">{{ marker.label }}</text>
      </g>
      <text x="8" y="24" class="axis-label">{{ formatNumber(maxPrice) }}</text>
      <text x="8" y="136" class="axis-label">{{ formatNumber(minPrice) }}</text>
    </svg>
    <div class="chart-legend">
      <span><i class="legend-line price" />价格</span>
      <span><i class="legend-dot buy" />交易信号</span>
      <span><i class="legend-line buy-line" />买入线</span>
      <span><i class="legend-line sell-line" />卖出线</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StatusHistoryPoint, TradeSignalMarker } from '../types'

const props = defineProps<{
  points: StatusHistoryPoint[]
  markers: TradeSignalMarker[]
  buyLow: number
  sellHigh: number
}>()

const chart = {
  left: 36,
  right: 340,
  top: 18,
  bottom: 132,
}

const prices = computed(() => props.points.map((point) => point.last_price).filter((price) => price > 0))
const latestPrice = computed(() => prices.value.length > 0 ? prices.value[prices.value.length - 1] : 0)
const minPrice = computed(() => {
  const candidates = [...prices.value, props.buyLow, props.sellHigh].filter((price) => price > 0)
  if (candidates.length === 0) return 0
  const min = Math.min(...candidates)
  const max = Math.max(...candidates)
  return min - Math.max((max - min) * 0.08, 0.01)
})
const maxPrice = computed(() => {
  const candidates = [...prices.value, props.buyLow, props.sellHigh].filter((price) => price > 0)
  if (candidates.length === 0) return 1
  const min = Math.min(...candidates)
  const max = Math.max(...candidates)
  return max + Math.max((max - min) * 0.08, 0.01)
})

function xForIndex(index: number, count: number) {
  if (count <= 1) return chart.left
  return chart.left + (index / (count - 1)) * (chart.right - chart.left)
}

function yForPrice(price: number) {
  const range = maxPrice.value - minPrice.value || 1
  return chart.bottom - ((price - minPrice.value) / range) * (chart.bottom - chart.top)
}

const pricePath = computed(() => {
  if (prices.value.length === 0) return ''
  return prices.value
    .map((price, index) => `${index === 0 ? 'M' : 'L'} ${xForIndex(index, prices.value.length).toFixed(2)} ${yForPrice(price).toFixed(2)}`)
    .join(' ')
})

const buyLineY = computed(() => props.buyLow > 0 ? yForPrice(props.buyLow) : null)
const sellLineY = computed(() => props.sellHigh > 0 ? yForPrice(props.sellHigh) : null)

const markerPositions = computed(() => {
  if (props.points.length === 0) return []
  const firstTs = Date.parse(props.points[0].timestamp)
  const lastTs = Date.parse(props.points[props.points.length - 1].timestamp)
  const span = Math.max(lastTs - firstTs, 1)
  return props.markers.map((marker) => {
    const markerTs = Date.parse(marker.timestamp)
    const clampedRatio = Math.min(1, Math.max(0, (markerTs - firstTs) / span))
    const x = chart.left + clampedRatio * (chart.right - chart.left)
    return {
      id: marker.broker_order_id,
      x,
      y: yForPrice(marker.price),
      kind: marker.side === 'BUY' || marker.side === 'BUY_TO_COVER' ? 'marker-buy' : 'marker-sell',
      label: marker.side === 'BUY' || marker.side === 'BUY_TO_COVER' ? 'B' : 'S',
    }
  })
})

function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
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
  color: #172033;
  font-size: 20px;
}

.chart-svg {
  width: 100%;
  height: 170px;
  margin-top: 8px;
}

.axis {
  stroke: #d8e0ec;
  stroke-width: 1;
}

.axis-label,
.marker-label {
  fill: #7a8595;
  font-size: 10px;
}

.price-line {
  fill: none;
  stroke: #2563eb;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2.4;
}

.threshold {
  stroke-dasharray: 4 4;
  stroke-width: 1.2;
}

.threshold-buy {
  stroke: #14884f;
}

.threshold-sell {
  stroke: #c43838;
}

.marker-buy {
  fill: #14884f;
  stroke: #fff;
  stroke-width: 2;
}

.marker-sell {
  fill: #c43838;
  stroke: #fff;
  stroke-width: 2;
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

.legend-line.price {
  background: #2563eb;
}

.legend-line.buy-line {
  background: #14884f;
}

.legend-line.sell-line {
  background: #c43838;
}

.legend-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
}

.legend-dot.buy {
  background: #14884f;
}
</style>
