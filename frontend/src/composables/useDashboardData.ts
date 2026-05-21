import { ref, computed } from 'vue'
import { getStrategy, getStatus } from '../api'
import type { StrategyConfig, StatusData } from '../types'

const defaultStrategy: StrategyConfig = {
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, max_daily_loss: 5000, max_consecutive_losses: 3,
  llm_interval_minutes: 240,
  updated_at: '',
}

const defaultStatus: StatusData = {
  engine_state: 'flat', paused: false, kill_switch: false, runner_running: false,
  daily_pnl: 0, consecutive_losses: 0,
  last_price: 0, last_trigger_price: 0, last_trigger_at: null,
}

export function useDashboardData() {
  const strategy = ref<StrategyConfig>({ ...defaultStrategy })
  const status = ref<StatusData>({ ...defaultStatus })
  const strategyLoading = ref(true)
  const statusLoading = ref(true)
  const loadError = ref(false)
  const strategyError = ref(false)
  const statusError = ref(false)

  const initialLoading = computed(() => strategyLoading.value && statusLoading.value)

  async function loadStrategy() {
    strategyLoading.value = true
    try {
      strategy.value = await getStrategy()
      strategyError.value = false
      loadError.value = statusError.value
    } catch (e) {
      console.error('Dashboard strategy load failed:', e)
      strategyError.value = true
      loadError.value = true
      throw e
    } finally {
      strategyLoading.value = false
    }
  }

  async function loadStatus() {
    statusLoading.value = true
    try {
      status.value = await getStatus()
      statusError.value = false
      loadError.value = strategyError.value
    } catch (e) {
      console.error('Dashboard status load failed:', e)
      statusError.value = true
      loadError.value = true
      throw e
    } finally {
      statusLoading.value = false
    }
  }

  async function load() {
    await Promise.allSettled([loadStrategy(), loadStatus()])
    if (loadError.value) {
      throw new Error('Dashboard data load failed')
    }
  }

  async function refreshStatus() {
    try {
      status.value = await getStatus()
      statusError.value = false
      loadError.value = strategyError.value
    } catch {
      void 0
    }
  }

  return {
    strategy,
    status,
    initialLoading,
    strategyLoading,
    statusLoading,
    loadError,
    load,
    loadStrategy,
    loadStatus,
    refreshStatus,
  }
}
