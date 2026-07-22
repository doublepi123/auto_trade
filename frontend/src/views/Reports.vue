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
          <el-button size="small" plain :disabled="loading" data-testid="reports-preset-7d" @click="applyRangePreset(7)">近 7 天</el-button>
          <el-button size="small" plain :disabled="loading" data-testid="reports-preset-30d" @click="applyRangePreset(30)">近 30 天</el-button>
          <el-button size="small" plain :disabled="loading" data-testid="reports-preset-90d" @click="applyRangePreset(90)">近 90 天</el-button>
          <el-button plain :disabled="!reportData" @click="handleExport('json')" data-testid="reports-export-json">导出 JSON</el-button>
          <el-button plain :disabled="!reportData" @click="handleExport('csv')" data-testid="reports-export-csv">导出 CSV</el-button>
          <el-button
            plain
            :disabled="!reportData || reportData.daily_points.length === 0"
            data-testid="reports-export-local-csv"
            @click="handleExportLocalCsv"
          >
            本地导出明细 CSV
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="reportData">
      <div class="report-context-card">
        <span data-testid="reports-query-summary">当前报告：{{ reportData.symbol }} · {{ reportData.start_date }} 至 {{ reportData.end_date }}</span>
        <span class="muted" data-testid="reports-export-preview">导出文件：{{ exportBaseName }}.json / .csv</span>
        <span class="muted" data-testid="reports-last-refresh">更新于 {{ lastRefreshedLabel }}</span>
      </div>

      <StatisticsQualityAlert :quality="reportData.statistics_quality" />

      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="pnlClass">{{ signedCurrency(reportData.metrics.total_pnl) }}</div>
            <div class="summary-label">总盈亏</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reportData.metrics.total_trades ?? 0 }}</div>
            <div class="summary-label">总交易次数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="winRateClass">{{ ((reportData.metrics.win_rate ?? 0) * 100).toFixed(1) }}%</div>
            <div class="summary-label">胜率</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ (reportData.metrics.avg_pnl_per_trade ?? 0).toFixed(2) }}</div>
            <div class="summary-label">均笔盈亏</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value positive">+{{ (reportData.metrics.max_profit ?? 0).toFixed(2) }}</div>
            <div class="summary-label">最大盈利</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value negative">{{ (reportData.metrics.max_loss ?? 0).toFixed(2) }}</div>
            <div class="summary-label">最大亏损</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ ((reportData.metrics.llm_apply_rate ?? 0) * 100).toFixed(1) }}% / {{ ((reportData.metrics.llm_accuracy_rate ?? 0) * 100).toFixed(1) }}%</div>
            <div class="summary-label">LLM 采纳率 / 准确率</div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value negative">{{ (reportData.metrics.max_drawdown ?? 0).toFixed(2) }}</div>
            <div class="summary-label">最大回撤</div>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-if="reportInsights" class="insights-card" data-testid="reports-insights">
        <template #header>
          <span>报告洞察</span>
        </template>
        <div class="insights-grid">
          <div class="insight-item">
            <span>最佳日</span>
            <strong :class="reportInsights.bestDay.pnl >= 0 ? 'positive' : 'negative'">{{ reportInsights.bestDay.date }} · {{ signedCurrency(reportInsights.bestDay.pnl) }}</strong>
          </div>
          <div class="insight-item">
            <span>最差日</span>
            <strong :class="reportInsights.worstDay.pnl >= 0 ? 'positive' : 'negative'">{{ reportInsights.worstDay.date }} · {{ signedCurrency(reportInsights.worstDay.pnl) }}</strong>
          </div>
          <div class="insight-item">
            <span>盈利日/亏损日</span>
            <strong>{{ reportInsights.profitableDays }} / {{ reportInsights.losingDays }}</strong>
          </div>
          <div class="insight-item">
            <span>最大回撤日</span>
            <strong :class="reportInsights.maxDrawdownDay.drawdown > 0 ? 'negative' : ''">{{ reportInsights.maxDrawdownDay.date }} · {{ reportInsights.maxDrawdownDay.drawdown.toFixed(2) }}</strong>
          </div>
          <div v-if="pnlConsistency" class="insight-item" data-testid="reports-consistency">
            <span>日均 / 波动</span>
            <strong :class="pnlConsistency.mean >= 0 ? 'positive' : 'negative'">
              {{ signedCurrency(pnlConsistency.mean) }} / ±{{ pnlConsistency.std.toFixed(2) }}
            </strong>
            <small>稳定性 {{ pnlConsistency.ratio === null ? '—' : pnlConsistency.ratio.toFixed(2) }}</small>
          </div>
        </div>
      </el-card>

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

      <el-card class="table-card">
        <template #header>
          <span>交易归因</span>
        </template>
        <el-table v-if="reportData.attribution.length > 0" :data="reportData.attribution" style="width: 100%" data-testid="reports-attribution-table">
          <el-table-column prop="label" label="归因" min-width="120" />
          <el-table-column prop="trade_count" label="交易次数" width="100" />
          <el-table-column label="盈亏" width="120">
            <template #default="{ row }">
              <span :class="(row.pnl ?? 0) >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.pnl ?? 0) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="胜率" width="100">
            <template #default="{ row }">{{ (((row.win_rate ?? 0) * 100)).toFixed(1) }}%</template>
          </el-table-column>
          <el-table-column label="占比" width="100">
            <template #default="{ row }">{{ (((row.share ?? 0) * 100)).toFixed(1) }}%</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="该报告区间暂无归因数据" />
      </el-card>

      <el-card v-if="reportData.daily_points.length > 0" class="table-card">
        <template #header>
          <span>每日明细</span>
        </template>
        <el-table :data="reportData.daily_points" style="width: 100%" data-testid="reports-daily-table">
          <el-table-column type="expand" width="48">
            <template #default="{ row }">
              <div class="order-details" data-testid="reports-order-details">
                <el-table v-if="ordersForDate(row.date).length > 0" :data="ordersForDate(row.date)" size="small" style="width: 100%">
                  <el-table-column prop="broker_order_id" label="订单号" min-width="120" />
                  <el-table-column prop="side" label="方向" width="110" />
                  <el-table-column prop="quantity" label="数量" width="90" />
                  <el-table-column label="成交价" width="100">
                    <template #default="{ row: order }">{{ order.executed_price.toFixed(2) }}</template>
                  </el-table-column>
                  <el-table-column prop="status" label="状态" width="100" />
                  <el-table-column label="成交时间" min-width="160">
                    <template #default="{ row: order }">{{ order.filled_at ?? '-' }}</template>
                  </el-table-column>
                  <el-table-column label="盈亏" width="110">
                    <template #default="{ row: order }">
                      <span :class="(order.pnl ?? 0) >= 0 ? 'positive' : 'negative'">{{ signedCurrency(order.pnl ?? 0) }}</span>
                    </template>
                  </el-table-column>
                </el-table>
                <el-empty v-else description="该日暂无订单明细" />
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="date" label="日期" width="120" sortable />
          <el-table-column prop="trade_count" label="交易次数" width="100" sortable />
          <el-table-column prop="win_count" label="盈利次数" width="100" sortable />
          <el-table-column label="盈亏" width="120" sortable :sort-by="(row: ReportDailyPoint) => row.pnl ?? 0">
            <template #default="{ row }">
              <span :class="(row.pnl ?? 0) >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.pnl ?? 0) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="累计盈亏" width="120" sortable :sort-by="(row: ReportDailyPoint) => row.cumulative_pnl ?? 0">
            <template #default="{ row }">
              <span :class="(row.cumulative_pnl ?? 0) >= 0 ? 'positive' : 'negative'">{{ signedCurrency(row.cumulative_pnl ?? 0) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="回撤" width="120" sortable :sort-by="(row: ReportDailyPoint) => row.drawdown ?? 0">
            <template #default="{ row }">
              <span :class="(row.drawdown ?? 0) > 0 ? 'negative' : ''">{{ (row.drawdown ?? 0).toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="胜率">
            <template #default="{ row }">
              <span>{{ row.trade_count > 0 ? (((row.win_count ?? 0) / row.trade_count) * 100).toFixed(1) : '0' }}%</span>
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
import { useRegisterViewRefresh } from '../composables/useViewRefreshRegistry'
import { ElMessage } from 'element-plus'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'
import { getRangeReport, exportReport } from '../api/reports'
import { downloadCsv } from '../utils/csv'
import type { ReportDailyPoint, ReportOrderDetail, ReportResponse } from '../types'

interface ReportInsights {
  bestDay: ReportDailyPoint
  worstDay: ReportDailyPoint
  maxDrawdownDay: ReportDailyPoint
  profitableDays: number
  losingDays: number
}

interface ReportQuery {
  symbol: string
  from_date: string
  to_date: string
}

const form = ref({
  symbol: 'AAPL.US',
  from_date: daysAgo(30),
  to_date: formatDate(new Date()),
})

const loading = ref(false)
const searched = ref(false)
const reportData = ref<ReportResponse | null>(null)
const lastRefreshedAt = ref<Date | null>(null)
const lastRefreshedLabel = computed(() => {
  if (!lastRefreshedAt.value) return '未刷新'
  return lastRefreshedAt.value.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
})
const submittedQuery = ref<ReportQuery | null>(null)
// Register a refresh that re-runs the current report query (if any) for the
// command palette's "refresh page".
useRegisterViewRefresh(() => {
  if (submittedQuery.value) handleSearch()
})

const pnlClass = computed(() => {
  if (!reportData.value) return ''
  return reportData.value.metrics.total_pnl >= 0 ? 'positive' : 'negative'
})

const winRateClass = computed(() => {
  if (!reportData.value) return ''
  return reportData.value.metrics.win_rate >= 0.5 ? 'positive' : 'negative'
})

const exportQuery = computed<ReportQuery | null>(() => submittedQuery.value)
const exportBaseName = computed(() => {
  const query = exportQuery.value ?? form.value
  return `report_${query.symbol.split('.').join('_')}_${query.from_date}_${query.to_date}`
})

const reportInsights = computed<ReportInsights | null>(() => {
  const points = reportData.value?.daily_points ?? []
  if (points.length === 0) return null
  const bestDay = points.reduce((best, point) => point.pnl > best.pnl ? point : best, points[0])
  const worstDay = points.reduce((worst, point) => point.pnl < worst.pnl ? point : worst, points[0])
  const maxDrawdownDay = points.reduce((worst, point) => point.drawdown > worst.drawdown ? point : worst, points[0])
  return {
    bestDay,
    worstDay,
    maxDrawdownDay,
    profitableDays: points.filter((point) => point.pnl > 0).length,
    losingDays: points.filter((point) => point.pnl < 0).length,
  }
})

/** Daily-PnL consistency derived client-side: mean, population std-dev, and a
 * mean/std stability ratio (higher = more consistent daily outcome). Reuses
 * already-loaded daily_points only — no extra request. */
const pnlConsistency = computed<{ mean: number; std: number; ratio: number | null } | null>(() => {
  const points = reportData.value?.daily_points ?? []
  if (points.length < 2) return null
  const pnls = points.map((p) => p.pnl)
  const mean = pnls.reduce((a, b) => a + b, 0) / pnls.length
  const variance = pnls.reduce((a, b) => a + (b - mean) ** 2, 0) / pnls.length
  const std = Math.sqrt(variance)
  return { mean, std, ratio: std > 0 ? mean / std : null }
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
  const query: ReportQuery = {
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
  }
  getRangeReport(query)
    .then((res) => {
      reportData.value = res
      submittedQuery.value = query
      lastRefreshedAt.value = new Date()
    })
    .catch(() => {
      ElMessage.error('查询报告数据失败')
      reportData.value = null
      submittedQuery.value = null
    })
    .finally(() => {
      loading.value = false
    })
}

function applyRangePreset(days: number) {
  form.value.from_date = daysAgo(Math.max(0, days - 1))
  form.value.to_date = formatDate(new Date())
  handleSearch()
}

function handleExportLocalCsv() {
  const data = reportData.value
  if (!data || data.daily_points.length === 0) return
  const rows = data.daily_points.map((p) => ({
    date: p.date,
    trade_count: p.trade_count,
    win_count: p.win_count,
    pnl: (p.pnl ?? 0).toFixed(2),
    cumulative_pnl: (p.cumulative_pnl ?? 0).toFixed(2),
    drawdown: (p.drawdown ?? 0).toFixed(2),
    win_rate: p.trade_count > 0 ? (((p.win_count ?? 0) / p.trade_count) * 100).toFixed(1) : '0',
  }))
  downloadCsv(`${exportBaseName.value}_daily.csv`, [
    { key: 'date', label: 'date' },
    { key: 'trade_count', label: 'trade_count' },
    { key: 'win_count', label: 'win_count' },
    { key: 'pnl', label: 'pnl' },
    { key: 'cumulative_pnl', label: 'cumulative_pnl' },
    { key: 'drawdown', label: 'drawdown' },
    { key: 'win_rate', label: 'win_rate(%)' },
  ], rows)
  ElMessage.success('已本地导出每日明细')
}

function handleExport(fmt: 'json' | 'csv') {
  const query = exportQuery.value
  if (!query) return
  exportReport({
    symbol: query.symbol,
    from_date: query.from_date,
    to_date: query.to_date,
    format: fmt,
  })
    .then((res) => {
      const blob: Blob = res instanceof Blob ? res : new Blob([JSON.stringify(res)], { type: fmt === 'json' ? 'application/json' : 'text/csv' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${exportBaseName.value}.${fmt}`
      document.body.appendChild(link)
      // Revoke the object URL on click rather than after a 1-second timer to
      // avoid races when the user fires several exports in quick succession
      // (each link was previously holding its URL alive for a full second).
      const cleanup = () => {
        URL.revokeObjectURL(url)
        link.removeEventListener('click', cleanup)
        if (link.parentNode) link.parentNode.removeChild(link)
      }
      link.addEventListener('click', cleanup)
      link.click()
      setTimeout(cleanup, 1000)
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

function ordersForDate(date: string): ReportOrderDetail[] {
  return reportData.value?.details.find((detail) => detail.date === date)?.orders ?? []
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

.report-context-card {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: space-between;
  padding: 10px 12px;
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #f8fafc;
  color: #374151;
  font-size: 13px;
}

.muted {
  color: #6b7280;
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

.insights-card {
  margin-bottom: 8px;
}

.insights-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}

.insight-item {
  padding: 10px 12px;
  border-radius: 8px;
  background: #f8fafc;
}

.insight-item span {
  display: block;
  margin-bottom: 4px;
  color: #6b7280;
  font-size: 12px;
}

.insight-item strong {
  color: #172033;
}

.insight-item strong.positive {
  color: #14884f;
}

.insight-item strong.negative {
  color: #c43838;
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

.order-details {
  padding: 8px 16px;
  background: #f8fafc;
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
