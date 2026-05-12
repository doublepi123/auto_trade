<template>
  <div>
    <h3>Dashboard</h3>
    <el-row :gutter="20">
      <el-col :span="8">
        <el-card>
          <template #header>Engine State</template>
          <el-tag :type="stateTagType">{{ status.engine_state.toUpperCase() }}</el-tag>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>Last Price</template>
          <h1>${{ status.last_price.toFixed(2) }}</h1>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card>
          <template #header>Daily P&L</template>
          <h1 :style="{ color: status.daily_pnl >= 0 ? 'green' : 'red' }">
            ${{ status.daily_pnl.toFixed(2) }}
          </h1>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px">
      <el-col :span="12">
        <el-card>
          <template #header>Risk Status</template>
          <p>Kill Switch: <el-tag :type="status.kill_switch ? 'danger' : 'success'">{{ status.kill_switch ? 'ON' : 'OFF' }}</el-tag></p>
          <p>Paused: <el-tag :type="status.paused ? 'warning' : 'success'">{{ status.paused ? 'YES' : 'NO' }}</el-tag></p>
          <p>Consecutive Losses: {{ status.consecutive_losses }}</p>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Controls</template>
          <el-space>
            <el-button type="primary" @click="handleStart">Start</el-button>
            <el-button type="danger" @click="handleStop">Stop</el-button>
            <el-button type="warning" @click="handlePause" :disabled="status.paused">Pause</el-button>
            <el-button type="success" @click="handleResume" :disabled="!status.paused">Resume</el-button>
            <el-button type="danger" plain @click="handleKillSwitch">Kill Switch</el-button>
          </el-space>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top: 20px">
      <template #header>Quote Info</template>
      <p>Symbol: {{ strategy.symbol || 'Not configured' }}</p>
      <p>Market: {{ strategy.market }}</p>
      <p>Buy Low: ${{ strategy.buy_low }}</p>
      <p>Sell High: ${{ strategy.sell_high }}</p>
      <p>Short Selling: {{ strategy.short_selling ? 'Yes' : 'No' }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getStrategy, getStatus, pauseTrading, resumeTrading, activateKillSwitch, startTrading, stopTrading } from '../api'
import type { StrategyConfig, StatusData } from '../types'

const strategy = ref<StrategyConfig>({
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  sct_key: '', updated_at: '',
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
    console.error('Failed to refresh:', e)
  }
}

async function handlePause() {
  await pauseTrading()
  await refresh()
}

async function handleResume() {
  await resumeTrading()
  await refresh()
}

async function handleKillSwitch() {
  await activateKillSwitch()
  await refresh()
}

async function handleStart() {
  await startTrading()
  await refresh()
}

async function handleStop() {
  await stopTrading()
  await refresh()
}
</script>
