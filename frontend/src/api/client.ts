import axios from 'axios'

export const api = axios.create({ baseURL: '', timeout: 10000 })

let _notified401 = false

api.interceptors.request.use((config) => {
  const key = localStorage.getItem('api_key')
  if (key) {
    config.headers['X-API-Key'] = key
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response) {
      const { status } = error.response
      if (status === 401) {
        localStorage.removeItem('api_key')
        if (!_notified401) {
          _notified401 = true
          window.dispatchEvent(new CustomEvent('api-key-required'))
          setTimeout(() => { _notified401 = false }, 1000)
        }
      }
    }
    return Promise.reject(error)
  },
)
