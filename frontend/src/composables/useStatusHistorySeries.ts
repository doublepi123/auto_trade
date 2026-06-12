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
  let loadSeq = 0

  async function load(query: StatusHistoryQuery): Promise<StatusHistory> {
    const seq = ++loadSeq
    loading.value = true
    error.value = ''
    try {
      const result = await getStatusHistory(query)
      if (seq !== loadSeq) {
        return history.value
      }
      history.value = result
      return result
    } catch (err) {
      if (seq !== loadSeq) {
        throw err
      }
      history.value = { points: [], markers: [] }
      error.value = err instanceof Error ? err.message : '加载运行时状态历史失败'
      throw err
    } finally {
      if (seq === loadSeq) {
        loading.value = false
      }
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
