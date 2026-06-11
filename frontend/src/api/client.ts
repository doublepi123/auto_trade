import axios from 'axios'
import { resolveApiKey } from '../config/apiKey'

export const api = axios.create({ baseURL: '', timeout: 10000 })

api.interceptors.request.use((config) => {
  const apiKey = resolveApiKey()
  if (apiKey) {
    config.headers['X-API-Key'] = apiKey
  }
  return config
})
