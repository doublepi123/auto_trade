import { api } from './client'
import type { ReviewResponse } from '../types'

export async function getReview(params: {
  symbol: string
  from_date: string
  to_date: string
}) {
  return api.get<ReviewResponse>('/api/review', { params })
}

export async function exportReview(params: {
  symbol: string
  from_date: string
  to_date: string
  format: 'json' | 'csv'
}) {
  return api.get('/api/review/export', {
    params,
    responseType: 'blob',
  })
}
