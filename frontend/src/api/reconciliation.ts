import { api } from './client'
import type { ReconciliationStatus } from '../types'

export async function getReconciliationStatus(): Promise<ReconciliationStatus> {
  const resp = await api.get('/api/reconciliation/status')
  return resp.data
}

export async function forceResumeReconciliation(reason: string): Promise<void> {
  await api.post('/api/force-resume', { reason })
}