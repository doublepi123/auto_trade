import { ref, onMounted, onUnmounted } from 'vue'
import { getAccount } from '../api'
import type { AccountInfo } from '../types'

const defaultAccount: AccountInfo = {
  total_assets: 0,
  cash_balances: [],
  positions: [],
  available: true,
  error: null,
}

export function useAccountRefresh(intervalMs = 10000) {
  const account = ref<AccountInfo>({ ...defaultAccount })
  const accountError = ref(false)
  const loading = ref(true)
  const refreshing = ref(false)
  const hasLoaded = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let inFlight = false

  async function refresh() {
    if (inFlight) return
    inFlight = true
    refreshing.value = hasLoaded.value
    loading.value = !hasLoaded.value
    try {
      const nextAccount = await getAccount()
      account.value = nextAccount
      accountError.value = !nextAccount.available
      hasLoaded.value = true
    } catch {
      accountError.value = true
    } finally {
      loading.value = false
      refreshing.value = false
      inFlight = false
    }
  }

  onMounted(() => {
    refresh()
    timer = setInterval(refresh, intervalMs)
  })

  onUnmounted(() => {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  })

  return {
    account,
    accountError,
    loading,
    refreshing,
    hasLoaded,
    refresh,
  }
}