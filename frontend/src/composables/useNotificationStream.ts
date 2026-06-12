import { ref, onMounted, onUnmounted } from 'vue'
import { ElNotification, ElMessage } from 'element-plus'
import { api } from '../api/client'

export type NotificationSeverity = 'INFO' | 'WARNING' | 'CRITICAL'

interface NotificationEvent {
  type: string
  severity: NotificationSeverity
  action?: string
  event_type?: string
  payload?: Record<string, unknown>
  detail?: Record<string, unknown>
  message?: string
}

interface UserPreferences {
  soundEnabled: boolean
  criticalPersistMaxPerMinute: number
}

interface EventItem {
  id?: number
  source?: 'trade' | 'audit'
  event_type?: string
  action?: string
  severity?: string
  payload?: Record<string, unknown>
  detail?: Record<string, unknown>
  message?: string
  created_at?: string
}

const STORAGE_KEY = 'notification.preferences.v1'
const THROTTLE_MS = 1000
const POLL_INTERVAL_MS = 10000
const PERSISTENT_LIMIT_WINDOW_MS = 60_000

function loadPreferences(): UserPreferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaultPreferences()
    return { ...defaultPreferences(), ...JSON.parse(raw) }
  } catch {
    return defaultPreferences()
  }
}

function defaultPreferences(): UserPreferences {
  return {
    soundEnabled: true,
    criticalPersistMaxPerMinute: 5,
  }
}

function savePreferences(prefs: UserPreferences) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs))
}

function detailHash(detail: unknown): string {
  return JSON.stringify(detail ?? {})
}

function emitBySeverity(severity: NotificationSeverity, title: string, message: string) {
  if (severity === 'CRITICAL') {
    ElNotification({
      title: `🚨 ${title}`,
      message,
      type: 'error',
      position: 'top-right',
      duration: 0,
      customClass: 'notification-critical',
    })
  } else if (severity === 'WARNING') {
    ElNotification({
      title: `⚠️ ${title}`,
      message,
      type: 'warning',
      position: 'bottom-right',
      duration: 4000,
    })
  } else {
    ElMessage({
      message: `${title}: ${message}`,
      type: 'info',
      duration: 2000,
    })
  }
}

function parseSeverity(raw: string | undefined): NotificationSeverity {
  const s = (raw ?? 'INFO').toUpperCase()
  if (s === 'CRITICAL' || s === 'ERROR') return 'CRITICAL'
  if (s === 'WARNING' || s === 'WARN') return 'WARNING'
  return 'INFO'
}

// --- Sound playback (gated by soundEnabled) ---
let _audioCtx: AudioContext | null = null
function getAudioContext(): AudioContext {
  if (!_audioCtx) {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    _audioCtx = new AudioCtx()
  }
  return _audioCtx
}
function playNotificationSound(severity: NotificationSeverity) {
  if (!sharedPrefs.value.soundEnabled) return
  try {
    const ctx = getAudioContext()
    if (ctx.state === 'suspended') {
      ctx.resume()
    }
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.connect(gain)
    gain.connect(ctx.destination)
    osc.frequency.value = severity === 'CRITICAL' ? 880 : severity === 'WARNING' ? 660 : 440
    osc.type = 'sine'
    gain.gain.value = 0.3
    osc.start()
    osc.stop(ctx.currentTime + 0.15)
  } catch {
    // Audio not available
  }
}

// --- Module-level shared state (singleton) ---
const sharedPrefs = ref<UserPreferences>(loadPreferences())
const sharedLastEmittedAt = new Map<string, number>()
const sharedCriticalCount = { count: 0, windowStart: Date.now() }
const sharedKnownEventIds = new Map<string, number>() // `${source}:${id}` -> timestamp
let sharedPollTimer: ReturnType<typeof setInterval> | null = null
let sharedEnabled = false
let refCount = 0

function eventDetail(evt: { payload?: Record<string, unknown>; detail?: Record<string, unknown> }): unknown {
  return evt.payload ?? evt.detail
}

function handleEvent(evt: NotificationEvent) {
  const severity = parseSeverity(evt.severity)
  const title = evt.event_type ?? 'Notification'
  const message = evt.message ?? detailHash(eventDetail(evt))

  // CRITICAL persistent limit
  if (severity === 'CRITICAL') {
    const now = Date.now()
    if (now - sharedCriticalCount.windowStart > PERSISTENT_LIMIT_WINDOW_MS) {
      sharedCriticalCount.count = 0
      sharedCriticalCount.windowStart = now
    }
    if (sharedCriticalCount.count >= sharedPrefs.value.criticalPersistMaxPerMinute) {
      return
    }
    sharedCriticalCount.count += 1
  }

  // Throttle
  const key = `${evt.type}:${title}:${message}`
  const now = Date.now()
  const last = sharedLastEmittedAt.get(key) ?? 0
  if (now - last < THROTTLE_MS) {
    return
  }
  // Re-insert to maintain true LRU insertion order
  sharedLastEmittedAt.delete(key)
  sharedLastEmittedAt.set(key, now)
  if (sharedLastEmittedAt.size > 1000) {
    const entries = [...sharedLastEmittedAt.entries()]
    for (let i = 0; i < entries.length - 500; i++) {
      sharedLastEmittedAt.delete(entries[i][0])
    }
  }

  emitBySeverity(severity, title, message)
  playNotificationSound(severity)
}

async function fetchRecentEvents(limit = 20): Promise<EventItem[]> {
  try {
    const resp = await api.get('/api/events', { params: { source: 'all', limit } })
    const data = resp.data
    return data.items ?? data ?? []
  } catch {
    return []
  }
}

function processEvents(items: EventItem[], isBackfill = false) {
  const now = Date.now()
  for (const item of items) {
    const id = item.id ?? 0
    const dedupKey = `${item.source ?? 'trade'}:${id}`
    if (id && sharedKnownEventIds.has(dedupKey)) continue
    if (id) {
      sharedKnownEventIds.set(dedupKey, now)
      if (sharedKnownEventIds.size > 1000) {
        // Evict oldest entries by timestamp (LRU-style)
        const entries = [...sharedKnownEventIds.entries()].sort((a, b) => a[1] - b[1])
        const toRemove = entries.slice(0, entries.length - 1000)
        for (const [oldKey] of toRemove) {
          sharedKnownEventIds.delete(oldKey)
        }
      }
    }

    const severity = parseSeverity(item.severity)
    const title = item.event_type ?? 'Notification'
    const message = item.message ?? detailHash(eventDetail(item))

    if (isBackfill) {
      // Backfill only marks events as known — no notification display
      continue
    } else {
      handleEvent({
        type: item.event_type ? 'trade_event' : 'audit_log',
        severity,
        action: item.action,
        event_type: item.event_type,
        payload: item.payload,
        detail: item.detail,
        message,
      })
    }
  }
}

async function poll() {
  const items = await fetchRecentEvents(10)
  processEvents(items)
}

async function backfill(limit = 20) {
  const items = await fetchRecentEvents(limit)
  processEvents(items, true)
}

function _enable() {
  if (sharedEnabled) return
  sharedEnabled = true
  // Immediate backfill on enable
  backfill()
  // Start polling
  sharedPollTimer = setInterval(poll, POLL_INTERVAL_MS)
}

function _disable() {
  sharedEnabled = false
  if (sharedPollTimer) {
    clearInterval(sharedPollTimer)
    sharedPollTimer = null
  }
}

export function useNotificationStream() {
  onMounted(() => {
    refCount++
    if (refCount === 1) {
      _enable()
    }
  })

  // enable() kept as a no-op for backward compatibility;
  // lifecycle is now automatic via refCount transitions.
  function enable() {
    // Auto-managed — no-op
  }

  // disable() kept for backward compatibility;
  // lifecycle is now automatic via refCount transitions.
  function disable() {
    // Auto-managed — no-op
  }

  function updatePreferences(patch: Partial<UserPreferences>) {
    sharedPrefs.value = { ...sharedPrefs.value, ...patch }
    savePreferences(sharedPrefs.value)
  }

  onUnmounted(() => {
    refCount = Math.max(0, refCount - 1)
    if (refCount <= 0) {
      _disable()
      _audioCtx?.close()
      _audioCtx = null
      refCount = 0
    }
  })

  return { enable, disable, prefs: sharedPrefs, updatePreferences, backfill }
}
