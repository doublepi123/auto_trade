import { api } from './client'
import type {
  UniverseCatalogItem,
  UniversePromotionReadinessResponse,
  UniverseRotationForwardScorecardResponse,
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

const PROMOTION_UNIVERSE_ROLES = new Set([
  'SELECTED',
  'EXPLORATION',
  'TRADING_TARGET',
])

const ROTATION_SCORECARD_STATUSES = new Set([
  'NOT_REGISTERED',
  'AWAITING_PRECOMMITMENT',
  'COLLECTING',
  'DATA_BLOCKED',
  'PERFORMANCE_BLOCKED',
  'READY_FOR_MANUAL_REVIEW',
])

const ROTATION_SCORECARD_EVIDENCE_MODES = new Set([
  'FORWARD_PRECOMMITTED',
  'BACKFILLED_AFTER_ENTRY',
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

function assertNullablePositiveInteger(value: unknown, field: string): void {
  if (value !== null) assertPositiveInteger(value, field)
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
  assertString(value.universe_role, `${prefix}.universe_role`)
  if (!PROMOTION_UNIVERSE_ROLES.has(value.universe_role)) {
    throw new Error(
      `Unexpected /api/universe/promotion-readiness response: ${prefix}.universe_role is invalid`,
    )
  }
  assertNullablePositiveInteger(value.rank, `${prefix}.rank`)
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

function scorecardError(field: string): Error {
  return new Error(
    `Unexpected /api/universe/rotation-forward-scorecard response: ${field} is invalid`,
  )
}

function assertScorecardString(
  value: unknown,
  field: string,
): asserts value is string {
  if (typeof value !== 'string' || !value) throw scorecardError(field)
}

function assertScorecardInteger(
  value: unknown,
  field: string,
  minimum = 0,
): asserts value is number {
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < minimum
  ) throw scorecardError(field)
}

function assertScorecardNullableNumber(
  value: unknown,
  field: string,
): asserts value is number | null {
  if (value !== null && (typeof value !== 'number' || !Number.isFinite(value))) {
    throw scorecardError(field)
  }
}

function assertScorecardStringArray(
  value: unknown,
  field: string,
): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw scorecardError(field)
  }
}

function assertScorecardCohort(value: unknown, field: string): void {
  assertObject(value, `/api/universe/rotation-forward-scorecard.${field}`)
  assertScorecardInteger(value.source_run_id, `${field}.source_run_id`, 1)
  for (const key of [
    'source_as_of_date',
    'cohort_month',
    'status',
    'signal_date',
    'entry_date',
    'mark_date',
  ]) assertScorecardString(value[key], `${field}.${key}`)
  assertScorecardString(value.evidence_mode, `${field}.evidence_mode`)
  assertScorecardString(
    value.registered_as_of_date,
    `${field}.registered_as_of_date`,
  )
  if (!ROTATION_SCORECARD_EVIDENCE_MODES.has(value.evidence_mode)) {
    throw scorecardError(`${field}.evidence_mode`)
  }
  if (typeof value.forward_eligible !== 'boolean') {
    throw scorecardError(`${field}.forward_eligible`)
  }
  if (
    value.forward_eligible !== (
      value.evidence_mode === 'FORWARD_PRECOMMITTED'
      && value.registered_as_of_date === value.signal_date
    )
  ) throw scorecardError(`${field}.forward_eligible`)
  assertScorecardStringArray(value.target_symbols, `${field}.target_symbols`)
  assertScorecardInteger(
    value.forward_observation_sessions,
    `${field}.forward_observation_sessions`,
  )
  for (const key of [
    'net_return_pct',
    'qqq_return_pct',
    'dia_return_pct',
    'excess_return_vs_qqq_pct',
    'excess_return_vs_dia_pct',
  ]) assertScorecardNullableNumber(value[key], `${field}.${key}`)
  if (typeof value.selection_drift_detected !== 'boolean') {
    throw scorecardError(`${field}.selection_drift_detected`)
  }
  if (typeof value.survivorship_bias !== 'boolean') {
    throw scorecardError(`${field}.survivorship_bias`)
  }
  assertScorecardStringArray(value.blockers, `${field}.blockers`)
}

function assertScorecardTrack(value: unknown, index: number): void {
  const field = `tracks[${index}]`
  assertObject(value, `/api/universe/rotation-forward-scorecard.${field}`)
  assertScorecardString(value.variant_name, `${field}.variant_name`)
  assertScorecardString(value.status, `${field}.status`)
  if (!ROTATION_SCORECARD_STATUSES.has(value.status)) {
    throw scorecardError(`${field}.status`)
  }
  assertScorecardInteger(value.observed_cohorts, `${field}.observed_cohorts`)
  assertScorecardInteger(
    value.forward_eligible_cohorts,
    `${field}.forward_eligible_cohorts`,
  )
  assertScorecardInteger(value.completed_cohorts, `${field}.completed_cohorts`)
  assertScorecardInteger(
    value.remaining_completed_cohorts,
    `${field}.remaining_completed_cohorts`,
  )
  assertScorecardInteger(
    value.backfilled_cohorts,
    `${field}.backfilled_cohorts`,
  )
  assertScorecardInteger(
    value.incomplete_closed_cohorts,
    `${field}.incomplete_closed_cohorts`,
  )
  assertScorecardInteger(
    value.selection_drift_cohorts,
    `${field}.selection_drift_cohorts`,
  )
  assertScorecardInteger(
    value.invalid_evidence_records,
    `${field}.invalid_evidence_records`,
  )
  assertScorecardInteger(
    value.minimum_completed_cohorts,
    `${field}.minimum_completed_cohorts`,
    1,
  )
  if (
    value.completed_cohorts > value.forward_eligible_cohorts
    || value.forward_eligible_cohorts + value.backfilled_cohorts
      !== value.observed_cohorts
    || value.remaining_completed_cohorts
      !== Math.max(0, value.minimum_completed_cohorts - value.completed_cohorts)
  ) throw scorecardError(`${field}.cohort_counts`)
  for (const key of [
    'first_completed_cohort_month',
    'latest_completed_cohort_month',
  ]) {
    if (value[key] !== null) assertScorecardString(value[key], `${field}.${key}`)
  }
  const openCohort = value.open_cohort
  if (openCohort !== null) {
    assertObject(openCohort, `${field}.open_cohort`)
    assertScorecardCohort(openCohort, `${field}.open_cohort`)
    if (openCohort.forward_eligible !== true) {
      throw scorecardError(`${field}.open_cohort.forward_eligible`)
    }
  }
  const diagnosticCohort = value.diagnostic_cohort
  if (diagnosticCohort !== null) {
    assertObject(diagnosticCohort, `${field}.diagnostic_cohort`)
    assertScorecardCohort(
      diagnosticCohort,
      `${field}.diagnostic_cohort`,
    )
    if (diagnosticCohort.forward_eligible !== false) {
      throw scorecardError(`${field}.diagnostic_cohort.forward_eligible`)
    }
  }
  if (
    (value.backfilled_cohorts > 0) !== (diagnosticCohort !== null)
  ) {
    throw scorecardError(`${field}.diagnostic_cohort`)
  }
  for (const key of [
    'compounded_return_pct',
    'qqq_compounded_return_pct',
    'dia_compounded_return_pct',
    'compounded_excess_vs_qqq_pct',
    'compounded_excess_vs_dia_pct',
    'positive_cohort_rate_pct',
    'excess_win_rate_vs_qqq_pct',
    'excess_win_rate_vs_dia_pct',
    'average_cohort_return_pct',
    'worst_cohort_return_pct',
  ]) assertScorecardNullableNumber(value[key], `${field}.${key}`)
  if (typeof value.manual_review_ready !== 'boolean') {
    throw scorecardError(`${field}.manual_review_ready`)
  }
  if (value.automatic_promotion_allowed !== false) {
    throw scorecardError(`${field}.automatic_promotion_allowed`)
  }
  assertScorecardStringArray(value.blockers, `${field}.blockers`)
  assertScorecardStringArray(value.warnings, `${field}.warnings`)
  if (
    value.manual_review_ready !== (value.status === 'READY_FOR_MANUAL_REVIEW')
    || (value.manual_review_ready && value.blockers.length > 0)
  ) throw scorecardError(`${field}.manual_review_ready`)
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
    'exploration_symbols',
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

export async function getRotationForwardScorecard(): Promise<UniverseRotationForwardScorecardResponse> {
  const resp = await api.get('/api/universe/rotation-forward-scorecard')
  assertObject(resp.data, '/api/universe/rotation-forward-scorecard')
  assertScorecardString(resp.data.algorithm_version, 'algorithm_version')
  assertScorecardInteger(resp.data.universe_run_id, 'universe_run_id', 1)
  assertScorecardString(resp.data.as_of_date, 'as_of_date')
  assertScorecardString(resp.data.generated_at, 'generated_at')
  assertScorecardInteger(resp.data.source_run_count, 'source_run_count', 1)
  if (!Array.isArray(resp.data.tracks)) throw scorecardError('tracks')
  resp.data.tracks.forEach(assertScorecardTrack)
  if (resp.data.automatic_promotion_allowed !== false) {
    throw scorecardError('automatic_promotion_allowed')
  }
  return resp.data as unknown as UniverseRotationForwardScorecardResponse
}
