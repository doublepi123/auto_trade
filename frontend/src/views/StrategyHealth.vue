<template>
  <div class="strategy-health-page" data-testid="strategy-health-page">
    <div class="page-header">
      <div>
        <h3>策略健康度监控</h3>
        <p>对比实盘与影子策略表现，监控漂移与健康状态</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="symbol"
          placeholder="可选标的筛选（留空为全部）"
          style="width: 200px"
          clearable
          data-testid="health-symbol-input"
          @keyup.enter="loadAll"
        />
        <el-select v-model="weeks" style="width: 120px" data-testid="health-weeks-select" @change="loadTrend">
          <el-option label="近 4 周" :value="4" />
          <el-option label="近 8 周" :value="8" />
          <el-option label="近 12 周" :value="12" />
          <el-option label="近 24 周" :value="24" />
        </el-select>
        <el-button type="primary" :loading="loading" data-testid="health-query-btn" @click="loadAll">查询</el-button>
      </div>
    </div>

    <el-card v-loading="loading" class="section-card">
      <template #header><span>健康状态</span></template>
      <el-empty v-if="!report && !loading" description="暂无健康报告，请查询" />
      <template v-else-if="report">
        <div class="health-banner">
          <el-tag
            size="large"
            :type="statusTagType(report.health_status)"
            effect="dark"
            data-testid="health-status-tag"
          >{{ statusLabelCn(report.health_status) }}</el-tag>
          <el-tag type="info" effect="plain">周期 {{ report.period_days }} 天</el-tag>
          <el-tag v-if="report.symbol" type="info" effect="plain">{{ report.symbol }}</el-tag>
          <el-tag v-else type="info" effect="plain">全部标的</el-tag>
        </div>
        <div v-if="report.alerts.length" class="alerts-list">
          <el-alert
            v-for="(alert, idx) in report.alerts"
            :key="idx"
            :title="alert"
            type="warning"
            :closable="false"
            show-icon
          />
        </div>
      </template>
    </el-card>

    <template v-if="report">
      <el-row :gutter="12" class="summary-row">
        <el-col :xs="24" :md="12">
          <el-card class="section-card">
            <template #header><span>指标对比（实盘 vs 影子）</span></template>
            <el-table :data="comparisonRows" style="width: 100%" data-testid="health-comparison-table">
              <el-table-column prop="metric" label="指标" min-width="120" />
              <el-table-column label="实盘" min-width="120">
                <template #default="{ row }">
                  <span :class="{ negative: row.isNegative && row.live < 0 }">{{ row.live }}</span>
                </template>
              </el-table-column>
              <el-table-column label="影子" min-width="120">
                <template #default="{ row }">
                  <span :class="{ negative: row.isNegative && row.shadow < 0 }">{{ row.shadow }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-card class="section-card">
            <template #header><span>漂移监控</span></template>
            <div class="drift-block">
              <div class="drift-item">
                <div class="drift-label">胜率漂移</div>
                <el-progress :percentage="toPct(report.drift.win_rate_drift)" :color="driftColor(report.drift.win_rate_drift)" />
                <div class="drift-pct">{{ (report.drift.win_rate_drift * 100).toFixed(2) }}%</div>
              </div>
              <div class="drift-item">
                <div class="drift-label">盈亏漂移</div>
                <el-progress :percentage="toPct(report.drift.pnl_drift)" :color="driftColor(report.drift.pnl_drift)" />
                <div class="drift-pct">{{ (report.drift.pnl_drift * 100).toFixed(2) }}%</div>
              </div>
              <div class="drift-item">
                <div class="drift-label">交易频率漂移</div>
                <el-progress :percentage="toPct(report.drift.trade_frequency_drift)" :color="driftColor(report.drift.trade_frequency_drift)" />
                <div class="drift-pct">{{ (report.drift.trade_frequency_drift * 100).toFixed(2) }}%</div>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card class="section-card">
        <template #header><span>每周表现趋势</span></template>
        <el-table
          v-loading="trendLoading"
          :data="trend"
          style="width: 100%"
          empty-text="暂无趋势数据"
          data-testid="health-trend-table"
        >
          <el-table-column prop="week_start" label="周起始" width="130" sortable />
          <el-table-column label="实盘胜率" min-width="110" sortable :sort-by="(row: TrendRow) => row.live_win_rate">
            <template #default="{ row }">{{ (row.live_win_rate * 100).toFixed(1) }}%</template>
          </el-table-column>
          <el-table-column label="影子胜率" min-width="110" sortable :sort-by="(row: TrendRow) => row.shadow_win_rate">
            <template #default="{ row }">{{ (row.shadow_win_rate * 100).toFixed(1) }}%</template>
          </el-table-column>
          <el-table-column label="实盘均笔" min-width="110" sortable :sort-by="(row: TrendRow) => row.live_avg_pnl">
            <template #default="{ row }">
              <span :class="row.live_avg_pnl >= 0 ? 'positive' : 'negative'">{{ row.live_avg_pnl.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="影子均笔" min-width="110" sortable :sort-by="(row: TrendRow) => row.shadow_avg_pnl">
            <template #default="{ row }">
              <span :class="row.shadow_avg_pnl >= 0 ? 'positive' : 'negative'">{{ row.shadow_avg_pnl.toFixed(2) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="live_trades" label="实盘笔数" width="100" sortable />
          <el-table-column prop="shadow_trades" label="影子笔数" width="100" sortable />
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getHealthReport, getPerformanceTrend } from '../api/strategyHealth'
import type { HealthReport, TrendRow, TradeSideMetrics } from '../api/strategyHealth'

const symbol = ref('')
const weeks = ref(12)
const loading = ref(false)
const trendLoading = ref(false)
const report = ref<HealthReport | null>(null)
const trend = ref<TrendRow[]>([])

type HealthStatus = HealthReport['health_status']

function statusTagType(s: HealthStatus): 'success' | 'warning' | 'danger' | 'info' {
  switch (s) {
    case 'HEALTHY': return 'success'
    case 'WARNING': return 'warning'
    case 'DEGRADED': return 'danger'
    default: return 'info'
  }
}

function statusLabelCn(s: HealthStatus): string {
  switch (s) {
    case 'HEALTHY': return '健康'
    case 'WARNING': return '警告'
    case 'DEGRADED': return '退化'
    case 'INSUFFICIENT_DATA': return '数据不足'
    default: return s
  }
}

function driftColor(d: number): string {
  const abs = Math.abs(d)
  if (abs < 0.1) return '#14884f'
  if (abs < 0.3) return '#e6a23c'
  return '#c43838'
}

function toPct(d: number): number {
  return Math.min(100, Math.round(Math.abs(d) * 100))
}

interface ComparisonRow {
  metric: string
  live: string
  shadow: string
  isNegative: boolean
}

function fmtMetric(m: TradeSideMetrics): { win_rate: string; avg_pnl: string; trade_count: string; avg_holding: string; profit_factor: string } {
  return {
    win_rate: (m.win_rate * 100).toFixed(1) + '%',
    avg_pnl: m.avg_pnl.toFixed(2),
    trade_count: String(m.trade_count),
    avg_holding: m.avg_holding_minutes.toFixed(0) + ' 分钟',
    profit_factor: m.profit_factor.toFixed(2),
  }
}

const comparisonRows = computed<ComparisonRow[]>(() => {
  const r = report.value
  if (!r) return []
  const live = fmtMetric(r.live_metrics)
  const shadow = fmtMetric(r.shadow_metrics)
  return [
    { metric: '胜率', live: live.win_rate, shadow: shadow.win_rate, isNegative: false },
    { metric: '均笔盈亏', live: live.avg_pnl, shadow: shadow.avg_pnl, isNegative: true },
    { metric: '交易次数', live: live.trade_count, shadow: shadow.trade_count, isNegative: false },
    { metric: '平均持仓时长', live: live.avg_holding, shadow: shadow.avg_holding, isNegative: false },
    { metric: '盈亏比', live: live.profit_factor, shadow: shadow.profit_factor, isNegative: false },
  ]
})

async function loadReport() {
  loading.value = true
  try {
    report.value = await getHealthReport(symbol.value.trim() || undefined)
  } catch (e) {
    console.error('加载健康报告失败：', e)
    ElMessage.error('加载健康报告失败')
    report.value = null
  } finally {
    loading.value = false
  }
}

async function loadTrend() {
  trendLoading.value = true
  try {
    const trimmed = symbol.value.trim()
    trend.value = await getPerformanceTrend(trimmed ? { symbol: trimmed, weeks: weeks.value } : { weeks: weeks.value })
  } catch (e) {
    console.error('加载趋势失败：', e)
    ElMessage.error('加载趋势失败')
    trend.value = []
  } finally {
    trendLoading.value = false
  }
}

async function loadAll() {
  await Promise.all([loadReport(), loadTrend()])
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.strategy-health-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.section-card {
  margin-bottom: 0;
}

.summary-row {
  margin-bottom: 0;
}

.health-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.alerts-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.drift-block {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.drift-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.drift-label {
  color: #4b5563;
  font-size: 13px;
}

.drift-pct {
  color: #6b7280;
  font-size: 12px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}
</style>
