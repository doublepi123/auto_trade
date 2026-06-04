import { ref, onUnmounted } from 'vue'
import { ElNotification, ElMessage } from 'element-plus'

export type NotificationSeverity = 'INFO' | 'WARNING' | 'CRITICAL'

interface NotificationEvent {
  type: string
  severity: NotificationSeverity
  action?: string
  event_type?: string
  detail?: Record<string, unknown>
}

interface UserPreferences {
  soundEnabled: boolean
  criticalPersistMaxPerMinute: number
}

interface EventItem {
  id?: number
  event_type?: string
  action?: string
  severity?: string
  detail?: Record<string, unknown>
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

function eventKey(evt: EventItem): string {
  return `${evt.event_type ?? evt.action ?? 'unknown'}:${detailHash(evt.detail)}`
}

export function useNotificationStream() {
  const prefs = ref<UserPreferences>(loadPreferences())
  const lastEmittedAt = new Map<string, number>()
  const criticalCount = { count: 0, windowStart: Date.now() }
  const knownEventIds = new Set<number>()
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let enabled = false

  function handleEvent(evt: NotificationEvent) {
    const severity = parseSeverity(evt.severity)
    const title = evt.action ?? evt.event_type ?? 'Notification'
    const message = detailHash(evt.detail)

    // CRITICAL persistent limit
    if (severity === 'CRITICAL') {
      const now = Date.now()
      if (now - criticalCount.windowStart > PERSISTENT_LIMIT_WINDOW_MS) {
        criticalCount.count = 0
        criticalCount.windowStart = now
      }
      if (criticalCount.count >= prefs.value.criticalPersistMaxPerMinute) {
        return
      }
      criticalCount.count += 1
    }

    // Throttle
    const key = `${evt.type}:${title}:${message}`
    const now = Date.now()
    const last = lastEmittedAt.get(key) ?? 0
    if (now - last < THROTTLE_MS) {
      return
    }
    lastEmittedAt.set(key, now)

    emitBySeverity(severity, title, message)
  }

  async function fetchRecentEvents(limit = 20): Promise<EventItem[]> {
    try {
      const res = await fetch(`/api/events?source=all&limit=${limit}`)
      if (!res.ok) return []
      const data = await res.json()
      return data.items ?? data ?? []
    } catch {
      return []
    }
  }

  function processEvents(items: EventItem[], isBackfill = false) {
    for (const item of items) {
      const id = item.id ?? 0
      if (id && knownEventIds.has(id)) continue
      if (id) knownEventIds.add(id)

      const severity = parseSeverity(item.severity)
      const title = item.action ?? item.event_type ?? 'Notification'
      const message = detailHash(item.detail)

      if (isBackfill) {
        // Backfill shows all events without throttle, but still respect CRITICAL limit
        if (severity === 'CRITICAL') {
          const now = Date.now()
          if (now - criticalCount.windowStart > PERSISTENT_LIMIT_WINDOW_MS) {
            criticalCount.count = 0
            criticalCount.windowStart = now
          }
          if (criticalCount.count >= prefs.value.criticalPersistMaxPerMinute) {
            continue
          }
          criticalCount.count += 1
        }
        emitBySeverity(severity, title, message)
      } else {
        handleEvent({
          type: item.event_type ? 'trade_event' : 'audit_log',
          severity,
          action: item.action,
          event_type: item.event_type,
          detail: item.detail,
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

  function enable() {
    if (enabled) return
    enabled = true
    // Immediate backfill on enable
    backfill()
    // Start polling
    pollTimer = setInterval(poll, POLL_INTERVAL_MS)
  }

  function disable() {
    enabled = false
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function updatePreferences(patch: Partial<UserPreferences>) {
    prefs.value = { ...prefs.value, ...patch }
    savePreferences(prefs.value)
  }

  onUnmounted(() => disable())

  return { enable, disable, prefs, updatePreferences, backfill }
}
