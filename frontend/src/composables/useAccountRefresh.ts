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

  let timer: ReturnType<typeof setInterval> | null = null

  async function refresh() {
    try {
      account.value = await getAccount()
      accountError.value = !account.value.available
    } catch {
      accountError.value = true
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
    refresh,
  }
}
