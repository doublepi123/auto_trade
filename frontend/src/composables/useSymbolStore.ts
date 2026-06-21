import { ref } from 'vue'

// Tiny cross-cutting store for "the user wants to look at this symbol next".
// The command palette (and the pinned-symbols bar) set a requested symbol;
// the Dashboard consumes it to switch its price/PnL chart. Lives at module
// level so it survives navigation without props/threading.
const requestedSymbol = ref<string | null>(null)

function requestSymbol(symbol: string): void {
  requestedSymbol.value = symbol
}

function clearRequested(): void {
  requestedSymbol.value = null
}

/** Return and clear the pending request (one-shot). */
function consumeRequestedSymbol(): string | null {
  const sym = requestedSymbol.value
  if (sym) requestedSymbol.value = null
  return sym
}

export function useSymbolStore() {
  return { requestedSymbol, requestSymbol, clearRequested, consumeRequestedSymbol }
}
