import { ref } from 'vue'

// App-wide command-palette singleton. `open`/`query`/`activeIndex` live at the
// module level so any caller (the global Cmd/Ctrl+K binding, a header button,
// future "open palette with command X" entry points) shares one instance.
const RECENT_KEY = 'auto_trade.palette.recent'
const MAX_RECENT = 8

const open = ref(false)
const query = ref('')
const activeIndex = ref(0)
const recentIds = ref<string[]>([])
let initialized = false

function loadRecent(): void {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
    if (Array.isArray(raw)) {
      recentIds.value = raw.filter((x) => typeof x === 'string').slice(0, MAX_RECENT)
    }
  } catch {
    recentIds.value = []
  }
}

function recordRecent(id: string): void {
  recentIds.value = [id, ...recentIds.value.filter((x) => x !== id)].slice(0, MAX_RECENT)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(recentIds.value))
  } catch {
    /* localStorage unavailable (private mode); keep in-memory only */
  }
}

function ensureInit(): void {
  if (initialized) return
  initialized = true
  loadRecent()
}

function openPalette(): void {
  ensureInit()
  query.value = ''
  activeIndex.value = 0
  open.value = true
}

function closePalette(): void {
  open.value = false
}

export function useCommandPalette() {
  ensureInit()
  return { open, query, activeIndex, recentIds, recordRecent, openPalette, closePalette }
}
