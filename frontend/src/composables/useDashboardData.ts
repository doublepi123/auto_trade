import { ref } from 'vue'
import { getAccount, getStatus, getStrategy } from '../api'
import type { AccountInfo, StatusData, StrategyConfig } from '../types'

const defaultStrategy: StrategyConfig = {
  id: 0,
  symbol: '',
  market: 'US',
  buy_low: 0,
  sell_high: 0,
  short_selling: false,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
  updated_at: '',
}

const defaultStatus: StatusData = {
  engine_state: 'flat',
  paused: false,
  kill_switch: false,
  daily_pnl: 0,
  consecutive_losses: 0,
  last_price: 0,
  last_trigger_price: 0,
  last_trigger_at: null,
}

export function useDashboardData() {
  const strategy = ref<StrategyConfig>({ ...defaultStrategy })
  const status = ref<StatusData>({ ...defaultStatus })
  const account = ref<AccountInfo | null>(null)
  const initialLoading = ref(true)
  const loadError = ref(false)
  const accountLoading = ref(false)
  const accountError = ref(false)

  let accountRefreshTimer: ReturnType<typeof setInterval> | null = null

  async function refreshAccount() {
    accountLoading.value = true
    accountError.value = false
    try {
      account.value = await getAccount()
    } catch (e) {
      console.error('刷新账户数据失败：', e)
      account.value = null
      accountError.value = true
    } finally {
      accountLoading.value = false
    }
  }

  async function loadInitial() {
    initialLoading.value = true
    loadError.value = false
    accountError.value = false
    try {
      const [s, st, acc] = await Promise.all([
        getStrategy(),
        getStatus(),
        getAccount().catch((e) => {
          console.error('刷新账户数据失败：', e)
          accountError.value = true
          return null
        }),
      ])
      strategy.value = s
      status.value = st
      account.value = acc
    } catch (e) {
      console.error('刷新仪表盘失败：', e)
      loadError.value = true
    } finally {
      initialLoading.value = false
    }
  }

  async function refresh() {
    try {
      const [s, st] = await Promise.all([getStrategy(), getStatus()])
      strategy.value = s
      status.value = st
    } catch (e) {
      console.error('刷新仪表盘失败：', e)
    }
    await refreshAccount()
  }

  function startAccountRefresh() {
    if (accountRefreshTimer) return
    accountRefreshTimer = setInterval(refreshAccount, 10000)
  }

  function stopAccountRefresh() {
    if (accountRefreshTimer) {
      clearInterval(accountRefreshTimer)
      accountRefreshTimer = null
    }
  }

  return {
    strategy,
    status,
    account,
    initialLoading,
    loadError,
    accountLoading,
    accountError,
    loadInitial,
    refresh,
    refreshAccount,
    startAccountRefresh,
    stopAccountRefresh,
  }
}
