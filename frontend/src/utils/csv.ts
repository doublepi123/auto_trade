/**
 * Client-side CSV helpers shared by views that export already-loaded data.
 * Keep this dependency-free so every round can serialize in-memory rows to a
 * downloadable file without hitting the backend.
 */

function escapeCell(value: unknown): string {
  if (value == null) return ''
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
  const trimmed = text.trimStart()
  const sanitized = /^[=+\-@]/.test(trimmed) ? `'${text}` : text
  // Quote whenever the cell contains a delimiter, quote, or newline; double
  // embedded quotes per RFC 4180.
  if (/[",\n\r]/.test(sanitized)) {
    return `"${sanitized.replace(/"/g, '""')}"`
  }
  return sanitized
}

export function buildCsv<T extends Record<string, unknown>>(
  headers: { key: keyof T; label: string }[],
  rows: T[],
): string {
  const head = headers.map((h) => escapeCell(h.label)).join(',')
  const body = rows
    .map((row) => headers.map((h) => escapeCell(row[h.key])).join(','))
    .join('\n')
  return `${head}\n${body}`
}

export function downloadText(filename: string, content: string, mime = 'text/csv;charset=utf-8'): void {
  // Prepend a UTF-8 BOM so Excel/Numbers detect encoding for CJK content.
  const blob = new Blob([`﻿${content}`], { type: mime })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  // Defer revoke so the click has time to dispatch in all browsers.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadCsv(filename: string, headers: { key: string; label: string }[], rows: Record<string, unknown>[]): void {
  downloadText(filename, buildCsv(headers as never, rows as never[]))
}
