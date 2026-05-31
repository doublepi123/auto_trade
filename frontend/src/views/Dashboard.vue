<template>
  <div class="dashboard-page">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon :closable="false" class="dashboard-alert">
      <el-button size="small" type="primary" plain @click="handleRetry">重试连接</el-button>
    </el-alert>

    <el-alert v-if="accountError" type="warning" title="账户资产暂时不可用，请检查券商凭证或稍后重试" show-icon class="dashboard-alert" />

    <section class="dashboard-cockpit" data-testid="dashboard-cockpit">
      <div class="page-heading cockpit-heading">
        <div>
          <h3>交易驾驶舱</h3>
          <p>{{ strategy.symbol || '未配置标的' }} · {{ marketLabel(strategy.market) }} · {{ strategy.short_selling ? '允许做空' : '仅做多' }}</p>
        </div>
        <div class="heading-tags">
          <el-tag :type="realtimeStatusType" effect="plain">{{ realtimeStatusLabel }}</el-tag>
          <el-tag :type="status.runner_running ? 'success' : 'info'" effect="plain">{{ status.runner_running ? '运行器运行中' : '运行器未启动' }}</el-tag>
        </div>
      </div>

      <div class="status-strip" data-testid="status-strip" v-loading="statusLoading || strategyLoading">
        <div class="strip-item">
          <span>标的</span>
          <strong>{{ strategy.symbol || '未配置' }}</strong>
          <small>{{ marketLabel(strategy.market) }}</small>
        </div>
        <div class="strip-item">
          <span>交易状态</span>
          <strong>{{ status.paused ? '已暂停' : '运行中' }}</strong>
          <small>{{ status.kill_switch ? '紧急停止开启' : '紧急停止关闭' }}</small>
        </div>
        <div class="strip-item">
          <span>引擎状态</span>
          <strong>{{ engineStateLabel(status.engine_state) }}</strong>
          <small>连续亏损 {{ status.consecutive_losses }}</small>
        </div>
        <div class="strip-item">
          <span>LLM</span>
          <strong>{{ llmStatus?.enabled ? `${llmStatus.interval_minutes}m 自动` : '未启用' }}</strong>
          <small>{{ llmStatus?.last_analysis_at ? formatTime(llmStatus.last_analysis_at) : '暂无刷新' }}</small>
        </div>
        <div class="strip-item">
          <span>今日盈亏</span>
          <strong :class="metricClass(status.daily_pnl)">{{ signedCurrency(status.daily_pnl) }}</strong>
          <small>{{ pnlLabel(status.daily_pnl) }}</small>
        </div>
        <div class="strip-item" data-testid="session-hours-indicator">
          <span>下单时段</span>
          <strong>{{ status.trading_session_mode === 'RTH_ONLY' ? '仅 RTH' : '不限' }}</strong>
          <small class="session-strip-hint">
            <span class="session-dot" :class="sessionOrderDotClass" />
            {{ sessionOrderHint }}
          </small>
        </div>
      </div>

      <div class="cockpit-grid">
        <section class="cockpit-panel price-panel" data-testid="price-panel" v-loading="statusLoading || strategyLoading">
          <div class="panel-heading">
            <span>最新价格</span>
            <el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag>
          </div>
          <div class="price-value">${{ formatNumber(status.last_price) }}</div>
          <div class="range-line" aria-hidden="true">
            <span class="range-fill" :style="{ width: priceRangeWidth }" />
            <span class="range-marker" :style="{ left: priceRangeLeft }" />
          </div>
          <div class="range-labels">
            <span>买入线 ${{ formatNumber(strategy.buy_low) }}</span>
            <span>卖出线 ${{ formatNumber(strategy.sell_high) }}</span>
          </div>
          <div class="mini-grid">
            <div>
              <span>上次触发</span>
              <strong>{{ status.last_trigger_price > 0 ? `$${formatNumber(status.last_trigger_price)}` : '-' }}</strong>
            </div>
            <div>
              <span>最低盈利</span>
              <strong>${{ formatNumber(strategy.min_profit_amount) }}</strong>
            </div>
            <div v-if="status.last_action_message" class="action-message">
              <span>最近动作</span>
              <strong>{{ status.last_action_message }}</strong>
            </div>
          </div>
        </section>

        <section class="cockpit-panel position-panel" data-testid="position-panel" v-loading="accountLoading">
          <div class="panel-heading">
            <span>持仓明细</span>
            <el-tag :type="primaryPosition ? 'success' : 'info'" effect="plain">{{ primaryPosition ? positionSideLabel(primaryPosition.side) : '空仓' }}</el-tag>
          </div>
          <template v-if="primaryPosition">
            <div class="position-symbol">{{ primaryPosition.symbol }}</div>
            <div class="position-main">
              <div>
                <span>数量</span>
                <strong>{{ primaryPosition.quantity.toFixed(0) }}</strong>
              </div>
              <div>
                <span>均价</span>
                <strong>${{ formatNumber(primaryPosition.avg_price) }}</strong>
              </div>
              <div>
                <span>市值</span>
                <strong>${{ formatNumber(primaryPosition.market_value) }}</strong>
              </div>
            </div>
            <div class="pnl-box" :class="metricClass(unrealizedPnl)">
              <span>浮动盈亏</span>
              <strong>{{ signedCurrency(unrealizedPnl) }} / {{ signedPercent(unrealizedPnlPct) }}</strong>
            </div>
          </template>
          <p v-else class="empty-note">暂无持仓</p>
        </section>

        <section class="cockpit-panel llm-panel" data-testid="llm-panel" v-loading="llmStatusLoading">
          <div class="panel-heading">
            <span>LLM 智能区间</span>
            <el-tag :type="llmStatus?.enabled ? 'success' : 'info'" effect="plain">{{ llmStatus?.enabled ? '已启用' : '已禁用' }}</el-tag>
          </div>
          <template v-if="llmStatus?.current_suggestion">
            <div class="llm-decision">
              <strong>{{ llmStatus.current_suggestion.confidence_score.toFixed(2) }}</strong>
              <span>置信度</span>
            </div>
            <p class="llm-range">建议区间 {{ llmStatus.current_suggestion.buy_low.toFixed(2) }} ~ {{ llmStatus.current_suggestion.sell_high.toFixed(2) }}</p>
            <p class="llm-analysis">{{ llmStatus.current_suggestion.analysis }}</p>
            <div class="llm-meta">
              <span>最近刷新：{{ llmStatus.last_analysis_at ? formatDateTime(llmStatus.last_analysis_at) : '-' }}</span>
              <span>下次分析：{{ llmStatus.next_analysis_at ? formatDateTime(llmStatus.next_analysis_at) : '-' }}</span>
            </div>
            <div class="llm-apply-state">
              <el-tag v-if="llmStatus.applied_values" type="success" effect="plain">已应用</el-tag>
              <span v-if="llmStatus.applied_values">当前 {{ llmStatus.applied_values.buy_low.toFixed(2) }} ~ {{ llmStatus.applied_values.sell_high.toFixed(2) }}</span>
              <span v-else-if="llmStatus.reject_reason">未应用：{{ llmStatus.reject_reason }}</span>
            </div>
          </template>
          <p v-else class="empty-note">暂无 LLM 建议</p>
        </section>

        <section class="cockpit-panel action-panel" data-testid="quick-actions" v-loading="statusLoading">
          <div class="panel-heading">
            <span>操作控制</span>
            <el-tag :type="status.kill_switch ? 'danger' : status.paused ? 'warning' : 'success'" effect="plain">
              {{ status.kill_switch ? '紧急停止' : status.paused ? '暂停中' : '可交易' }}
            </el-tag>
          </div>
          <div class="action-grid">
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch || status.runner_running">启动</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused || status.kill_switch">恢复</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused || status.kill_switch">暂停</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button class="kill-button" type="danger" plain @click="handleKillSwitch">紧急停止</el-button>
            <el-button v-if="status.kill_switch" type="success" plain @click="handleDisableKillSwitch">解除紧急停止</el-button>
          </div>
        </section>
      </div>
    </section>

    <section class="chart-grid" data-testid="dashboard-charts">
      <div class="chart-controls">
        <el-button v-if="isMobile" size="small" text @click="chartExpanded = !chartExpanded"
          >{{ chartExpanded ? '收起图表' : '展开图表' }}</el-button
        >
      </div>
      <div v-show="chartExpanded || !isMobile" class="chart-panels">
        <PriceChart
          :points="chartPoints"
          :markers="tradeMarkers"
          :buy-low="strategy.buy_low"
          :sell-high="strategy.sell_high"
        />
        <PnLChart :points="chartPoints" />
      </div>
    </section>

    <section class="detail-grid">
      <div class="detail-panel account-panel" v-loading="accountLoading">
        <div class="section-title">
          <h4>总资产</h4>
          <strong :class="account.available ? 'metric-positive' : 'metric-negative'">${{ formatNumber(account.total_assets) }}</strong>
          <el-tag v-if="accountRefreshing && !accountLoading" size="small" type="info">刷新中</el-tag>
        </div>
        <h4 class="subsection-title">现金余额</h4>
        <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" class="responsive-table">
          <el-table-column prop="currency" label="币种" min-width="80" />
          <el-table-column prop="available_cash" label="可用" min-width="120">
            <template #default="{ row }">${{ formatNumber(row.available_cash) }}</template>
          </el-table-column>
          <el-table-column prop="frozen_cash" label="冻结" min-width="120">
            <template #default="{ row }">${{ formatNumber(row.frozen_cash) }}</template>
          </el-table-column>
        </el-table>
        <p v-else-if="!account.available" class="empty-note">数据不可用</p>
        <p v-else class="empty-note">暂无数据</p>
      </div>

      <div class="detail-panel" v-loading="strategyLoading || statusLoading">
        <div class="section-title">
          <h4>行情信息</h4>
          <span>{{ strategy.symbol || '未配置' }}</span>
        </div>
        <div class="strategy-list">
          <div><span>买入价下限</span><strong>${{ formatNumber(strategy.buy_low) }}</strong></div>
          <div><span>卖出价上限</span><strong>${{ formatNumber(strategy.sell_high) }}</strong></div>
          <div><span>做空</span><strong>{{ strategy.short_selling ? '是' : '否' }}</strong></div>
          <div><span>暂停自动恢复</span><strong>{{ strategy.auto_resume_minutes }} 分钟</strong></div>
        </div>
        <h4 class="subsection-title">风控状态</h4>
        <div class="risk-list">
          <span>紧急停止：{{ status.kill_switch ? '已开启' : '已关闭' }}</span>
          <span>暂停状态：{{ status.paused ? '已暂停' : '运行中' }}</span>
          <span>单日最大亏损：${{ formatNumber(strategy.max_daily_loss) }}</span>
        </div>
      </div>

      <div class="detail-panel recent-orders" data-testid="recent-orders" v-loading="recentOrdersLoading">
        <div class="section-title">
          <h4>最近订单</h4>
          <span>{{ recentOrders.length }} 条</span>
        </div>
        <el-table :data="recentOrders" size="small" v-if="recentOrders.length > 0" class="responsive-table">
          <el-table-column prop="side" label="方向" min-width="90" />
          <el-table-column prop="quantity" label="数量" min-width="70">
            <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" min-width="100" />
          <el-table-column prop="executed_price" label="成交价" min-width="100">
            <template #default="{ row }">{{ row.executed_price ? `$${formatNumber(row.executed_price)}` : '-' }}</template>
          </el-table-column>
        </el-table>
        <p v-else class="empty-note">暂无订单</p>
      </div>

      <div class="detail-panel recent-events" data-testid="recent-events" v-loading="recentEventsLoading">
        <div class="section-title">
          <h4>决策时间线</h4>
          <span>{{ recentEvents.length }} 条</span>
        </div>
        <div v-if="recentEvents.length > 0" class="event-list">
          <div v-for="event in recentEvents" :key="`${event.source}-${event.id}`" class="event-row">
            <div class="event-main">
              <el-tag size="small" :type="eventTagType(event.event_type, event.status, event.source)" effect="plain">
                {{ event.source === 'audit' ? auditActionLabel(event.event_type) : tradeEventTypeLabel(event.event_type) }}
              </el-tag>
              <strong>{{ event.symbol || event.broker_order_id || '-' }}</strong>
              <small>{{ formatDateTime(event.created_at) }}</small>
            </div>
            <p>{{ event.message || event.status || '-' }}</p>
            <small
              v-if="event.event_type === 'ORDER_SKIPPED' && event.payload?.skip_category"
              class="skip-category"
            >{{ skipCategoryLabel(String(event.payload.skip_category)) }}</small>
          </div>
        </div>
        <p v-else class="empty-note">暂无决策事件</p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import PriceChart from '../components/PriceChart.vue'
import PnLChart from '../components/PnLChart.vue'
import { useDashboardData } from '../composables/useDashboardData'
import { useStatusStream } from '../composables/useStatusStream'
import { useAccountRefresh } from '../composables/useAccountRefresh'
import { startTrading, stopTrading, pauseTrading, resumeTrading, activateKillSwitch, disableKillSwitch, getLLMIntervalStatus, getOrders, getTradeEvents, getStatusHistory } from '../api'
import type { LLMIntervalStatus, OrderRecord, Position, StatusHistoryPoint, TradeEventRecord, TradeSignalMarker } from '../types'
import { engineStateLabel, auditActionLabel, marketLabel, positionSideLabel, skipCategoryLabel, tradeEventTypeLabel } from '../utils/labels'

type CypressWindow = Window & { Cypress?: unknown }
const accountRefreshIntervalMs = (window as CypressWindow).Cypress ? 500 : 10000
const { strategy, status, strategyLoading, statusLoading, loadError, load, refreshStatus } = useDashboardData()
const { realtimeStatus } = useStatusStream(status)
const { account, accountError, accountLoading, accountRefreshing, refresh: refreshAccount } = useAccountRefresh(accountRefreshIntervalMs)

const llmStatus = ref<LLMIntervalStatus | null>(null)
const recentOrders = ref<OrderRecord[]>([])
const recentEvents = ref<TradeEventRecord[]>([])
const llmStatusLoading = ref(true)
const recentOrdersLoading = ref(true)
const recentEventsLoading = ref(true)
const chartPoints = ref<StatusHistoryPoint[]>([])
const tradeMarkers = ref<TradeSignalMarker[]>([])
let llmStatusTimer: ReturnType<typeof setInterval> | null = null
const MAX_CHART_POINTS = 200
const MOBILE_BREAKPOINT = 768
const isMobile = ref(window.innerWidth <= MOBILE_BREAKPOINT)
const chartExpanded = ref(!isMobile.value)

function handleResize() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
}

watch(isMobile, (mobile) => {
  if (mobile) {
    chartExpanded.value = false
  }
})

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

const realtimeStatusLabel = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected': return '实时连接正常'
    case 'reconnecting': return '实时重连中'
    case 'polling': return '轮询兜底'
    default: return '实时连接中'
  }
})

const realtimeStatusType = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected': return 'success'
    case 'reconnecting': return 'warning'
    case 'polling': return 'info'
    default: return 'info'
  }
})

const primaryPosition = computed<Position | null>(() => {
  if (account.value.positions.length === 0) return null
  return account.value.positions.find((position) => position.symbol === strategy.value.symbol) ?? account.value.positions[0]
})

const sessionOrderHint = computed(() => {
  if (status.value.trading_session_mode !== 'RTH_ONLY') {
    return '未限制 RTH（非完整交易日历）'
  }
  return status.value.is_trading_hours
    ? 'RTH 内可下单'
    : '非 RTH，新单拦截'
})

const sessionOrderDotClass = computed(() => {
  if (status.value.trading_session_mode !== 'RTH_ONLY') return 'session-dot-neutral'
  return status.value.is_trading_hours ? 'session-dot-ok' : 'session-dot-block'
})

const unrealizedPnl = computed(() => {
  const position = primaryPosition.value
  if (!position || !status.value.last_price) return 0
  const priceDelta = position.side === 'SHORT'
    ? position.avg_price - status.value.last_price
    : status.value.last_price - position.avg_price
  return priceDelta * position.quantity
})

const unrealizedPnlPct = computed(() => {
  const position = primaryPosition.value
  if (!position || position.avg_price <= 0) return 0
  const priceDelta = position.side === 'SHORT'
    ? position.avg_price - status.value.last_price
    : status.value.last_price - position.avg_price
  return (priceDelta / position.avg_price) * 100
})

const priceRangePercent = computed(() => {
  const low = strategy.value.buy_low
  const high = strategy.value.sell_high
  if (low <= 0 || high <= low) return 0
  const raw = ((status.value.last_price - low) / (high - low)) * 100
  return Math.min(100, Math.max(0, raw))
})

const priceRangeLeft = computed(() => `${priceRangePercent.value}%`)
const priceRangeWidth = computed(() => `${Math.max(4, priceRangePercent.value)}%`)

async function handleRetry() {
  loadError.value = false
  try {
    await load()
    await Promise.all([refreshAccount(), loadLLMStatus(), loadRecentOrders(), loadRecentEvents()])
    await loadStatusHistory()
  } catch {
    void 0
  }
}

async function loadLLMStatus() {
  llmStatusLoading.value = true
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    llmStatus.value = null
  } finally {
    llmStatusLoading.value = false
  }
}

async function loadRecentOrders() {
  recentOrdersLoading.value = true
  try {
    recentOrders.value = (await getOrders({ scope: 'today', page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentOrders.value = []
  } finally {
    recentOrdersLoading.value = false
  }
}

async function loadRecentEvents() {
  recentEventsLoading.value = true
  try {
    recentEvents.value = (await getTradeEvents({ page: 1, page_size: 5 })).items.slice(0, 5)
  } catch {
    recentEvents.value = []
  } finally {
    recentEventsLoading.value = false
  }
}

async function loadStatusHistory() {
  try {
    const history = await getStatusHistory(MAX_CHART_POINTS)
    chartPoints.value = history.points.slice(-MAX_CHART_POINTS)
    tradeMarkers.value = history.markers
  } catch {
    chartPoints.value = []
    tradeMarkers.value = []
  }
}

function appendStatusPoint() {
  const now = new Date().toISOString()
  const point: StatusHistoryPoint = {
    timestamp: now,
    engine_state: status.value.engine_state,
    paused: status.value.paused,
    kill_switch: status.value.kill_switch,
    daily_pnl: status.value.daily_pnl,
    consecutive_losses: status.value.consecutive_losses,
    last_price: status.value.last_price,
    last_trigger_price: status.value.last_trigger_price,
  }
  const previous = chartPoints.value[chartPoints.value.length - 1]
  if (
    previous
    && previous.last_price === point.last_price
    && previous.daily_pnl === point.daily_pnl
    && previous.engine_state === point.engine_state
  ) {
    return
  }
  chartPoints.value = [...chartPoints.value, point].slice(-MAX_CHART_POINTS)
}

onMounted(() => {
  loadLLMStatus()
  loadRecentOrders()
  loadRecentEvents()
  loadStatusHistory()
  llmStatusTimer = setInterval(() => {
    loadLLMStatus()
    loadRecentOrders()
    loadRecentEvents()
  }, 3000)
  load().catch(() => void 0)
  window.addEventListener('resize', handleResize)
})

watch(
  () => [
    status.value.last_price,
    status.value.daily_pnl,
    status.value.engine_state,
    status.value.paused,
    status.value.kill_switch,
  ],
  appendStatusPoint,
)

onUnmounted(() => {
  if (llmStatusTimer) {
    clearInterval(llmStatusTimer)
    llmStatusTimer = null
  }
  window.removeEventListener('resize', handleResize)
})

async function handleStart() {
  try {
    await startTrading()
    ElMessage.success('交易已启动')
    await refreshStatus()
  } catch {
    ElMessage.error('启动失败')
  }
}

async function handleStop() {
  try {
    await stopTrading()
    ElMessage.success('交易已停止')
    await refreshStatus()
  } catch {
    ElMessage.error('停止失败')
  }
}

async function handlePause() {
  try {
    await pauseTrading()
    ElMessage.success('交易已暂停')
    await refreshStatus()
  } catch {
    ElMessage.error('暂停失败')
  }
}

async function handleResume() {
  try {
    await resumeTrading()
    ElMessage.success('交易已恢复')
    await refreshStatus()
  } catch {
    ElMessage.error('恢复失败')
  }
}

async function handleKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要触发紧急停止吗？', '紧急停止', { type: 'warning' })
    await activateKillSwitch()
    ElMessage.success('紧急停止已触发')
    await refreshStatus()
  } catch {
    void 0
  }
}

async function handleDisableKillSwitch() {
  try {
    await disableKillSwitch()
    ElMessage.success('紧急停止已解除')
    await refreshStatus()
  } catch {
    ElMessage.error('解除失败')
  }
}

function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function signedPercent(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+${amount}%`
  if (normalized < 0) return `-${amount}%`
  return `${amount}%`
}

function pnlLabel(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return '盈利'
  if (normalized < 0) return '亏损'
  return '持平'
}

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}

function eventTagType(
  eventTypeValue: string,
  status: string,
  source?: TradeEventRecord['source'],
): string {
  if (source === 'audit') return eventTypeValue === 'KILL_SWITCH' ? 'danger' : 'info'
  if (eventTypeValue === 'LLM_ANALYSIS') return status === 'FAILED' ? 'danger' : 'primary'
  if (eventTypeValue === 'RISK_PAUSED') return 'danger'
  if (eventTypeValue === 'RISK_AUTO_RESUMED') return 'success'
  if (eventTypeValue === 'ORDER_FILLED') return 'success'
  if (eventTypeValue === 'ORDER_CANCELLED') return 'info'
  if (eventTypeValue === 'ORDER_REJECTED') return 'danger'
  if (eventTypeValue === 'ORDER_SKIPPED') return 'warning'
  return 'warning'
}
</script>

<style scoped>
.dashboard-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dashboard-alert {
  margin-bottom: 0;
}

.page-heading,
.cockpit-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-heading h3 {
  margin: 0;
}

.page-heading p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.heading-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.dashboard-cockpit {
  border: 1px solid #dfe5ee;
  border-radius: 8px;
  padding: 16px;
  background: #f7f9fc;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin: 14px 0;
}

.strip-item,
.cockpit-panel,
.detail-panel {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
}

.strip-item {
  min-height: 72px;
  padding: 10px 12px;
}

.strip-item span,
.mini-grid span,
.position-main span,
.pnl-box span,
.strategy-list span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.strip-item strong {
  display: block;
  margin-top: 4px;
  color: #172033;
  font-size: 18px;
  line-height: 1.1;
}

.strip-item small {
  display: block;
  margin-top: 4px;
  color: #7a8595;
  font-size: 12px;
}

.session-strip-hint {
  display: flex !important;
  align-items: center;
  gap: 6px;
}

.session-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.session-dot-ok {
  background: #22c55e;
}

.session-dot-block {
  background: #94a3b8;
}

.session-dot-neutral {
  background: #cbd5e1;
}

.cockpit-grid {
  display: grid;
  grid-template-columns: minmax(280px, 1.25fr) minmax(260px, 1fr) minmax(260px, 1fr) minmax(250px, .95fr);
  gap: 10px;
}

.cockpit-panel {
  min-height: 248px;
  padding: 14px;
}

.panel-heading,
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.panel-heading span,
.section-title h4,
.subsection-title {
  margin: 0;
  color: #4b5563;
  font-size: 13px;
  font-weight: 700;
}

.price-value {
  margin: 14px 0 12px;
  color: #111827;
  font-size: 44px;
  font-weight: 800;
  line-height: 1;
}

.range-line {
  position: relative;
  height: 10px;
  border-radius: 999px;
  background: #edf1f7;
  overflow: hidden;
}

.range-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: #409eff;
}

.range-marker {
  position: absolute;
  top: -3px;
  width: 4px;
  height: 16px;
  border-radius: 999px;
  background: #172033;
  transform: translateX(-50%);
}

.range-labels {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  color: #6b7280;
  font-size: 12px;
}

.mini-grid,
.position-main {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.mini-grid div,
.position-main div,
.pnl-box {
  border-radius: 6px;
  padding: 10px;
  background: #f8fafc;
}

.mini-grid strong,
.position-main strong,
.pnl-box strong {
  display: block;
  margin-top: 3px;
  color: #172033;
  font-size: 18px;
}

.action-message {
  grid-column: 1 / -1;
}

.action-message strong {
  font-size: 13px;
  line-height: 1.45;
}

.position-symbol {
  margin-top: 12px;
  color: #111827;
  font-size: 28px;
  font-weight: 800;
}

.position-main {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.pnl-box {
  margin-top: 10px;
}

.llm-decision {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 14px;
}

.llm-decision strong {
  color: #111827;
  font-size: 36px;
  line-height: 1;
}

.llm-decision span,
.llm-panel small {
  color: #6b7280;
  font-size: 12px;
}

.llm-range {
  margin: 12px 0 8px;
  color: #172033;
  font-weight: 700;
}

.llm-analysis {
  display: -webkit-box;
  min-height: 44px;
  margin: 0 0 10px;
  overflow: hidden;
  color: #4b5563;
  font-size: 13px;
  line-height: 1.55;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.llm-meta,
.llm-apply-state {
  display: grid;
  gap: 4px;
  color: #6b7280;
  font-size: 12px;
}

.llm-apply-state {
  margin-top: 8px;
}

.llm-apply-state span {
  line-height: 1.4;
}

.action-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.action-grid :deep(.el-button) {
  width: 100%;
  margin-left: 0;
}

.kill-button {
  grid-column: 1 / -1;
}

.chart-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chart-panels {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: 12px;
}

.chart-controls {
  display: flex;
  justify-content: flex-end;
}

.detail-grid {
  display: grid;
  grid-template-columns: 1.05fr 1fr 1.05fr 1.15fr;
  gap: 12px;
}

.detail-panel {
  padding: 14px;
}

.section-title h4 {
  color: #172033;
  font-size: 15px;
}

.section-title strong,
.section-title span {
  color: #172033;
  font-weight: 800;
}

.subsection-title {
  margin: 14px 0 8px;
}

.strategy-list,
.risk-list {
  display: grid;
  gap: 8px;
}

.strategy-list {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.strategy-list div {
  border-radius: 6px;
  padding: 8px;
  background: #f8fafc;
}

.strategy-list strong {
  display: block;
  margin-top: 4px;
}

.risk-list {
  color: #4b5563;
  font-size: 13px;
}

.event-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.event-row {
  border-radius: 6px;
  padding: 9px;
  background: #f8fafc;
}

.event-main {
  display: grid;
  grid-template-columns: auto minmax(80px, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.event-main strong {
  overflow: hidden;
  color: #172033;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-main small {
  color: #7a8595;
  font-size: 11px;
  white-space: nowrap;
}

.event-row p {
  display: -webkit-box;
  margin: 7px 0 0;
  overflow: hidden;
  color: #4b5563;
  font-size: 12px;
  line-height: 1.45;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.skip-category {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.empty-note {
  margin: 24px 0;
  color: #999;
  text-align: center;
}

.metric-positive {
  color: #14884f !important;
}

.metric-negative {
  color: #c43838 !important;
}

.responsive-table {
  width: 100%;
}

@media (max-width: 1280px) {
  .status-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .cockpit-grid,
  .chart-panels,
  .detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 768px) {
  .page-heading,
  .cockpit-heading {
    flex-direction: column;
    gap: 10px;
  }

  .heading-tags {
    justify-content: flex-start;
  }

  .status-strip,
  .cockpit-grid,
  .chart-panels,
  .strategy-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .status-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  .detail-grid {
    grid-template-columns: 1fr;
  }

  .dashboard-cockpit,
  .cockpit-panel {
    padding: 12px;
  }

  .strip-item {
    min-height: 64px;
    padding: 8px;
  }

  .strip-item strong {
    font-size: 16px;
  }

  .cockpit-panel {
    min-height: 210px;
  }

  .price-value {
    font-size: 36px;
  }

  .position-main {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .action-grid :deep(.el-button) {
    min-height: 44px;
    font-size: 14px;
  }

  .chart-panels {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 520px) {
  .status-strip,
  .cockpit-grid,
  .chart-panels,
  .position-main,
  .strategy-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .cockpit-grid,
  .chart-panels,
  .position-main,
  .strategy-list {
    grid-template-columns: 1fr;
  }

  .action-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .action-grid :deep(.el-button) {
    min-height: 48px;
    font-size: 15px;
  }

  .kill-button {
    min-height: 52px;
    font-size: 16px;
  }
}
</style>
