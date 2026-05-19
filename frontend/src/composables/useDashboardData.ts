import { ref } from 'vue'
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
  const initialLoading = ref(true)
  const loadError = ref(false)

  async function load() {
    try {
      const [s, st] = await Promise.all([getStrategy(), getStatus()])
      strategy.value = s
      status.value = st
      loadError.value = false
    } catch (e) {
      console.error('Dashboard data load failed:', e)
      loadError.value = true
      throw e
    } finally {
      initialLoading.value = false
    }
  }

  async function refreshStatus() {
    try {
      status.value = await getStatus()
      loadError.value = false
    } catch {
      void 0
    }
  }

  return {
    strategy,
    status,
    initialLoading,
    loadError,
    load,
    refreshStatus,
  }
}
