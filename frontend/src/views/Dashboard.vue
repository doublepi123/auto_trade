<template>
  <div>
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
            <el-button type="primary" @click="handleStart">启动</el-button>
            <el-button type="danger" @click="handleStop">停止</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused">暂停</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused">恢复</el-button>
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
import { ElMessage } from 'element-plus'
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

const stateTagType = computed(() => {
  switch (status.value.engine_state) {
    case 'long': return 'success'
    case 'short': return 'danger'
    default: return 'info'
  }
})

let timer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await refresh()
  timer = setInterval(refresh, 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function refresh() {
  try {
    const [s, st] = await Promise.all([getStrategy(), getStatus()])
    strategy.value = s
    status.value = st
  } catch (e) {
    console.error('刷新仪表盘失败：', e)
    ElMessage.error('刷新仪表盘数据失败')
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
    await activateKillSwitch()
    await refresh()
  } catch (e: any) {
    console.error('开启紧急停止失败：', e)
    ElMessage.error('开启紧急停止失败')
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
    await stopTrading()
    await refresh()
  } catch (e: any) {
    console.error('停止交易失败：', e)
    ElMessage.error('停止交易失败')
  }
}
</script>
