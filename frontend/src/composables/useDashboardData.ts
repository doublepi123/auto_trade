import { ref } from 'vue'
import { getStrategy, getStatus } from '../api'
import type { StrategyConfig } from '../types'
import { PROMISE_STATUS } from '../utils/constants'
import { useConnectionHealth } from './useConnectionHealth'

const defaultStrategy: StrategyConfig = {
  id: 0, symbol: '', market: 'US', buy_low: 0, sell_high: 0,
  short_selling: false, min_profit_amount: 0, auto_resume_minutes: 3,
  max_daily_loss: 5000, max_consecutive_losses: 3,
  llm_interval_minutes: 2,
  fee_rate_us: 0.0005,
  fee_rate_hk: 0.003,
  min_repricing_pct: 0.003,
  llm_action_cooldown_seconds: 60,
  trading_session_mode: 'ANY',
  margin_safety_factor: 0.9,
  allow_position_addons: false,
  max_position_quantity: 100,
  max_position_notional: 5000,
  max_risk_per_trade: 250,
  stop_loss_pct: 1,
  max_holding_minutes: 60,
  entry_cutoff_minutes_before_close: 45,
  flatten_minutes_before_close: 15,
  llm_order_execution_enabled: false,
  report_schedule_enabled: false,
  report_schedule_interval_hours: 24,
  report_schedule_symbol: '',
  updated_at: '',
}

export function useDashboardData() {
  // `status` is the app-wide singleton owned by useConnectionHealth so the
  // global health badge and the Dashboard cockpit always agree, and a REST
  // refresh here marks the shared stream fresh.
  const { status, markFresh } = useConnectionHealth()
  const strategy = ref<StrategyConfig>({ ...defaultStrategy })
  const initialLoading = ref(true)
  const loadError = ref(false)
  const strategyLoading = ref(true)
  const statusLoading = ref(true)
  let statusRefreshInFlight = false

  async function load() {
    initialLoading.value = true
    loadError.value = false
    strategyLoading.value = true
    statusLoading.value = true
    const strategyPromise = getStrategy()
      .then((s) => { strategy.value = s })
      .finally(() => { strategyLoading.value = false })
    const statusPromise = getStatus()
      .then((st) => { status.value = st; markFresh() })
      .finally(() => { statusLoading.value = false })

    const results = await Promise.allSettled([strategyPromise, statusPromise])
    initialLoading.value = false
    if (results.some((result) => result.status === PROMISE_STATUS.REJECTED)) {
      loadError.value = true
      throw new Error('Dashboard data load failed')
    }
  }

  async function refreshStatus() {
    if (statusRefreshInFlight) return
    statusRefreshInFlight = true
    statusLoading.value = true
    try {
      status.value = await getStatus()
      markFresh()
      loadError.value = false
    } catch {
      loadError.value = true
    } finally {
      statusRefreshInFlight = false
      statusLoading.value = false
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
    refreshStatus,
  }
}
