import { api } from './client'
import type { SymbolAttributionResponse } from '../types'

export interface PnlBySymbolQuery {
  symbol?: string
  days?: number
}

export async function getPnlBySymbol(params: PnlBySymbolQuery = {}): Promise<SymbolAttributionResponse> {
  const resp = await api.get('/api/pnl/by-symbol', { params })
  return resp.data
}
