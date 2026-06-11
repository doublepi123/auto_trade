declare global {
  interface Window {
    __AUTO_TRADE_API_KEY__?: string
  }
}

/** Build-time (Vite) or runtime (Docker entrypoint /runtime-config.js) API key. */
export function resolveApiKey(): string {
  const runtime = typeof window !== 'undefined' ? window.__AUTO_TRADE_API_KEY__ : undefined
  if (runtime) {
    return runtime
  }
  return import.meta.env.VITE_AUTO_TRADE_API_KEY ?? ''
}
