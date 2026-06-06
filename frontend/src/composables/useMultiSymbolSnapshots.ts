import { onBeforeUnmount, ref } from 'vue'
import { getWatchlistSnapshots } from '../api/watchlist'
import type { WatchlistSnapshot } from '../types'

export function useMultiSymbolSnapshots() {
  const snapshots = ref<WatchlistSnapshot[]>([])
  const loading = ref(false)
  const error = ref('')
  let timer: number | null = null

  async function refresh() {
    loading.value = true
    error.value = ''
    try {
      snapshots.value = await getWatchlistSnapshots()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '加载多标的快照失败'
    } finally {
      loading.value = false
    }
  }

  function start(intervalMs = 15000) {
    void refresh()
    if (timer !== null) return
    timer = window.setInterval(() => {
      void refresh()
    }, intervalMs)
  }

  function stop() {
    if (timer === null) return
    window.clearInterval(timer)
    timer = null
  }

  onBeforeUnmount(stop)
  return {
    snapshots,
    loading,
    error,
    refresh,
    start,
    stop,
  }
}
