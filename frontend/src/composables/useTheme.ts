import { ref } from 'vue'

const KEY = 'auto_trade.theme.dark'

const isDark = ref(false)
let initialized = false

function applyTheme(value: boolean, persist: boolean): void {
  isDark.value = value
  document.documentElement.classList.toggle('dark', value)
  if (!persist) return
  try {
    localStorage.setItem(KEY, value ? '1' : '0')
  } catch {
    /* ignore */
  }
}

export function initializeTheme(): void {
  if (initialized) return
  initialized = true
  try {
    applyTheme(localStorage.getItem(KEY) === '1', false)
  } catch {
    applyTheme(false, false)
  }
}

export function useTheme() {
  initializeTheme()
  return {
    isDark,
    toggleTheme: () => applyTheme(!isDark.value, true),
  }
}
