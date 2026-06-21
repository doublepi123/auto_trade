import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getMarketSession, getStrategy } from '../api'
import type { MarketSessionStatus } from '../types'

// Dismiss is tracked per phase: closing the "盘前" banner keeps it hidden while
// the market stays in pre, but a later phase change (e.g. -> 休市) resurfaces a
// fresh banner because that is a genuinely new situation worth flagging.
const DISMISS_KEY = 'auto_trade.session-banner.dismissed-phase'
const POLL_INTERVAL_MS = 60_000

export function useMarketSession() {
  const session = ref<MarketSessionStatus | null>(null)
  const dismissedPhase = ref('')
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let symbol = ''
  let symbolLoaded = false

  function readDismissed(): void {
    try {
      dismissedPhase.value = localStorage.getItem(DISMISS_KEY) ?? ''
    } catch {
      dismissedPhase.value = ''
    }
  }

  function dismiss(): void {
    const phase = session.value?.status
    if (!phase) return
    dismissedPhase.value = phase
    try {
      localStorage.setItem(DISMISS_KEY, phase)
    } catch {
      /* localStorage may be unavailable (private mode); ignore */
    }
  }

  async function ensureSymbol(): Promise<void> {
    if (symbolLoaded) return
    symbolLoaded = true
    try {
      symbol = (await getStrategy()).symbol || ''
    } catch {
      // Fall back to the backend's default market (US) for an empty symbol.
      symbol = ''
    }
  }

  async function load(): Promise<void> {
    await ensureSymbol()
    try {
      session.value = await getMarketSession(symbol)
    } catch {
      // Session is informational only; never block the UI on it.
    }
  }

  const isNonRth = computed(() => {
    const phase = session.value?.status
    return phase !== undefined && phase !== 'rth'
  })

  const showBanner = computed(
    () => isNonRth.value && session.value?.status !== dismissedPhase.value,
  )

  const phaseLabel = computed(() => {
    switch (session.value?.status) {
      case 'pre':
        return '盘前'
      case 'post':
        return '盘后'
      case 'lunch':
        return '午休'
      case 'closed':
        return '休市'
      default:
        return ''
    }
  })

  onMounted(() => {
    readDismissed()
    load()
    pollTimer = setInterval(load, POLL_INTERVAL_MS)
  })

  onUnmounted(() => {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  })

  return { session, isNonRth, showBanner, phaseLabel, dismiss, refresh: load }
}
