<template>
  <div v-loading="initialLoading">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" type="primary" plain @click="refresh">重试连接</el-button>
    </el-alert>
    <el-alert v-if="accountError" type="warning" title="账户资产暂时不可用，请检查券商凭证或稍后重试" show-icon style="margin-bottom: 16px" />
    <div class="page-heading">
      <h3>仪表盘</h3>
      <el-tag :type="realtimeStatusType" effect="plain">{{ realtimeStatusLabel }}</el-tag>
    </div>
    <el-row :gutter="20">
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>引擎状态</template>
          <el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>最新价格</template>
          <h1>${{ (status.last_price ?? 0).toFixed(2) }}</h1>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="8">
        <el-card>
          <template #header>今日盈亏</template>
          <h1 :class="metricClass(status.daily_pnl)">
            <span class="metric-label">{{ pnlLabel(status.daily_pnl) }}</span>
            {{ signedCurrency(status.daily_pnl) }}
          </h1>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :md="12">
        <el-card>
          <template #header>风控状态</template>
          <p>紧急停止：<el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? '已开启' : '已关闭' }}</el-tag></p>
          <p>暂停状态：<el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? '已暂停' : '运行中' }}</el-tag></p>
          <p>连续亏损次数：{{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="12">
        <el-card>
          <template #header>操作控制</template>
          <el-space>
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch">启动</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused || status.kill_switch">暂停</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused || status.kill_switch">恢复</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">紧急停止</el-button>
            <el-button v-if="status.kill_switch" type="success" plain @click="handleDisableKillSwitch">解除紧急停止</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :lg="8">
        <el-card>
          <template #header>总资产</template>
          <h1 :class="account.available ? 'metric-positive' : 'metric-negative'">
            <span class="metric-label">{{ account.available ? '可用' : '异常' }}</span>
            ${{ account.total_assets.toFixed(2) }}
          </h1>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="16">
        <el-card>
          <template #header>现金余额</template>
          <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" class="responsive-table">
            <el-table-column prop="currency" label="币种" min-width="90" />
            <el-table-column prop="available_cash" label="可用" min-width="120">
              <template #default="{ row }">${{ row.available_cash.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column prop="frozen_cash" label="冻结" min-width="120">
              <template #default="{ row }">${{ row.frozen_cash.toFixed(2) }}</template>
            </el-table-column>
          </el-table>
          <p v-else style="color: #999; text-align: center">暂无数据</p>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 20px">
      <template #header>持仓明细</template>
      <el-table :data="account.positions" size="small" v-if="account.positions.length > 0" class="responsive-table">
        <el-table-column prop="symbol" label="股票代码" min-width="130" />
        <el-table-column prop="side" label="方向" min-width="90">
          <template #default="{ row }">{{ positionSideLabel(row.side) }}</template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" min-width="100">
          <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
        </el-table-column>
        <el-table-column prop="avg_price" label="均价" min-width="120">
          <template #default="{ row }">${{ row.avg_price.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column prop="market_value" label="市值" min-width="120">
          <template #default="{ row }">${{ row.market_value.toFixed(2) }}</template>
        </el-table-column>
      </el-table>
      <p v-else style="color: #999; text-align: center">暂无持仓</p>
    </el-card>

    <el-card style="margin-top: 20px">
      <template #header>行情信息</template>
      <p>股票代码：{{ strategy.symbol || '未配置' }}</p>
      <p>市场：{{ marketLabel(strategy.market) }}</p>
      <p>买入价下限：${{ strategy.buy_low }}</p>
      <p>卖出价上限：${{ strategy.sell_high }}</p>
      <p>做空：{{ strategy.short_selling ? '是' : '否' }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getStrategy, getStatus, pauseTrading, resumeTrading, activateKillSwitch, disableKillSwitch, startTrading, stopTrading, getAccount } from '../api'
import type { StrategyConfig, StatusData, AccountInfo } from '../types'
import { engineStateLabel, marketLabel, positionSideLabel } from '../utils/labels'

const strategy = ref<StrategyConfig>({
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  updated_at: '',
})

const status = ref<StatusData>({
  engine_state: 'flat', paused: false, kill_switch: false,
  daily_pnl: 0, consecutive_losses: 0,
  last_price: 0, last_trigger_price: 0, last_trigger_at: null,
})

const account = ref<AccountInfo>({
  total_assets: 0,
  cash_balances: [],
  positions: [],
  available: true,
  error: null,
})
const accountLoading = ref(false)
const initialLoading = ref(true)
const loadError = ref(false)
const accountError = ref(false)
const realtimeStatus = ref<'connecting' | 'connected' | 'reconnecting' | 'polling'>('connecting')

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

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let useWebSocket = false
let reconnectAttempts = 0
let accountRefreshTimer: ReturnType<typeof setInterval> | null = null
let lastWsStatusAt = 0

function connectWebSocket() {
  realtimeStatus.value = 'connecting'
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/ws`
  ws = new WebSocket(wsUrl)

  const apiKey = localStorage.getItem('api_key')
  ws.onopen = () => {
    useWebSocket = true
    realtimeStatus.value = 'connected'
    reconnectAttempts = 0
    if (apiKey) {
      ws?.send(JSON.stringify({ token: apiKey }))
    }
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data.type === 'pong') return
      if (data.state !== undefined) {
        lastWsStatusAt = Date.now()
        realtimeStatus.value = 'connected'
        status.value = {
          engine_state: data.state,
          paused: data.risks?.paused ?? status.value.paused,
          kill_switch: data.risks?.kill_switch ?? status.value.kill_switch,
          daily_pnl: data.risks?.daily_pnl ?? status.value.daily_pnl,
          consecutive_losses: data.risks?.consecutive_losses ?? status.value.consecutive_losses,
          last_price: data.last_price ?? status.value.last_price,
          last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
          last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
        }
      }
    } catch {
      // ignore parse errors
    }
  }

  ws.onclose = () => {
    useWebSocket = false
    realtimeStatus.value = 'reconnecting'
    ws = null
    scheduleReconnect()
  }

  ws.onerror = () => {
    useWebSocket = false
    realtimeStatus.value = 'polling'
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return
  realtimeStatus.value = 'reconnecting'
  const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
  reconnectAttempts++
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connectWebSocket()
  }, delay)
}

function hasFreshWebSocketStatus() {
  return useWebSocket && Date.now() - lastWsStatusAt < 10000
}

function startPolling() {
  pollTimer = setInterval(async () => {
    if (hasFreshWebSocketStatus()) return
    try {
      const st = await getStatus()
      status.value = st
      loadError.value = false
      if (!hasFreshWebSocketStatus()) {
        realtimeStatus.value = 'polling'
      }
    } catch {
      // silent — WebSocket may reconnect
    }
  }, 3000)
}

function startAccountRefresh() {
  accountRefreshTimer = setInterval(async () => {
    try {
      account.value = await getAccount()
      accountError.value = !account.value.available
    } catch {
      accountError.value = true
      // silent — account data will retry on next interval
    }
  }, 10000)
}

onMounted(async () => {
  try {
    const [s, st, acc] = await Promise.all([
      getStrategy(),
      getStatus(),
      getAccount().catch(() => {
        accountError.value = true
        return { total_assets: 0, cash_balances: [], positions: [], available: false, error: 'Account data unavailable' }
      }),
    ])
    strategy.value = s
    status.value = st
    account.value = acc
    accountError.value = !acc.available
    loadError.value = false
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
    loadError.value = true
    ElMessage.error('刷新仪表盘数据失败')
  } finally {
    initialLoading.value = false
  }
  connectWebSocket()
  startPolling()
  startAccountRefresh()
  window.addEventListener('api-key-updated', handleApiKeyUpdated)
})

onUnmounted(() => {
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (accountRefreshTimer) {
    clearInterval(accountRefreshTimer)
    accountRefreshTimer = null
  }
  window.removeEventListener('api-key-updated', handleApiKeyUpdated)
})

async function refresh() {
  try {
    const [s, st, acc] = await Promise.all([
      getStrategy(),
      getStatus(),
      getAccount().catch(() => {
        accountError.value = true
        return { total_assets: 0, cash_balances: [], positions: [], available: false, error: 'Account data unavailable' }
      }),
    ])
    strategy.value = s
    status.value = st
    account.value = acc
    accountError.value = !acc.available
    loadError.value = false
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
    loadError.value = true
  }
}

function reconnectWebSocketNow() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  useWebSocket = false
  realtimeStatus.value = 'connecting'
  lastWsStatusAt = 0
  connectWebSocket()
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
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
  return 'metric-neutral'
}

function handleApiKeyUpdated() {
  refresh()
  reconnectWebSocketNow()
}

async function handlePause() {
  try {
    await pauseTrading()
    await refresh()
  } catch (e) {
    console.error('暂停交易失败：', e)
    ElMessage.error('暂停交易失败')
  }
}

async function handleResume() {
  try {
    await resumeTrading()
    await refresh()
  } catch (e) {
    console.error('恢复交易失败：', e)
    ElMessage.error('恢复交易失败')
  }
}

async function handleKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要开启紧急停止吗？这将立即停止所有交易。', '确认', { type: 'warning' })
    await activateKillSwitch()
    await refresh()
  } catch (e) {
    if (e !== 'cancel') {
      console.error('开启紧急停止失败：', e)
      ElMessage.error('开启紧急停止失败')
    }
  }
}

async function handleDisableKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要解除紧急停止吗？请确认风险后再恢复交易。', '确认', { type: 'warning' })
    await disableKillSwitch()
    await refresh()
  } catch (e) {
    if (e !== 'cancel') {
      console.error('解除紧急停止失败：', e)
      ElMessage.error('解除紧急停止失败')
    }
  }
}

async function handleStart() {
  try {
    await startTrading()
    await refresh()
  } catch (e) {
    console.error('启动交易失败：', e)
    ElMessage.error('启动交易失败')
  }
}

async function handleStop() {
  try {
    await ElMessageBox.confirm('确定要停止交易吗？', '确认', { type: 'warning' })
    await stopTrading()
    await refresh()
  } catch (e) {
    if (e !== 'cancel') {
      console.error('停止交易失败：', e)
      ElMessage.error('停止交易失败')
    }
  }
}
</script>

<style scoped>
.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-heading h3 {
  margin: 0 0 16px;
}

.metric-positive {
  color: #1f7a3a;
}

.metric-negative {
  color: #b42318;
}

.metric-neutral {
  color: #606266;
}

.metric-label {
  display: inline-block;
  margin-right: 8px;
  font-size: 14px;
  font-weight: 600;
}

.responsive-table {
  width: 100%;
}

:deep(.el-col) {
  margin-bottom: 16px;
}
</style>
