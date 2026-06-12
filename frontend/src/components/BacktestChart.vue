<template>
  <section class="backtest-chart" data-testid="backtest-chart">
    <div class="chart-block" data-testid="backtest-price-chart">
      <div class="chart-heading">
        <div>
          <h4>价格与信号</h4>
          <span>{{ result.equity_curve.length }} 个样本</span>
        </div>
        <strong>${{ formatNumber(latestClose) }}</strong>
      </div>
      <svg class="chart-svg" viewBox="0 0 520 210" role="img" aria-label="回测价格走势">
        <line x1="46" y1="22" x2="46" y2="162" class="axis" />
        <line x1="46" y1="162" x2="496" y2="162" class="axis" />
        <line x1="46" :y1="buyLineY" x2="496" :y2="buyLineY" class="threshold threshold-buy" />
        <line x1="46" :y1="sellLineY" x2="496" :y2="sellLineY" class="threshold threshold-sell" />
        <path v-if="pricePath" :d="pricePath" class="price-line" />
        <g v-for="marker in markerPositions" :key="marker.id" data-testid="backtest-trade-marker">
          <circle :cx="marker.x" :cy="marker.y" r="5" :class="marker.kind" />
          <text :x="marker.x" :y="marker.y - 9" text-anchor="middle" class="marker-label">{{ marker.label }}</text>
        </g>
        <text x="8" y="27" class="axis-label">{{ formatNumber(priceMax) }}</text>
        <text x="8" y="166" class="axis-label">{{ formatNumber(priceMin) }}</text>
      </svg>
      <div class="chart-legend">
        <span><i class="legend-line price" />收盘价</span>
        <span><i class="legend-line buy-line" />买入线</span>
        <span><i class="legend-line sell-line" />卖出线</span>
        <span><i class="legend-dot buy" />交易</span>
      </div>
    </div>

    <div class="chart-block" data-testid="backtest-equity-chart">
      <div class="chart-heading">
        <div>
          <h4>收益曲线</h4>
          <span>最大回撤 {{ result.metrics.max_drawdown_pct.toFixed(2) }}%</span>
        </div>
        <strong :class="metricClass(result.metrics.total_pnl)">{{ signedCurrency(result.metrics.total_pnl) }}</strong>
      </div>
      <svg class="chart-svg" viewBox="0 0 520 210" role="img" aria-label="回测收益曲线">
        <line x1="46" y1="22" x2="46" y2="162" class="axis" />
        <line x1="46" y1="162" x2="496" y2="162" class="axis" />
        <path v-if="equityAreaPath" :d="equityAreaPath" class="equity-area" />
        <path v-if="equityPath" :d="equityPath" class="equity-line" />
        <text x="8" y="27" class="axis-label">{{ formatNumber(equityMax) }}</text>
        <text x="8" y="166" class="axis-label">{{ formatNumber(equityMin) }}</text>
      </svg>
      <div class="chart-legend">
        <span><i class="legend-line equity" />账户权益</span>
        <span><i class="legend-dot drawdown" />含浮动盈亏</span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { BacktestResult, BacktestTradeLog } from '../types'

const props = defineProps<{
  result: BacktestResult
}>()

const chart = {
  left: 46,
  right: 496,
  top: 22,
  bottom: 162,
}

const closes = computed(() => props.result.equity_curve.map((point) => point.close))
const equities = computed(() => props.result.equity_curve.map((point) => point.equity))
const latestClose = computed(() => closes.value[closes.value.length - 1] ?? 0)

const priceMin = computed(() => {
  const candidates = [
    ...closes.value,
    ...props.result.trades.map((trade) => trade.price),
    props.result.params.buy_low,
    props.result.params.sell_high,
  ].filter((value) => value > 0)
  return paddedMin(candidates)
})

const priceMax = computed(() => {
  const candidates = [
    ...closes.value,
    ...props.result.trades.map((trade) => trade.price),
    props.result.params.buy_low,
    props.result.params.sell_high,
  ].filter((value) => value > 0)
  return paddedMax(candidates)
})

const equityMin = computed(() => paddedMin(equities.value))
const equityMax = computed(() => paddedMax(equities.value))

const pricePath = computed(() => pathForValues(closes.value, priceMin.value, priceMax.value))
const equityPath = computed(() => pathForValues(equities.value, equityMin.value, equityMax.value))
const equityAreaPath = computed(() => {
  if (!equityPath.value || equities.value.length === 0) return ''
  const firstX = xForIndex(0, equities.value.length)
  const lastX = xForIndex(equities.value.length - 1, equities.value.length)
  return `${equityPath.value} L ${lastX.toFixed(2)} ${chart.bottom} L ${firstX.toFixed(2)} ${chart.bottom} Z`
})

const buyLineY = computed(() => yForValue(props.result.params.buy_low, priceMin.value, priceMax.value))
const sellLineY = computed(() => yForValue(props.result.params.sell_high, priceMin.value, priceMax.value))

const markerPositions = computed(() => {
  if (props.result.equity_curve.length === 0) return []
  const firstTs = Date.parse(props.result.equity_curve[0].timestamp)
  const lastTs = Date.parse(props.result.equity_curve[props.result.equity_curve.length - 1].timestamp)
  const span = Math.max(lastTs - firstTs, 1)
  return props.result.trades.map((trade, index) => {
    const ratio = Math.min(1, Math.max(0, (Date.parse(trade.timestamp) - firstTs) / span))
    return {
      id: `${trade.timestamp}-${trade.action}-${index}`,
      x: chart.left + ratio * (chart.right - chart.left),
      y: yForValue(trade.price, priceMin.value, priceMax.value),
      kind: markerClass(trade),
      label: markerLabel(trade.action),
    }
  })
})

function paddedMin(values: number[]): number {
  if (values.length === 0) return 0
  const min = values.reduce((a, b) => Math.min(a, b), values[0])
  const max = values.reduce((a, b) => Math.max(a, b), values[0])
  return min - Math.max((max - min) * 0.08, 0.01)
}

function paddedMax(values: number[]): number {
  if (values.length === 0) return 1
  const min = values.reduce((a, b) => Math.min(a, b), values[0])
  const max = values.reduce((a, b) => Math.max(a, b), values[0])
  return max + Math.max((max - min) * 0.08, 0.01)
}

function xForIndex(index: number, count: number): number {
  if (count <= 1) return chart.left
  return chart.left + (index / (count - 1)) * (chart.right - chart.left)
}

function yForValue(value: number, min: number, max: number): number {
  const range = max - min || 1
  return chart.bottom - ((value - min) / range) * (chart.bottom - chart.top)
}

function pathForValues(values: number[], min: number, max: number): string {
  if (values.length === 0) return ''
  return values
    .map((value, index) => `${index === 0 ? 'M' : 'L'} ${xForIndex(index, values.length).toFixed(2)} ${yForValue(value, min, max).toFixed(2)}`)
    .join(' ')
}

function markerClass(trade: BacktestTradeLog): string {
  if (trade.action === 'BUY' || trade.action === 'BUY_TO_COVER' || trade.action === 'STOP_LOSS_COVER') {
    return 'marker-buy'
  }
  if (trade.action.startsWith('STOP_LOSS')) return 'marker-stop'
  return 'marker-sell'
}

function markerLabel(action: string): string {
  if (action === 'SELL_SHORT') return 'SS'
  if (action === 'BUY_TO_COVER') return 'C'
  if (action.startsWith('STOP_LOSS')) return 'X'
  return action === 'BUY' ? 'B' : 'S'
}

function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}
</script>

<style scoped>
.backtest-chart {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 12px;
}

.chart-block {
  min-height: 292px;
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
  height: 210px;
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

.price-line,
.equity-line {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2.4;
}

.price-line {
  stroke: #2563eb;
}

.equity-line {
  stroke: #7c3aed;
}

.equity-area {
  fill: rgba(124, 58, 237, .12);
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

.marker-buy,
.marker-sell,
.marker-stop {
  stroke: #fff;
  stroke-width: 2;
}

.marker-buy {
  fill: #14884f;
}

.marker-sell {
  fill: #c43838;
}

.marker-stop {
  fill: #b45309;
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

.legend-line.equity {
  background: #7c3aed;
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

.legend-dot.drawdown {
  background: rgba(124, 58, 237, .5);
}

.metric-positive {
  color: #14884f !important;
}

.metric-negative {
  color: #c43838 !important;
}

@media (max-width: 960px) {
  .backtest-chart {
    grid-template-columns: 1fr;
  }
}
</style>
