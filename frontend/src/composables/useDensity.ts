import { ref } from 'vue'

// Global Element Plus density (component `size`), persisted. Applied app-wide
// via <el-config-provider :size> at the root so every control honours it.
type Size = 'small' | 'default' | 'large'
const KEY = 'auto_trade.density'
const ORDER: Size[] = ['default', 'small', 'large']

const size = ref<Size>('default')
let loaded = false

function load(): void {
  if (loaded) return
  loaded = true
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'small' || v === 'default' || v === 'large') size.value = v
  } catch {
    /* ignore */
  }
}

function persist(): void {
  try {
    localStorage.setItem(KEY, size.value)
  } catch {
    /* ignore */
  }
}

function setDensity(s: Size): void {
  load()
  size.value = s
  persist()
}

function cycleDensity(): void {
  load()
  const idx = ORDER.indexOf(size.value)
  size.value = ORDER[(idx + 1) % ORDER.length]
  persist()
}

export function useDensity() {
  load()
  return { size, setDensity, cycleDensity, ORDER }
}
