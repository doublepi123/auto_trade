import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getNotifications } from '../api'
import type { NotificationLogOut } from '../types'

const STORAGE_KEY = 'notifications_last_read_at'
const POLL_INTERVAL_MS = 10000
const FETCH_PAGE_SIZE = 50

const sharedUnreadCount = ref(0)
const sharedLastReadAt = ref<string>('')
const sharedRecentItems = ref<NotificationLogOut[]>([])
let sharedPollTimer: ReturnType<typeof setInterval> | null = null
let sharedEnabled = false
let refCount = 0

function loadLastReadAt(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function saveLastReadAt(value: string) {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // ignore
  }
}

function isUnread(item: NotificationLogOut, lastReadAt: string): boolean {
  if (!lastReadAt) return true
  return item.created_at > lastReadAt
}

async function load() {
  try {
    const data = await getNotifications({ page: 1, page_size: FETCH_PAGE_SIZE })
    const items = data.items ?? []
    sharedRecentItems.value = items
    const lastRead = sharedLastReadAt.value
    sharedUnreadCount.value = lastRead
      ? items.filter((i) => isUnread(i, lastRead)).length
      : items.length
  } catch {
    // Keep previous state on error to avoid badge flicker
  }
}

function enable() {
  if (sharedEnabled) return
  sharedEnabled = true
  sharedLastReadAt.value = loadLastReadAt()
  load()
  sharedPollTimer = setInterval(load, POLL_INTERVAL_MS)
}

function disable() {
  sharedEnabled = false
  if (sharedPollTimer) {
    clearInterval(sharedPollTimer)
    sharedPollTimer = null
  }
}

export function useNotificationBadge() {
  onMounted(() => {
    refCount++
    if (refCount === 1) {
      enable()
    }
  })

  onUnmounted(() => {
    refCount = Math.max(0, refCount - 1)
    if (refCount <= 0) {
      disable()
      refCount = 0
    }
  })

  async function markAllRead() {
    const now = new Date().toISOString()
    sharedLastReadAt.value = now
    saveLastReadAt(now)
    sharedUnreadCount.value = 0
  }

  function refresh() {
    return load()
  }

  const lastReadAt = computed(() => sharedLastReadAt.value)
  const isItemUnread = (item: NotificationLogOut) => isUnread(item, sharedLastReadAt.value)

  return {
    unreadCount: sharedUnreadCount,
    recentItems: sharedRecentItems,
    lastReadAt,
    markAllRead,
    refresh,
    isItemUnread,
  }
}
