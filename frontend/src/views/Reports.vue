<template>
  <div class="reports-page" data-testid="reports-view">
    <div class="reports-header">
      <div>
        <h3>交易报告</h3>
        <p>按时间段汇总交易表现与 LLM 建议效果</p>
      </div>
    </div>

    <el-card class="filter-card">
      <el-form :inline="true" @submit.prevent="handleSearch">
        <el-form-item label="股票代码">
          <span data-testid="reports-symbol-input">
            <el-input v-model="form.symbol" placeholder="例如 AAPL.US" style="width: 160px" />
          </span>
        </el-form-item>
        <el-form-item label="开始日期">
          <span data-testid="reports-from-date">
            <el-date-picker v-model="form.from_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
          </span>
        </el-form-item>
        <el-form-item label="结束日期">
          <span data-testid="reports-to-date">
            <el-date-picker v-model="form.to_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
          </span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleSearch" data-testid="reports-search">查询</el-button>
          <el-button plain :disabled="!reportData" @click="handleExport('json')" data-testid="reports-export-json">导出 JSON</el-button>
          <el-button plain :disabled="!reportData" @click="handleExport('csv')" data-testid="reports-export-csv">导出 CSV</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="reportData">
      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="pnlClass">{{ signedCurrency(reportData.metrics.total_pnl) }}</div>
            <div class="summary-label">总盈亏</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reportData.metrics.total_trades }}</div>
            <div class="summary-label">总交易次数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="winRateClass">{{ (reportData.metrics.win_rate * 100).toFixed(1) }}%</div>
            <div class="summary-label">胜率</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reportData.metrics.avg_pnl_per_trade.toFixed(2) }}</div>
            <div class="summary-label">均笔盈亏</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value positive">+{{ reportData.metrics.max_profit.toFixed(2) }}</div>
            <div class="summary-label">最大盈利</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value negative">{{ reportData.metrics.max_loss.toFixed(2) }}</div>
            <div class="summary-label">最大亏损</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ (reportData.metrics.llm_apply_rate * 100).toFixed(1) }}% / {{ (reportData.metrics.llm_accuracy_rate * 100).toFixed(1) }}%</div>
            <div class="summary-label">LLM 采纳率 / 准确率</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value negative">{{ reportData.metrics.max_drawdown.toFixed(2) }}</div>
            <div class="summary-label">最大回撤</div>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-if="reportData.daily_points.length > 0" class="chart-card">
        <template #header>
          <span>每日盈亏趋势</span>
        </template>
        <div class="chart-container">
          <svg :width="chartWidth" :height="chartHeight" class="pnl-chart">
            <line v-for="i in 5" :key="'h' + i"
              :x1="padding.left" :y1="padding.top + (chartHeight - padding.top - padding.bottom) * i / 5"
              :x2="chartWidth - padding.right" :y2="padding.top + (chartHeight - padding.top - padding.bottom) * i / 5"
              stroke="#e1e7f0" stroke-width="1" />
            <text v-for="(point, idx) in xAxisLabels" :key="'x' + idx"
              :x="point.x" :y="chartHeight - padding.bottom + 20"
              text-anchor="middle" font-size="11" fill="#6b7280">
              {{ point.label }}
            </text>
            <rect v-for="(bar, idx) in bars" :key="'bar' + idx"
              :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height"
              :fill="bar.pnl >= 0 ? '#14884f' : '#c43838'" rx="2" />
            <polyline :points="cumulativePolylinePoints" class="cumulative-pnl-line" fill="none" stroke="#2f6fed" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
            <line :x1="padding.left" :y1="zeroY"
              :x2="chartWidth - padding.right" :y2="zeroY"
              stroke="#172033" stroke-width="1" stroke-dasharray="4" />
          </svg>
        </div>
      </el-card>

      <el-card v-if="reportData.daily_points.length > 0" class="table-card">
        <template #header>
          <span>每日明细</span>
        </template>
        <el-table :data="reportData.daily_points" style="width: 100%">
          <el-table-column prop="date" label="日期" width="120" />
          <el-table-column prop="trade_count" label="交易次数" width="100" />
          <el-table-column prop="win_count" label="盈利次数" width="100" />
          <el-table-column label="盈亏" width="120">
            <template #default="{ row }">
              <span :class="row.pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="累计盈亏" width="120">
            <template #default="{ row }">
              <span :class="row.cumulative_pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.cumulative_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="回撤" width="120">
            <template #default="{ row }">
              <span :class="row.drawdown > 0 ? 'negative' : ''">{{ row.drawdown.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="胜率">
            <template #default="{ row }">
              <span>{{ row.trade_count > 0 ? ((row.win_count / row.trade_count) * 100).toFixed(1) : '0' }}%</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-empty v-else description="该报告区间没有日内交易记录" />
    </template>

    <el-empty v-else-if="searched && !loading" description="尚未加载报告，请先查询" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { getRangeReport, exportReport } from '../api/reports'
import type { ReportResponse } from '../types'

const form = ref({
  symbol: 'AAPL.US',
  from_date: daysAgo(30),
  to_date: formatDate(new Date()),
})

const loading = ref(false)
const searched = ref(false)
const reportData = ref<ReportResponse | null>(null)

const pnlClass = computed(() => {
  if (!reportData.value) return ''
  return reportData.value.metrics.total_pnl >= 0 ? 'positive' : 'negative'
})

const winRateClass = computed(() => {
  if (!reportData.value) return ''
  return reportData.value.metrics.win_rate >= 0.5 ? 'positive' : 'negative'
})

const chartWidth = 800
const chartHeight = 300
const padding = { top: 20, right: 20, bottom: 50, left: 60 }

const chartData = computed(() => {
  if (!reportData.value || reportData.value.daily_points.length === 0) return []
  return reportData.value.daily_points
})

const maxAbsChartValue = computed(() => {
  if (chartData.value.length === 0) return 1
  const values = chartData.value.flatMap(point => [Math.abs(point.pnl), Math.abs(point.cumulative_pnl)])
  return Math.max(...values, 1)
})

const cumulativePolylinePoints = computed(() => {
  const data = chartData.value
  if (data.length === 0) return ''
  const plotWidth = chartWidth - padding.left - padding.right
  const plotHeight = chartHeight - padding.top - padding.bottom
  const spacing = plotWidth / data.length
  const maxBarHeight = plotHeight / 2 - 10

  return data
    .map((point, idx) => {
      const x = padding.left + idx * spacing + spacing / 2
      const y = zeroY.value - (point.cumulative_pnl / maxAbsChartValue.value) * maxBarHeight
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})

const zeroY = computed(() => {
  const plotHeight = chartHeight - padding.top - padding.bottom
  return padding.top + plotHeight / 2
})

const bars = computed(() => {
  const data = chartData.value
  if (data.length === 0) return []
  const plotWidth = chartWidth - padding.left - padding.right
  const plotHeight = chartHeight - padding.top - padding.bottom
  const spacing = plotWidth / data.length
  const barWidth = Math.max(1, Math.min(24, spacing * 0.6))

  return data.map((point, idx) => {
    const x = padding.left + idx * spacing + (spacing - barWidth) / 2
    const maxBarHeight = plotHeight / 2 - 10
    const barHeight = Math.min(maxBarHeight, (Math.abs(point.pnl) / maxAbsChartValue.value) * maxBarHeight)
    const y = point.pnl >= 0 ? zeroY.value - barHeight : zeroY.value
    return { x, y, width: barWidth, height: barHeight, pnl: point.pnl }
  })
})

const xAxisLabels = computed(() => {
  const data = chartData.value
  if (data.length === 0) return []
  const plotWidth = chartWidth - padding.left - padding.right
  const spacing = plotWidth / data.length
  const step = Math.max(1, Math.ceil(data.length / 10))

  return data.map((point, idx) => ({
    x: padding.left + idx * spacing + spacing / 2,
    label: idx % step === 0 ? point.date.slice(5) : '',
  }))
})

function handleSearch() {
  if (!validateForm()) {
    return
  }
  loading.value = true
  searched.value = true
  getRangeReport({
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
  })
    .then((res) => {
      reportData.value = res
    })
    .catch(() => {
      ElMessage.error('查询报告数据失败')
      reportData.value = null
    })
    .finally(() => {
      loading.value = false
    })
}

function handleExport(fmt: 'json' | 'csv') {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) return
  exportReport({
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
      link.download = `report_${form.value.symbol.split('.').join('_')}_${form.value.from_date}_${form.value.to_date}.${fmt}`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      ElMessage.success(`导出 ${fmt.toUpperCase()} 成功`)
    })
    .catch(() => {
      ElMessage.error('导出失败')
    })
}

function signedCurrency(value: number): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function validateForm(): boolean {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) {
    ElMessage.warning('请填写完整的查询条件')
    return false
  }
  if (form.value.from_date > form.value.to_date) {
    ElMessage.warning('开始日期不能晚于结束日期')
    return false
  }
  return true
}

function formatDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function daysAgo(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return formatDate(date)
}
</script>

<style scoped>
.reports-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.reports-header h3 {
  margin: 0;
}

.reports-header p {
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

.summary-label {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.chart-card {
  margin-bottom: 8px;
}

.chart-container {
  overflow-x: auto;
}

.pnl-chart {
  display: block;
  min-width: 600px;
}

.cumulative-pnl-line {
  pointer-events: none;
}

.table-card {
  margin-bottom: 8px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 520px) {
  .reports-page {
    padding: 8px;
    gap: 12px;
  }
}
</style>
