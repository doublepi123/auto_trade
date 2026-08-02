import { api } from './client'
import type { CronHealthSnapshot } from '../types'

/**
 * Read-only process-local cron job health snapshot. Pure observation — the
 * backend exposes no run/restart/register operations for these jobs, and this
 * client must not imply any. Viewing never triggers a cron tick.
 */
export async function getCronHealth(): Promise<CronHealthSnapshot> {
  const resp = await api.get<CronHealthSnapshot>('/api/cron-health')
  return resp.data
}
