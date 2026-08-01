import { api } from './client'
import type { DatabaseHealthSnapshot } from '../types'

/**
 * Read-only SQLite storage-health snapshot. Pure observation — the backend
 * exposes no maintenance operations (no VACUUM/checkpoint/repair), and this
 * client must not imply any.
 */
export async function getDatabaseHealth(): Promise<DatabaseHealthSnapshot> {
  const resp = await api.get<DatabaseHealthSnapshot>('/api/database-health')
  return resp.data
}
