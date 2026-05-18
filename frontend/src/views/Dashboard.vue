<template>
  <div v-loading="initialLoading">
    <el-alert v-if="loadError" type="error" title="无法连接服务器，请检查网络和 API 密钥" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" type="primary" plain @click="handleRetry">重试连接</el-button>
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
          <p style="margin-top: 12px">
            运行器：<el-tag :type="status.runner_running ? 'success' : 'info'">{{ status.runner_running ? '运行中' : '未启动' }}</el-tag>
          </p>
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
            <el-button type="primary" @click="handleStart" :disabled="status.kill_switch || status.runner_running">启动</el-button>
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
          <p v-else-if="!account.available" style="color: #999; text-align: center">数据不可用</p>
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
      <p v-else-if="!account.available" style="color: #999; text-align: center">数据不可用</p>
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
import { computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useDashboardData } from '../composables/useDashboardData'
import { useStatusStream } from '../composables/useStatusStream'
import { useAccountRefresh } from '../composables/useAccountRefresh'
import { startTrading, stopTrading, pauseTrading, resumeTrading, activateKillSwitch, disableKillSwitch } from '../api'
import { engineStateLabel, marketLabel, positionSideLabel } from '../utils/labels'

const { strategy, status, initialLoading, loadError, load, refreshStatus } = useDashboardData()
const { realtimeStatus, reconnectNow } = useStatusStream(status)
const { account, accountError, refresh: refreshAccount } = useAccountRefresh()

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

async function handleRetry() {
  loadError.value = false
  try {
    await load()
    await refreshAccount()
  } catch {
    void 0
  }
}

async function handleStart() {
  try {
    await startTrading()
    ElMessage.success('交易已启动')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('启动失败')
  }
}

async function handleStop() {
  try {
    await stopTrading()
    ElMessage.success('交易已停止')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('停止失败')
  }
}

async function handlePause() {
  try {
    await pauseTrading()
    ElMessage.success('交易已暂停')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('暂停失败')
  }
}

async function handleResume() {
  try {
    await resumeTrading()
    ElMessage.success('交易已恢复')
    await refreshStatus()
  } catch (e) {
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
  } catch (e) {
    ElMessage.error('解除失败')
  }
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
  return ''
}
</script>
