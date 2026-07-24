import { api } from './client'
import type {
  UniverseCatalogItem,
  UniversePromotionReadinessResponse,
  UniverseSelectionRefreshResponse,
  UniverseSelectionRunResponse,
} from '../types'

const PROMOTION_FORWARD_STATUSES = new Set([
  'NOT_REGISTERED',
  'FROZEN',
  'COLLECTING',
  'READY_FOR_REVIEW',
  'MATURE_EVIDENCE',
  'BLOCKED',
])

function assertObject(value: unknown, endpoint: string): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Unexpected ${endpoint} response`)
  }
}

function assertString(value: unknown, field: string): asserts value is string {
  if (typeof value !== 'string') {
    throw new Error(`Unexpected /api/universe/promotion-readiness response: ${field} is not a string`)
  }
}

function assertBoolean(value: unknown, field: string): asserts value is boolean {
  if (typeof value !== 'boolean') {
    throw new Error(`Unexpected /api/universe/promotion-readiness response: ${field} is not a boolean`)
  }
}

function assertFiniteNumber(value: unknown, field: string): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`Unexpected /api/universe/promotion-readiness response: ${field} is not a finite number`)
  }
}

function assertNonNegativeInteger(value: unknown, field: string): asserts value is number {
  assertFiniteNumber(value, field)
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`Unexpected /api/universe/promotion-readiness response: ${field} is not a non-negative integer`)
  }
}

function assertPositiveInteger(value: unknown, field: string): asserts value is number {
  assertFiniteNumber(value, field)
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`Unexpected /api/universe/promotion-readiness response: ${field} is not a positive integer`)
  }
}

function assertNullableFiniteNumber(value: unknown, field: string): void {
  if (value !== null) assertFiniteNumber(value, field)
}

function assertNullableString(value: unknown, field: string): void {
  if (value !== null) assertString(value, field)
}

function assertShadowMetrics(value: unknown, field: string): void {
  assertObject(value, `/api/universe/promotion-readiness.${field}`)
  for (const metric of [
    'bars',
    'eligible_bars',
    'breaches',
    'reclaims',
    'entries',
    'exits',
    'closed_trades',
    'win_rate',
    'gross_pnl',
    'fees',
    'net_pnl',
    'max_drawdown',
    'avg_holding_minutes',
    'avg_mae_pct',
    'avg_mfe_pct',
  ]) {
    assertFiniteNumber(value[metric], `${field}.${metric}`)
  }
  assertBoolean(value.comparison_available, `${field}.comparison_available`)
  for (const metric of [
    'live_action_count',
    'action_agreement_rate',
    'net_pnl_delta_vs_live',
  ]) {
    assertNullableFiniteNumber(value[metric], `${field}.${metric}`)
  }
}

function assertPromotionReadinessItem(value: unknown, index: number): void {
  const prefix = `items[${index}]`
  assertObject(value, `/api/universe/promotion-readiness.${prefix}`)
  assertString(value.symbol, `${prefix}.symbol`)
  assertPositiveInteger(value.rank, `${prefix}.rank`)
  assertFiniteNumber(value.selection_score, `${prefix}.selection_score`)
  assertPositiveInteger(value.priority_rank, `${prefix}.priority_rank`)
  assertFiniteNumber(value.priority_score, `${prefix}.priority_score`)
  assertFiniteNumber(value.quant_weight, `${prefix}.quant_weight`)
  assertFiniteNumber(value.quant_adjustment, `${prefix}.quant_adjustment`)
  if (value.quant_weight < 0 || value.quant_weight > 0.35) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.quant_weight is outside [0, 0.35]`,
    )
  }
  if (value.quant_adjustment < -25 || value.quant_adjustment > 17.5) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.quant_adjustment is outside [-25, 17.5]`,
    )
  }
  assertNullableFiniteNumber(value.quant_score, `${prefix}.quant_score`)
  assertNullableFiniteNumber(value.quant_confidence, `${prefix}.quant_confidence`)
  assertString(value.quant_recommended_action, `${prefix}.quant_recommended_action`)
  assertString(value.quant_source, `${prefix}.quant_source`)
  assertBoolean(value.quant_fresh, `${prefix}.quant_fresh`)
  assertNullableString(value.quant_expires_at, `${prefix}.quant_expires_at`)
  assertBoolean(value.is_trading_target, `${prefix}.is_trading_target`)
  assertBoolean(value.shadow_enabled, `${prefix}.shadow_enabled`)
  assertString(value.forward_status, `${prefix}.forward_status`)
  if (!PROMOTION_FORWARD_STATUSES.has(value.forward_status)) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.forward_status is invalid`,
    )
  }
  assertNonNegativeInteger(value.included_pairs, `${prefix}.included_pairs`)
  assertPositiveInteger(value.minimum_ready_pairs, `${prefix}.minimum_ready_pairs`)
  assertPositiveInteger(value.minimum_mature_pairs, `${prefix}.minimum_mature_pairs`)
  if (value.minimum_mature_pairs < value.minimum_ready_pairs) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.minimum_mature_pairs is below minimum_ready_pairs`,
    )
  }
  assertNonNegativeInteger(value.remaining_ready_pairs, `${prefix}.remaining_ready_pairs`)
  assertNonNegativeInteger(value.remaining_mature_pairs, `${prefix}.remaining_mature_pairs`)
  if (!Array.isArray(value.blockers) || value.blockers.some((blocker) => typeof blocker !== 'string')) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.blockers is not a string array`,
    )
  }
  assertShadowMetrics(value.baseline_metrics, `${prefix}.baseline_metrics`)
  assertShadowMetrics(value.candidate_metrics, `${prefix}.candidate_metrics`)
  assertBoolean(value.review_ready, `${prefix}.review_ready`)
  assertBoolean(value.mature_evidence, `${prefix}.mature_evidence`)
  if (value.automatic_promotion_allowed !== false) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.automatic_promotion_allowed must be false`,
    )
  }
}

export async function getUniverseCatalog(): Promise<UniverseCatalogItem[]> {
  const resp = await api.get('/api/universe/catalog')
  if (!Array.isArray(resp.data)) {
    throw new Error('Unexpected /api/universe/catalog response')
  }
  return resp.data as UniverseCatalogItem[]
}

export async function getLatestUniverseSelection(): Promise<UniverseSelectionRunResponse> {
  const resp = await api.get('/api/universe/latest')
  assertObject(resp.data, '/api/universe/latest')
  if (!Array.isArray(resp.data.items)) {
    throw new Error('Unexpected /api/universe/latest response: items is not an array')
  }
  return resp.data as unknown as UniverseSelectionRunResponse
}

export async function refreshUniverseSelection(): Promise<UniverseSelectionRefreshResponse> {
  const resp = await api.post('/api/universe/refresh', undefined, { timeout: 120_000 })
  assertObject(resp.data, '/api/universe/refresh')
  assertObject(resp.data.run, '/api/universe/refresh.run')
  if (!Array.isArray(resp.data.run.items)) {
    throw new Error('Unexpected /api/universe/refresh response: run.items is not an array')
  }
  for (const field of [
    'added_symbols',
    'removed_symbols',
    'retained_symbols',
    'shadow_enabled_symbols',
    'shadow_disabled_symbols',
    'shadow_failed_symbols',
  ]) {
    if (!Array.isArray(resp.data[field])) {
      throw new Error(`Unexpected /api/universe/refresh response: ${field} is not an array`)
    }
  }
  return resp.data as unknown as UniverseSelectionRefreshResponse
}

export async function getUniversePromotionReadiness(): Promise<UniversePromotionReadinessResponse> {
  const resp = await api.get('/api/universe/promotion-readiness')
  assertObject(resp.data, '/api/universe/promotion-readiness')
  assertPositiveInteger(resp.data.universe_run_id, 'universe_run_id')
  assertString(resp.data.as_of_date, 'as_of_date')
  assertString(resp.data.generated_at, 'generated_at')
  assertString(resp.data.priority_algorithm_version, 'priority_algorithm_version')
  if (!Array.isArray(resp.data.items)) {
    throw new Error(
      'Unexpected /api/universe/promotion-readiness response: items is not an array',
    )
  }
  resp.data.items.forEach(assertPromotionReadinessItem)
  return resp.data as unknown as UniversePromotionReadinessResponse
}
