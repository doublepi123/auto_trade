import { api } from './client'
import type {
  AlertRule,
  AlertRuleCreate,
  AlertRulePage,
  AlertEvaluateResult,
} from '../types'

export async function listAlertRules(enabled?: boolean): Promise<AlertRulePage> {
  const resp = await api.get('/api/alert-rules', {
    params: enabled === undefined ? {} : { enabled },
  })
  return resp.data
}

export async function createAlertRule(payload: AlertRuleCreate): Promise<AlertRule> {
  const resp = await api.post('/api/alert-rules', payload)
  return resp.data
}

export async function updateAlertRule(id: number, payload: AlertRuleCreate): Promise<AlertRule> {
  const resp = await api.put(`/api/alert-rules/${id}`, payload)
  return resp.data
}

export async function deleteAlertRule(id: number): Promise<void> {
  await api.delete(`/api/alert-rules/${id}`)
}

export async function evaluateAlertRules(): Promise<AlertEvaluateResult> {
  const resp = await api.post('/api/alert-rules/evaluate')
  return resp.data
}
