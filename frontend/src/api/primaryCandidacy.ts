import { api } from './client'
import type { PrimaryCandidacyQuery, PrimaryCandidacyResponse } from '../types'

const ENDPOINT = '/api/universe/primary-candidacy'

const VERDICTS = new Set([
  'NO_SELECTION_RUN',
  'SELECTION_SUPPORTED',
  'SELECTION_NOT_SUPPORTED_BY_EVIDENCE',
])

const INCUMBENT_STATUSES = new Set([
  'EVIDENCE_THIN',
  'ACCEPTABLE',
  'TREND_UNSUITABLE',
])

const POOL_GATE_STATUSES = new Set(['PASS', 'BLOCKED', 'UNASSESSABLE'])

const POWER_VERDICTS = new Set(['POWERED', 'UNPOWERED', 'UNMEASURABLE'])

function candidacyError(field: string): Error {
  return new Error(`Unexpected ${ENDPOINT} response: ${field} is invalid`)
}

function assertObject(
  value: unknown,
  field: string,
): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw candidacyError(field)
  }
}

function assertString(value: unknown, field: string): asserts value is string {
  if (typeof value !== 'string') throw candidacyError(field)
}

function assertBoolean(value: unknown, field: string): asserts value is boolean {
  if (typeof value !== 'boolean') throw candidacyError(field)
}

function assertFiniteNumber(value: unknown, field: string): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw candidacyError(field)
}

function assertNullableFiniteNumber(value: unknown, field: string): void {
  if (value !== null) assertFiniteNumber(value, field)
}

function assertStringArray(value: unknown, field: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw candidacyError(field)
  }
}

function assertMember(value: unknown, allowed: Set<string>, field: string): void {
  assertString(value, field)
  if (!allowed.has(value)) throw candidacyError(field)
}

function assertGateParameters(value: unknown): void {
  assertObject(value, 'gate_parameters')
  for (const key of [
    'lookback_days',
    'min_samples',
    'incumbent_trend_pct',
    'candidate_trend_pct',
    'reach_lookback_days',
    'min_reach_rate_pct',
    'min_closed_trades',
    'max_price_age_seconds',
  ]) assertFiniteNumber(value[key], `gate_parameters.${key}`)
}

function assertPoolGate(value: unknown): void {
  assertObject(value, 'pool_gate')
  assertMember(value.status, POOL_GATE_STATUSES, 'pool_gate.status')
  assertString(value.detail, 'pool_gate.detail')
  assertBoolean(value.enforced_by_switch, 'pool_gate.enforced_by_switch')
}

function assertPower(value: unknown): void {
  assertObject(value, 'power')
  assertMember(value.verdict, POWER_VERDICTS, 'power.verdict')
  for (const key of ['sigma_observations', 'delta_bps', 'alpha', 'power', 'max_trades_held']) {
    assertFiniteNumber(value[key], `power.${key}`)
  }
  for (const key of [
    'sigma_bps',
    'required_one_sample',
    'required_two_sample',
    'shortfall_factor',
  ]) assertNullableFiniteNumber(value[key], `power.${key}`)
  assertString(value.max_trades_symbol, 'power.max_trades_symbol')
  const operatingPoint = value.reach_gate_operating_point
  assertObject(operatingPoint, 'power.reach_gate_operating_point')
  for (const key of ['n', 'k_min', 'alpha_at_loser', 'power_at_winner']) {
    assertFiniteNumber(operatingPoint[key], `power.reach_gate_operating_point.${key}`)
  }
  assertString(operatingPoint.basis, 'power.reach_gate_operating_point.basis')
}

function assertCandidate(value: unknown, index: number): void {
  const field = `candidates[${index}]`
  assertObject(value, field)
  assertString(value.symbol, `${field}.symbol`)
  assertBoolean(value.passes_all_symbol_gates, `${field}.passes_all_symbol_gates`)
  assertStringArray(value.gate_reasons, `${field}.gate_reasons`)
  assertFiniteNumber(value.trend_blocked_pct, `${field}.trend_blocked_pct`)
  assertFiniteNumber(value.closed_trades, `${field}.closed_trades`)
  assertFiniteNumber(value.trades_held, `${field}.trades_held`)
  assertNullableFiniteNumber(value.reach_rate_pct, `${field}.reach_rate_pct`)
  assertNullableFiniteNumber(value.power_share_pct, `${field}.power_share_pct`)
  if (value.passes_all_symbol_gates !== (value.gate_reasons.length === 0)) {
    throw candidacyError(`${field}.gate_reasons`)
  }
}

function assertEdgePick(value: unknown): void {
  if (value === null) return
  assertObject(value, 'edge_pick')
  assertString(value.symbol, 'edge_pick.symbol')
  assertString(value.selection_rule, 'edge_pick.selection_rule')
  for (const key of ['trend_blocked_pct', 'closed_trades', 'reach_rate_pct']) {
    assertFiniteNumber(value[key], `edge_pick.${key}`)
  }
}

function assertGatesOnlyPick(value: unknown): void {
  if (value === null) return
  assertObject(value, 'gates_only_pick')
  assertString(value.symbol, 'gates_only_pick.symbol')
  assertString(value.selection_rule, 'gates_only_pick.selection_rule')
  for (const key of [
    'trend_blocked_pct',
    'closed_trades',
    'reach_rate_pct',
    'passing_count',
    'trades_held',
    'required_trades_one_sample',
    'power_share_pct',
  ]) assertFiniteNumber(value[key], `gates_only_pick.${key}`)
  assertStringArray(
    value.withheld_from_edge_pick_because,
    'gates_only_pick.withheld_from_edge_pick_because',
  )
  if (value.not_an_edge_claim !== true) {
    throw candidacyError('gates_only_pick.not_an_edge_claim')
  }
}

function assertTradeabilityPick(value: unknown): void {
  if (value === null) return
  assertObject(value, 'tradeability_pick')
  assertString(value.symbol, 'tradeability_pick.symbol')
  assertString(value.market, 'tradeability_pick.market')
  assertString(value.metrics_as_of, 'tradeability_pick.metrics_as_of')
  for (const key of ['relative_spread_bps', 'avg_dollar_volume', 'price']) {
    assertFiniteNumber(value[key], `tradeability_pick.${key}`)
  }
  assertBoolean(value.board_lot_uncertain, 'tradeability_pick.board_lot_uncertain')
  if (value.basis !== 'TRADEABILITY_ONLY') {
    throw candidacyError('tradeability_pick.basis')
  }
  if (value.not_an_edge_claim !== true) {
    throw candidacyError('tradeability_pick.not_an_edge_claim')
  }
}

function assertTradeabilityRow(value: unknown, index: number): void {
  const field = `tradeability[${index}]`
  assertObject(value, field)
  assertString(value.symbol, `${field}.symbol`)
  assertString(value.market, `${field}.market`)
  assertString(value.metrics_as_of, `${field}.metrics_as_of`)
  for (const key of ['rank', 'relative_spread_bps', 'avg_dollar_volume', 'price']) {
    assertFiniteNumber(value[key], `${field}.${key}`)
  }
  assertBoolean(value.eligible, `${field}.eligible`)
  assertBoolean(value.board_lot_uncertain, `${field}.board_lot_uncertain`)
  assertStringArray(value.reasons, `${field}.reasons`)
}

export async function getPrimaryCandidacy(
  query: PrimaryCandidacyQuery = {},
): Promise<PrimaryCandidacyResponse> {
  const resp = await api.get(ENDPOINT, { params: query })
  assertObject(resp.data, 'response')
  const data = resp.data
  // A payload claiming it may promote or trade contradicts the endpoint's
  // read-only contract; refuse it rather than render a page that implies the
  // system might act on it.
  if (data.automatic_promotion_allowed !== false || data.order_submission_allowed !== false) {
    throw new Error(
      `Refusing ${ENDPOINT} payload: it claims automatic promotion or order submission is allowed`,
    )
  }
  assertString(data.generated_at, 'generated_at')
  assertString(data.incumbent, 'incumbent')
  assertMember(data.incumbent_status, INCUMBENT_STATUSES, 'incumbent_status')
  assertBoolean(data.switch_enabled, 'switch_enabled')
  assertMember(data.verdict, VERDICTS, 'verdict')
  assertGateParameters(data.gate_parameters)
  assertPoolGate(data.pool_gate)
  assertPower(data.power)
  if (!Array.isArray(data.candidates)) throw candidacyError('candidates')
  data.candidates.forEach(assertCandidate)
  assertEdgePick(data.edge_pick)
  if (data.edge_pick_withheld_reason !== null) {
    assertString(data.edge_pick_withheld_reason, 'edge_pick_withheld_reason')
  }
  assertGatesOnlyPick(data.gates_only_pick)
  assertTradeabilityPick(data.tradeability_pick)
  if (!Array.isArray(data.tradeability)) throw candidacyError('tradeability')
  data.tradeability.forEach(assertTradeabilityRow)
  assertBoolean(data.safety_gate_evaluated, 'safety_gate_evaluated')
  return data as unknown as PrimaryCandidacyResponse
}
