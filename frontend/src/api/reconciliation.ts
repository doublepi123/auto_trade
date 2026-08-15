import { api } from './client'
import type { ReconciliationStatus, ReconciliationEvidenceSurface } from '../types'

export async function getReconciliationStatus(): Promise<ReconciliationStatus> {
  const resp = await api.get('/api/reconciliation/status')
  return resp.data
}

export async function getReconciliationEvidenceSurface(): Promise<ReconciliationEvidenceSurface> {
  const resp = await api.get('/api/reconciliation/evidence-surface')
  return resp.data
}

export async function forceResumeReconciliation(reason: string): Promise<void> {
  await api.post('/api/force-resume', { reason })
}