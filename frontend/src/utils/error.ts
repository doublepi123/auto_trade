/**
 * Best-effort extraction of a human-readable error message from an Axios-like
 * error object. Falls back to `fallback` if the structure is not recognised
 * (network error, plain `Error`, unexpected payload, etc.).
 */
export function resolveErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as Record<string, unknown>).response
    if (resp && typeof resp === 'object' && 'data' in resp) {
      const data = (resp as Record<string, unknown>).data
      if (data && typeof data === 'object' && 'detail' in data) {
        const detail = (data as Record<string, unknown>).detail
        if (typeof detail === 'string') return detail
        if (Array.isArray(detail) && detail.length > 0) {
          const first = detail[0]
          if (first && typeof first === 'object' && 'msg' in first) {
            const msg = (first as Record<string, unknown>).msg
            if (typeof msg === 'string') return msg
          }
        }
      }
    }
    const message = (err as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}
