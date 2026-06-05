import { computed, ref, type Ref } from 'vue'
import { getDiagnostics } from '../api'
import type { DiagnosticsResponse, DiagnosticSymbolRuntime } from '../types'

export function useDiagnosticsSnapshot(selectedSymbol?: Ref<string>) {
  const diagnostics = ref<DiagnosticsResponse | null>(null)
  const loading = ref(false)
  const error = ref('')

  const selectedRuntime = computed<DiagnosticSymbolRuntime | null>(() => {
    if (!selectedSymbol?.value) return null
    return diagnostics.value?.symbol_runtimes.find((item) => item.symbol === selectedSymbol.value) ?? null
  })

  async function load() {
    loading.value = true
    error.value = ''
    try {
      diagnostics.value = await getDiagnostics()
      return diagnostics.value
    } catch (err) {
      diagnostics.value = null
      error.value = err instanceof Error ? err.message : '加载运行诊断快照失败'
      throw err
    } finally {
      loading.value = false
    }
  }

  function reset() {
    diagnostics.value = null
    error.value = ''
  }

  return {
    diagnostics,
    loading,
    error,
    selectedRuntime,
    load,
    reset,
  }
}
