import axios from 'axios'

export const api = axios.create({ baseURL: '', timeout: 10000 })

const apiKey = import.meta.env.VITE_AUTO_TRADE_API_KEY
if (apiKey) {
  api.interceptors.request.use((config) => {
    config.headers['X-API-Key'] = apiKey
    return config
  })
}
