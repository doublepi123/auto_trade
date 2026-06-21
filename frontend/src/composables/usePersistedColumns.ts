import { ref, watch } from 'vue'

/**
 * Persisted per-view column-visibility map. `defaults` declares every
 * toggleable column key and its default visibility; persisted overrides are
 * merged on top so newly-added columns stay visible until the user opts out.
 *
 * The composable owns persistence: any mutation to `visible` (e.g. via
 * `v-model="visible.someKey"`) is written to localStorage automatically.
 */
export function usePersistedColumns(
  storageKey: string,
  defaults: Record<string, boolean>,
) {
  const visible = ref<Record<string, boolean>>({ ...defaults })
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) ?? '{}')
    if (raw && typeof raw === 'object') {
      visible.value = { ...defaults, ...raw }
    }
  } catch {
    /* keep defaults */
  }

  watch(
    visible,
    (val) => {
      try {
        localStorage.setItem(storageKey, JSON.stringify(val))
      } catch {
        /* ignore */
      }
    },
    { deep: true },
  )

  function toggle(key: string): void {
    visible.value = { ...visible.value, [key]: !visible.value[key] }
  }

  return { visible, toggle }
}
