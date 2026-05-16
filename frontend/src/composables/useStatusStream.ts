import { ref, type Ref } from 'vue'
import { getStatus } from '../api'
import type { StatusData } from '../types'

export type ConnectionMode = 'connecting' | 'websocket' | 'polling' | 'disconnected'

export function useStatusStream(status: Ref<StatusData>) {
  const connectionMode = ref<ConnectionMode>('connecting')

  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let useWebSocket = false
  let reconnectAttempts = 0
  let stopped = false

  function connectWebSocket() {
    if (ws) return

    stopped = false
    connectionMode.value = 'connecting'

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws`
    const socket = new WebSocket(wsUrl)
    ws = socket

    const apiKey = localStorage.getItem('api_key')
    socket.onopen = () => {
      if (ws !== socket) return
      useWebSocket = true
      reconnectAttempts = 0
      connectionMode.value = 'websocket'
      if (apiKey) {
        socket.send(JSON.stringify({ token: apiKey }))
      }
    }

    socket.onmessage = (event) => {
      if (ws !== socket) return
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'pong') return
        if (data.state !== undefined) {
          status.value = {
            engine_state: data.state,
            paused: data.risks?.paused ?? status.value.paused,
            kill_switch: data.risks?.kill_switch ?? status.value.kill_switch,
            daily_pnl: data.risks?.daily_pnl ?? status.value.daily_pnl,
            consecutive_losses: data.risks?.consecutive_losses ?? status.value.consecutive_losses,
            last_price: data.last_price ?? status.value.last_price,
            last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
            last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
          }
        }
      } catch {
        // Ignore malformed stream messages; polling/reconnect keeps status fresh.
      }
    }

    socket.onclose = () => {
      if (ws !== socket) return
      useWebSocket = false
      ws = null
      if (stopped) {
        connectionMode.value = 'disconnected'
        return
      }
      connectionMode.value = 'polling'
      scheduleReconnect()
    }

    socket.onerror = () => {
      if (ws !== socket) return
      useWebSocket = false
      if (!stopped) {
        connectionMode.value = 'polling'
      }
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer || stopped) return
    const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
    reconnectAttempts++
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectWebSocket()
    }, delay)
  }

  function startPolling() {
    if (pollTimer) return
    pollTimer = setInterval(async () => {
      if (useWebSocket) return
      try {
        const st = await getStatus()
        status.value = st
        if (!stopped && connectionMode.value !== 'connecting') {
          connectionMode.value = 'polling'
        }
      } catch {
        // Non-fatal fallback failure; the next poll or WebSocket reconnect can recover.
      }
    }, 3000)
  }

  function stop() {
    stopped = true
    useWebSocket = false
    connectionMode.value = 'disconnected'
    if (ws) {
      ws.onclose = null
      ws.close()
      ws = null
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  return { connectionMode, connectWebSocket, startPolling, stop }
}
