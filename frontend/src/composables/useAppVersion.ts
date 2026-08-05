import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fetchAppBuildId } from '../utils/appVersion'

const VERSION_CHECK_INTERVAL_MS = 5 * 60 * 1000

type UpdateReason = 'VERSION_CHANGED' | 'PRELOAD_ERROR'

export function useAppVersion() {
  const updateReason = ref<UpdateReason | null>(null)
  const checking = ref(false)
  let pollTimer: ReturnType<typeof setInterval> | null = null

  const updateAvailable = computed(() => updateReason.value !== null)
  const updateTitle = computed(() => (
    updateReason.value === 'PRELOAD_ERROR'
      ? '页面资源已更新，当前版本无法继续加载'
      : '检测到新的前端版本'
  ))

  function markUpdateAvailable(reason: UpdateReason): void {
    if (updateReason.value === null || reason === 'PRELOAD_ERROR') {
      updateReason.value = reason
    }
  }

  async function checkVersion(): Promise<void> {
    if (checking.value || updateAvailable.value) return
    checking.value = true
    try {
      const deployedBuildId = await fetchAppBuildId()
      if (
        deployedBuildId !== null
        && deployedBuildId !== __AUTO_TRADE_BUILD_ID__
      ) {
        markUpdateAvailable('VERSION_CHANGED')
      }
    } finally {
      checking.value = false
    }
  }

  function handleFocus(): void {
    void checkVersion()
  }

  function handleVisibilityChange(): void {
    if (document.visibilityState === 'visible') {
      void checkVersion()
    }
  }

  function handlePreloadError(event: Event): void {
    event.preventDefault()
    markUpdateAvailable('PRELOAD_ERROR')
  }

  onMounted(() => {
    void checkVersion()
    pollTimer = setInterval(() => {
      void checkVersion()
    }, VERSION_CHECK_INTERVAL_MS)
    window.addEventListener('focus', handleFocus)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('vite:preloadError', handlePreloadError)
  })

  onUnmounted(() => {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    window.removeEventListener('focus', handleFocus)
    document.removeEventListener('visibilitychange', handleVisibilityChange)
    window.removeEventListener('vite:preloadError', handlePreloadError)
  })

  return {
    currentBuildId: __AUTO_TRADE_BUILD_ID__,
    updateAvailable,
    updateReason,
    updateTitle,
    checkVersion,
  }
}
