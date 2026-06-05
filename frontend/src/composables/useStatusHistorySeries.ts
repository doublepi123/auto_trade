import { ref } from 'vue'
import { getStatusHistory } from '../api'
import type { StatusHistory } from '../types'

export interface StatusHistoryQuery {
  symbol?: string
  from?: string
  to?: string
  limit?: number
}

export function useStatusHistorySeries() {
  const history = ref<StatusHistory>({ points: [], markers: [] })
  const loading = ref(false)
  const error = ref('')

  async function load(query: StatusHistoryQuery) {
    loading.value = true
    error.value = ''
    try {
      history.value = await getStatusHistory(query)
      return history.value
    } catch (err) {
      history.value = { points: [], markers: [] }
      error.value = err instanceof Error ? err.message : '加载运行时状态历史失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  function reset() {
    history.value = { points: [], markers: [] }
    error.value = ''
  }

  return {
    history,
    loading,
    error,
    load,
    reset,
  }
}
