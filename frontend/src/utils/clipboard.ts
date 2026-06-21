/**
 * Copy text to the clipboard, returning whether it succeeded.
 *
 * Prefers the async Clipboard API (available in secure contexts — https or
 * localhost). Falls back to a hidden-textarea + `document.execCommand('copy')`
 * for non-secure contexts (e.g. plain-http on a trusted LAN, which is this
 * project's default deployment). Never throws: callers can branch on the
 * boolean without a try/catch.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false

  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied or insecure context — fall through to legacy path.
    }
  }

  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.top = '0'
    textarea.style.left = '0'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}
