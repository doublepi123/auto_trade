<template>
  <div v-loading="initialLoading">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon style="margin-bottom: 16px" />
    <h3>仪表盘</h3>

    <el-row :gutter="20">
      <el-col :xs="24" :sm="12" :md="8">
        <el-card>
          <template #header>连接状态</template>
          <p>连接方式：<el-tag :type="connectionTagType">{{ connectionModeLabel(connectionMode) }}</el-tag></p>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="8">
        <el-card>
          <template #header>策略状态</template>
          <p>引擎状态：<el-tag :type="stateTagType">{{ engineStateLabel(status.engine_state) }}</el-tag></p>
          <p>最新价格：${{ (status.last_price ?? 0).toFixed(2) }}</p>
          <p :style="{ color: status.daily_pnl >= 0 ? 'green' : 'red' }">今日盈亏：${{ (status.daily_pnl ?? 0).toFixed(2) }}</p>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="24" :md="8">
        <el-card v-loading="accountLoading">
          <template #header>账户摘要</template>
          <template v-if="account">
            <h1 :style="{ color: account.total_assets >= 0 ? 'green' : 'red' }">
              ${{ account.total_assets.toFixed(2) }}
            </h1>
          </template>
          <div v-else style="text-align: center">
            <p style="color: #999">账户数据暂不可用</p>
            <el-button size="small" @click="refreshAccount">重试</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :sm="12" :md="12">
        <el-card>
          <template #header>风控状态</template>
          <p>紧急停止：<el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? '已开启' : '已关闭' }}</el-tag></p>
          <p>暂停状态：<el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? '已暂停' : '运行中' }}</el-tag></p>
          <p>连续亏损次数：{{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="12">
        <el-card>
          <template #header>操作控制</template>
          <el-space wrap>
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch">启动</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused || status.kill_switch">暂停</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused || status.kill_switch">恢复</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">紧急停止</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :xs="24" :sm="24" :md="12">
        <el-card v-loading="accountLoading">
          <template #header>现金余额</template>
          <template v-if="account">
            <el-table :data="account.cash_balances" size="small" v-if="account.cash_balances.length > 0" style="width: 100%">
              <el-table-column prop="currency" label="币种" width="100" />
              <el-table-column prop="available_cash" label="可用" width="150">
                <template #default="{ row }">${{ row.available_cash.toFixed(2) }}</template>
              </el-table-column>
              <el-table-column prop="frozen_cash" label="冻结" width="150">
                <template #default="{ row }">${{ row.frozen_cash.toFixed(2) }}</template>
              </el-table-column>
            </el-table>
            <p v-else style="color: #999; text-align: center">暂无数据</p>
          </template>
          <div v-else style="text-align: center">
            <p style="color: #999">账户数据暂不可用</p>
            <el-button size="small" @click="refreshAccount">重试</el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="24" :md="12">
        <el-card>
          <template #header>行情信息</template>
          <p>股票代码：{{ strategy.symbol || '未配置' }}</p>
          <p>市场：{{ marketLabel(strategy.market) }}</p>
          <p>买入价下限：${{ strategy.buy_low }}</p>
          <p>卖出价上限：${{ strategy.sell_high }}</p>
          <p>做空：{{ strategy.short_selling ? '是' : '否' }}</p>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-loading="accountLoading" style="margin-top: 20px">
      <template #header>持仓明细</template>
      <template v-if="account">
        <el-table :data="account.positions" size="small" v-if="account.positions.length > 0" style="width: 100%">
          <el-table-column prop="symbol" label="股票代码" width="150" />
          <el-table-column prop="side" label="方向" width="100">
            <template #default="{ row }">{{ positionSideLabel(row.side) }}</template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" width="120">
            <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
          </el-table-column>
          <el-table-column prop="avg_price" label="均价" width="150">
            <template #default="{ row }">${{ row.avg_price.toFixed(2) }}</template>
          </el-table-column>
          <el-table-column prop="market_value" label="市值" width="150">
            <template #default="{ row }">${{ row.market_value.toFixed(2) }}</template>
          </el-table-column>
        </el-table>
        <p v-else style="color: #999; text-align: center">暂无持仓</p>
      </template>
      <div v-else style="text-align: center">
        <p style="color: #999">账户数据暂不可用</p>
        <el-button size="small" @click="refreshAccount">重试</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { pauseTrading, resumeTrading, activateKillSwitch, startTrading, stopTrading } from '../api'
import { useDashboardData } from '../composables/useDashboardData'
import { useStatusStream, type ConnectionMode } from '../composables/useStatusStream'
import { engineStateLabel, marketLabel, positionSideLabel } from '../utils/labels'

const {
  strategy,
  status,
  account,
  initialLoading,
  loadError,
  accountLoading,
  loadInitial,
  refresh,
  refreshAccount,
  startAccountRefresh,
  stopAccountRefresh,
} = useDashboardData()

const { connectionMode, connectWebSocket, startPolling, stop: stopStatusStream } = useStatusStream(status)

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

const connectionTagType = computed(() => {
  switch (connectionMode.value) {
    case 'websocket': return 'success'
    case 'polling': return 'warning'
    case 'connecting': return 'info'
    default: return 'danger'
  }
})

function connectionModeLabel(mode: ConnectionMode): string {
  switch (mode) {
    case 'connecting': return '连接中'
    case 'websocket': return 'WebSocket'
    case 'polling': return '轮询'
    case 'disconnected': return '已断开'
  }
}

onMounted(async () => {
  await loadInitial()
  if (loadError.value) {
    ElMessage.error('刷新仪表盘数据失败')
  }
  connectWebSocket()
  startPolling()
  startAccountRefresh()
})

onUnmounted(() => {
  stopStatusStream()
  stopAccountRefresh()
})

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
</script>
