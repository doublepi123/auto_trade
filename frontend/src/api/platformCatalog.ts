import { api } from './client'

export interface ModuleSummary {
  name: string
  category: string
  description: string
  function_count: number
  class_count: number
}

export interface ModuleDetail {
  name: string
  category: string
  description: string
  functions: { name: string; args: string; return_type: string }[]
  classes: string[]
  imports: string[]
  line_count: number
}

export interface CategoryCount {
  category: string
  count: number
}

export async function getModules(params: { category?: string; search?: string } = {}): Promise<ModuleSummary[]> {
  const resp = await api.get('/api/platform-catalog/modules', { params })
  return resp.data
}

export async function getModuleDetail(name: string): Promise<ModuleDetail> {
  const resp = await api.get(`/api/platform-catalog/modules/${encodeURIComponent(name)}`)
  return resp.data
}

export async function getCategories(): Promise<CategoryCount[]> {
  const resp = await api.get('/api/platform-catalog/categories')
  return resp.data
}
