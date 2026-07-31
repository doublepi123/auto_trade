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

    <el-card v-loading="loading" class="section-card" data-testid="observation-health-card">
      <template #header>
        <div class="card-header">
          <span>研究观察链路</span>
          <el-tag
            v-if="observationHealth"
            :type="observationStatusTagType(observationHealth.status)"
            effect="dark"
            data-testid="observation-health-status"
          >{{ observationStatusLabel(observationHealth.status) }}</el-tag>
        </div>
      </template>
      <el-empty v-if="!observationHealth && !loading" description="暂无观察链路报告" />
      <template v-else-if="observationHealth">
        <el-table :data="observationHealth.components" data-testid="observation-health-table">
          <el-table-column label="链路" min-width="180">
            <template #default="{ row }">{{ observationComponentLabel(row.name) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="observationStatusTagType(row.status)" size="small">
                {{ observationStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最新会话" min-width="120">
            <template #default="{ row }">{{ row.latest_session_date || '—' }}</template>
          </el-table-column>
          <el-table-column label="应到会话" min-width="120">
            <template #default="{ row }">{{ row.expected_session_date || '—' }}</template>
          </el-table-column>
          <el-table-column label="覆盖率" width="110">
            <template #default="{ row }">
              {{ row.coverage_ratio === null ? '—' : `${(row.coverage_ratio * 100).toFixed(1)}%` }}
            </template>
          </el-table-column>
          <el-table-column label="数据年龄" width="120">
            <template #default="{ row }">{{ observationAge(row.age_seconds) }}</template>
          </el-table-column>
          <el-table-column label="阻塞原因" min-width="240">
            <template #default="{ row }">
              {{ row.blockers.map(observationBlockerLabel).join('、') || '—' }}
            </template>
          </el-table-column>
        </el-table>
        <el-alert
          v-for="blocker in observationHealth.blockers"
          :key="blocker"
          :title="observationBlockerLabel(blocker)"
          type="warning"
          :closable="false"
          show-icon
          class="observation-alert"
        />
      </template>
    </el-card>

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
import { getHealthReport, getObservationHealth, getPerformanceTrend } from '../api/strategyHealth'
import type {
  HealthReport,
  ObservationHealthComponent,
  ObservationHealthReport,
  ObservationHealthStatus,
  TrendRow,
  TradeSideMetrics,
} from '../api/strategyHealth'

const symbol = ref('')
const weeks = ref(12)
const loading = ref(false)
const trendLoading = ref(false)
const report = ref<HealthReport | null>(null)
const observationHealth = ref<ObservationHealthReport | null>(null)
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

function observationStatusTagType(s: ObservationHealthStatus): 'success' | 'warning' | 'danger' | 'info' {
  switch (s) {
    case 'HEALTHY': return 'success'
    case 'WARNING': return 'warning'
    case 'DEGRADED': return 'danger'
    default: return 'info'
  }
}

function observationStatusLabel(s: ObservationHealthStatus): string {
  switch (s) {
    case 'HEALTHY': return '健康'
    case 'WARNING': return '警告'
    case 'DEGRADED': return '退化'
    case 'DISABLED': return '未启用'
    default: return s
  }
}

function observationComponentLabel(name: string): string {
  switch (name) {
    case 'UNIVERSE_SELECTION': return '指数股票池刷新'
    case 'ROTATION_FORWARD_PRECOMMITMENT': return '月末轮动预承诺'
    case 'WATCHLIST_QUANT': return '量化评分覆盖'
    case 'DIVERSIFIED_PRIORITY_OBSERVATION': return '分散优先观察'
    case 'GROWTH_SATELLITE_OBSERVATION': return '成长卫星观察'
    case 'LIVE_INTERVAL_ALIGNMENT': return 'Live 区间对齐'
    case 'LIVE_EXIT_CHALLENGER': return 'Live 退出挑战者'
    case 'STRATEGY_V2_EXIT_CHALLENGER': return 'Strategy v2 退出挑战者'
    case 'STRATEGY_V2_FORWARD': return 'Strategy v2 前瞻回放'
    case 'PORTFOLIO_ROUTING': return '组合路由观察'
    case 'OPENING_MOMENTUM_SHADOW': return '开盘策略影子'
    case 'OPENING_MOMENTUM_EXECUTION': return '开盘模拟执行'
    default: return name
  }
}

function observationBlockerLabel(blocker: string): string {
  const separator = blocker.indexOf(':')
  if (separator >= 0) {
    const component = blocker.slice(0, separator)
    const reason = blocker.slice(separator + 1)
    return `${observationComponentLabel(component)}：${observationBlockerLabel(reason)}`
  }
  const counted: Array<[RegExp, string]> = [
    [/^DIVERSIFIED_ELIGIBILITY_INVALID_(\d+)$/, '分散名单中有 $1 只已不满足资格'],
    [/^CURRENT_PROFIT_LOCK_REGISTRATION_MISSING_(\d+)$/, '当前利润锁注册缺失 $1 个方案'],
    [/^CURRENT_EVALUATOR_REGISTRATION_MISSING_(\d+)$/, '当前 v4 注册缺失 $1 个标的'],
    [/^BASELINE_REPLAY_MISMATCH_(\d+)$/, '基线回放不一致 $1 条'],
    [/^STRUCTURAL_FAILURE_(\d+)$/, '前瞻证据结构失败 $1 条'],
    [/^FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_(\d+)$/, '完整交易日后仍缺前瞻证据 $1 个标的'],
  ]
  for (const [pattern, label] of counted) {
    if (pattern.test(blocker)) return blocker.replace(pattern, label)
  }
  const labels: Record<string, string> = {
    DUPLICATE_RISK_GROUP: '分散名单出现重复风险组',
    NON_CONTIGUOUS_DIVERSIFIED_RANKS: '分散名单排名不连续',
    DIVERSIFIED_SHORTLIST_BELOW_4: '分散名单不足 4 只',
    DIVERSIFIED_SHORTLIST_BELOW_8: '分散名单不足 8 只',
    CURRENT_PRICE_UNAVAILABLE: '当前价格不可用',
    CURRENT_PRICE_BELOW_LONG_ENTRY_FLOOR: '当前价格已跌穿多头有效入场下限',
  }
  if (labels[blocker]) return labels[blocker]
  return blocker
}

function observationAge(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  return `${(seconds / 3600).toFixed(1)} 小时`
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

async function loadObservationHealth() {
  try {
    observationHealth.value = await getObservationHealth()
  } catch (e) {
    console.error('加载观察链路健康报告失败：', e)
    ElMessage.error('加载观察链路健康报告失败')
    observationHealth.value = null
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
  await Promise.all([loadReport(), loadObservationHealth(), loadTrend()])
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

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.observation-alert {
  margin-top: 8px;
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
