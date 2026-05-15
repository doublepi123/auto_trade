<template>
  <div v-loading="initialLoading">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon style="margin-bottom: 16px" />
    <h3>仪表盘</h3>
    <el-row :gutter="20">
      <el-col :span="8">
        <el-card>
          <template #header>引擎状态</template>
          <el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>最新价格</template>
          <h1>${{ (status.last_price ?? 0).toFixed(2) }}</h1>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>今日盈亏</template>
          <h1 :style="{ color: status.daily_pnl >= 0 ? 'green' : 'red' }">
            ${{ (status.daily_pnl ?? 0).toFixed(2) }}
          </h1>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :span="12">
        <el-card>
          <template #header>风控状态</template>
          <p>紧急停止：<el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? '已开启' : '已关闭' }}</el-tag></p>
          <p>暂停状态：<el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? '已暂停' : '运行中' }}</el-tag></p>
          <p>连续亏损次数：{{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>操作控制</template>
          <el-space>
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch">启动</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused || status.kill_switch">暂停</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused || status.kill_switch">恢复</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">紧急停止</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

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
import { getStrategy, getStatus, pauseTrading, resumeTrading, activateKillSwitch, startTrading, stopTrading } from '../api'
import type { StrategyConfig, StatusData } from '../types'
import { engineStateLabel, marketLabel } from '../utils/labels'

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

const initialLoading = ref(true)
const loadError = ref(false)

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let useWebSocket = false
let reconnectAttempts = 0

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/ws`
  ws = new WebSocket(wsUrl)

  const apiKey = localStorage.getItem('api_key')
  ws.onopen = () => {
    useWebSocket = true
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
    ws = null
    scheduleReconnect()
  }

  ws.onerror = () => {
    useWebSocket = false
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return
  const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
  reconnectAttempts++
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connectWebSocket()
  }, delay)
}

function startPolling() {
  pollTimer = setInterval(async () => {
    if (useWebSocket) return
    try {
      const st = await getStatus()
      status.value = st
    } catch {
      // silent — WebSocket may reconnect
    }
  }, 3000)
}

onMounted(async () => {
  try {
    const [s, st] = await Promise.all([getStrategy(), getStatus()])
    strategy.value = s
    status.value = st
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
    loadError.value = true
    ElMessage.error('刷新仪表盘数据失败')
  } finally {
    initialLoading.value = false
  }
  connectWebSocket()
  startPolling()
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
})

async function refresh() {
  try {
    const [s, st] = await Promise.all([getStrategy(), getStatus()])
    strategy.value = s
    status.value = st
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
  }
}

async function handlePause() {
  try {
    await pauseTrading()
    await refresh()
  } catch (e: any) {
    console.error('暂停交易失败：', e)
    ElMessage.error('暂停交易失败')
  }
}

async function handleResume() {
  try {
    await resumeTrading()
    await refresh()
  } catch (e: any) {
    console.error('恢复交易失败：', e)
    ElMessage.error('恢复交易失败')
  }
}

async function handleKillSwitch() {
  try {
    await ElMessageBox.confirm('确定要开启紧急停止吗？这将立即停止所有交易。', '确认', { type: 'warning' })
    await activateKillSwitch()
    await refresh()
  } catch (e: any) {
    if (e !== 'cancel') {
      console.error('开启紧急停止失败：', e)
      ElMessage.error('开启紧急停止失败')
    }
  }
}

async function handleStart() {
  try {
    await startTrading()
    await refresh()
  } catch (e: any) {
    console.error('启动交易失败：', e)
    ElMessage.error('启动交易失败')
  }
}

async function handleStop() {
  try {
    await ElMessageBox.confirm('确定要停止交易吗？', '确认', { type: 'warning' })
    await stopTrading()
    await refresh()
  } catch (e: any) {
    if (e !== 'cancel') {
      console.error('停止交易失败：', e)
      ElMessage.error('停止交易失败')
    }
  }
}
