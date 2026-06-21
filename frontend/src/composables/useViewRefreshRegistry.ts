import { onUnmounted, ref } from 'vue'

// The command palette lives in the App shell (an ancestor of the active
// route view), so provide/inject can't reach from view up to palette. This
// module singleton is the bridge: the active view registers its reload
// function on mount (and clears it on unmount), and the palette calls
// whatever is currently registered.
type RefreshFn = () => void | Promise<void>

const currentRefresh = ref<RefreshFn | null>(null)

function registerViewRefresh(fn: RefreshFn): void {
  currentRefresh.value = fn
}

/**
 * Register `fn` as the active view's reload for the lifetime of the calling
 * component. On unmount, clears the registration only if it still owns it
 * (so a newly-mounted view's registration isn't wiped by the old one's
 * cleanup).
 */
export function useRegisterViewRefresh(fn: RefreshFn): void {
  currentRefresh.value = fn
  onUnmounted(() => {
    if (currentRefresh.value === fn) currentRefresh.value = null
  })
}

export function useViewRefreshRegistry() {
  return { currentRefresh, registerViewRefresh }
}
