import axios from 'axios'
import { resolveApiKey } from '../config/apiKey'

export const api = axios.create({ baseURL: '', timeout: 10000 })

const apiKey = resolveApiKey()
if (apiKey) {
  api.interceptors.request.use((config) => {
    config.headers['X-API-Key'] = apiKey
    return config
  })
}
