import { ref } from 'vue'

// Persisted pinned-symbols list for the Dashboard quick-jump bar. Clicking a
// pinned chip hands the symbol to useSymbolStore so the chart switches to it.
const KEY = 'auto_trade.pinned-symbols'
const MAX = 12

const pinned = ref<string[]>([])
let loaded = false

function load(): void {
  if (loaded) return
  loaded = true
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    if (Array.isArray(raw)) {
      pinned.value = raw.filter((x) => typeof x === 'string').slice(0, MAX)
    }
  } catch {
    pinned.value = []
  }
}

function persist(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(pinned.value))
  } catch {
    /* ignore */
  }
}

export function usePinnedSymbols() {
  load()
  function isPinned(sym: string): boolean {
    return pinned.value.includes(sym)
  }
  function pin(sym: string): void {
    if (!sym || pinned.value.includes(sym)) return
    pinned.value = [...pinned.value, sym].slice(0, MAX)
    persist()
  }
  function unpin(sym: string): void {
    pinned.value = pinned.value.filter((s) => s !== sym)
    persist()
  }
  function togglePin(sym: string): void {
    if (isPinned(sym)) unpin(sym)
    else pin(sym)
  }
  return { pinned, isPinned, pin, unpin, togglePin }
}
