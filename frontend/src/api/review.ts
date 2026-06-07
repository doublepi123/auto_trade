import { api } from './client'
import type { ReviewResponse } from '../types'

export async function getReview(params: {
  symbol: string
  from_date: string
  to_date: string
}): Promise<ReviewResponse> {
  const resp = await api.get<ReviewResponse>('/api/review', { params })
  return resp.data
}

export async function exportReview(params: {
  symbol: string
  from_date: string
  to_date: string
  format: 'json' | 'csv'
}) {
  return api.get('/api/review/export', {
    params,
    responseType: params.format === 'csv' ? 'blob' : 'json',
  })
}
