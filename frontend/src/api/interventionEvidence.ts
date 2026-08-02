import { api } from './client'
import type { InterventionEvidenceResponse } from '../types'

/**
 * Query contract for GET /api/intervention-evidence.
 *
 * `from_date` / `to_date` are inclusive UTC calendar dates (YYYY-MM-DD);
 * `limit` is the response row bound (backend clamps to 1..1000, default 500).
 * Pairing is computed globally BEFORE these filters are applied server-side.
 * Inverted ranges are rejected with HTTP 422; an unavailable read snapshot
 * yields HTTP 503. Read-only: viewing/filtering never mutates evidence.
 */
export interface GetInterventionEvidenceParams {
  from_date?: string
  to_date?: string
  limit?: number
}

export async function getInterventionEvidence(
  params: GetInterventionEvidenceParams = {},
): Promise<InterventionEvidenceResponse> {
  const sp = new URLSearchParams()
  if (params.from_date) sp.set('from_date', params.from_date)
  if (params.to_date) sp.set('to_date', params.to_date)
  if (params.limit != null) sp.set('limit', String(params.limit))
  const qs = sp.toString()
  const url = qs ? `/api/intervention-evidence?${qs}` : '/api/intervention-evidence'
  const resp = await api.get<InterventionEvidenceResponse>(url)
  return resp.data
}
