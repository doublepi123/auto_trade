import { ref, onMounted, onUnmounted } from 'vue'
import { getStatus } from '../api'
import type { StatusData } from '../types'

export function useStatusStream(status: { value: StatusData }) {
  const realtimeStatus = ref<'connecting' | 'connected' | 'reconnecting' | 'polling'>('connecting')

  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let reconnectAttempts = 0
  let useWebSocket = false
  let lastWsStatusAt = 0

  function connectWebSocket() {
    realtimeStatus.value = 'connecting'
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws`
    ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      useWebSocket = true
      realtimeStatus.value = 'connected'
      reconnectAttempts = 0
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'pong') return
        if (data.state !== undefined) {
          lastWsStatusAt = Date.now()
          realtimeStatus.value = 'connected'
          status.value = {
            engine_state: data.state,
            paused: data.risks?.paused ?? status.value.paused,
            kill_switch: data.risks?.kill_switch ?? status.value.kill_switch,
            runner_running: data.runner_running ?? status.value.runner_running,
            daily_pnl: data.risks?.daily_pnl ?? status.value.daily_pnl,
            consecutive_losses: data.risks?.consecutive_losses ?? status.value.consecutive_losses,
            last_price: data.last_price ?? status.value.last_price,
            last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
            last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
          }
        }
      } catch {
        // ignore
      }
    }

    ws.onclose = () => {
      useWebSocket = false
      realtimeStatus.value = 'reconnecting'
      ws = null
      scheduleReconnect()
    }

    ws.onerror = () => {
      useWebSocket = false
      realtimeStatus.value = 'polling'
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return
    realtimeStatus.value = 'reconnecting'
    const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
    reconnectAttempts++
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectWebSocket()
    }, delay)
  }

  function hasFreshWebSocketStatus() {
    return useWebSocket && Date.now() - lastWsStatusAt < 10000
  }

  function startPolling() {
    pollTimer = setInterval(async () => {
      if (hasFreshWebSocketStatus()) return
      try {
        const st = await getStatus()
        status.value = st
        if (!hasFreshWebSocketStatus()) {
          realtimeStatus.value = 'polling'
        }
      } catch {
        // silent
      }
    }, 3000)
  }

  function reconnectNow() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      ws.onclose = null
      ws.close()
      ws = null
    }
    useWebSocket = false
    realtimeStatus.value = 'connecting'
    lastWsStatusAt = 0
    connectWebSocket()
  }

  onMounted(() => {
    connectWebSocket()
    startPolling()
  })

  onUnmounted(() => {
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
  })

  return {
    realtimeStatus,
    reconnectNow,
  }
}
