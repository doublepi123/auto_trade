export const APP_VERSION_MANIFEST_PATH = '/version.json'
const APP_VERSION_REQUEST_TIMEOUT_MS = 10_000

type FetchVersionManifest = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>

export function parseAppBuildId(value: unknown): string | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null
  }
  const buildId = Reflect.get(value, 'build_id')
  if (typeof buildId !== 'string') return null
  const normalized = buildId.trim()
  return normalized.length > 0 && normalized.length <= 200
    ? normalized
    : null
}

export async function fetchAppBuildId(
  fetchManifest: FetchVersionManifest = window.fetch.bind(window),
): Promise<string | null> {
  const controller = new AbortController()
  const timeout = setTimeout(
    () => controller.abort(),
    APP_VERSION_REQUEST_TIMEOUT_MS,
  )
  try {
    const response = await fetchManifest(APP_VERSION_MANIFEST_PATH, {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
    if (!response.ok) return null
    return parseAppBuildId(await response.json())
  } catch {
    // Version discovery must never disturb a working trading UI. A later
    // interval/focus check will retry transient or malformed responses.
    return null
  } finally {
    clearTimeout(timeout)
  }
}
