import { ref } from 'vue'

// Tracks recently-visited routes (most-recent first), persisted, so the command
// palette can surface the pages the user actually opens often — independent of
// whether they got there via the palette or the nav links.
const KEY = 'auto_trade.recent-pages'
const MAX = 8

const recentPaths = ref<string[]>([])
let loaded = false

function load(): void {
  if (loaded) return
  loaded = true
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    if (Array.isArray(raw)) {
      recentPaths.value = raw.filter((x) => typeof x === 'string').slice(0, MAX)
    }
  } catch {
    recentPaths.value = []
  }
}

function recordVisit(path: string): void {
  load()
  if (!path) return
  recentPaths.value = [path, ...recentPaths.value.filter((p) => p !== path)].slice(0, MAX)
  try {
    localStorage.setItem(KEY, JSON.stringify(recentPaths.value))
  } catch {
    /* ignore */
  }
}

/** 0 = most recently visited; Number.MAX_SAFE_INTEGER = never visited. */
function recencyRank(path: string): number {
  load()
  const idx = recentPaths.value.indexOf(path)
  return idx === -1 ? Number.MAX_SAFE_INTEGER : idx
}

export function useRecentPages() {
  load()
  return { recentPaths, recordVisit, recencyRank }
}
