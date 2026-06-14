import { ref, onMounted, onUnmounted } from 'vue'
import { getStatus } from '../api'
import { resolveApiKey } from '../config/apiKey'
import { object, optionalBoolean, optionalNumber, optionalObject, optionalString, safeValidate, string } from '../utils/validator'
import type { StatusData } from '../types'

// Runtime schema for the WebSocket status payload. The fields mirror the
// keys used in status.value below. ``state`` is required (the only field
// the WS message must carry to be a status update); the rest are
// optional and fall back to whatever value the local ref already holds.
const wsStatusMessageSchema = object({
  type: optionalString,
  state: string,
  // risks is a heterogeneous dict from the server. We do not deep-validate
  // it here — the consumer picks individual keys with `as` casts. A wrong
  // type used to drop the whole frame via safeValidate(); now we just
  // check it's a plain object.
  risks: optionalObject,
  runner_running: optionalBoolean,
  last_price: optionalNumber,
  last_trigger_price: optionalNumber,
  last_trigger_at: optionalString,
  last_action_message: optionalString,
  trading_session_mode: optionalString,
  is_trading_hours: optionalBoolean,
})

type CypressWindow = Window & { Cypress?: unknown }

export function useStatusStream(status: { value: StatusData }) {
  const realtimeStatus = ref<'connecting' | 'connected' | 'reconnecting' | 'polling'>('connecting')

  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let reconnectAttempts = 0
  let useWebSocket = false
  let wsAuthRejected = false
  let statusPollInFlight = false
  let lastWsStatusAt = 0
  const cypressWindow = window as CypressWindow
  const isCypress = Boolean(cypressWindow.Cypress)

  function connectWebSocket() {
    if (isCypress) {
      realtimeStatus.value = 'polling'
      return
    }
    realtimeStatus.value = 'connecting'
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws`
    const apiKey = resolveApiKey()
    ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      if (apiKey) {
        ws?.send(JSON.stringify({ type: 'auth', api_key: apiKey }))
      }
      useWebSocket = true
      realtimeStatus.value = 'connected'
      reconnectAttempts = 0
    }

    ws.onmessage = (event) => {
      try {
        const raw: unknown = JSON.parse(event.data)
        if (typeof raw === 'object' && raw !== null && (raw as { type?: string }).type === 'pong') {
          return
        }
        const data = safeValidate(wsStatusMessageSchema, raw)
        if (data === null) {
          // Validator already logged in DEV. Drop the message silently in
          // production to avoid console spam; the local ref keeps its
          // previous values so the UI remains consistent.
          return
        }
        if (data.state !== undefined) {
          lastWsStatusAt = Date.now()
          realtimeStatus.value = 'connected'
          const risks = (raw as { risks?: Record<string, unknown> })?.risks ?? {}
          status.value = {
            engine_state: data.state,
            paused: (risks.paused as boolean | undefined) ?? status.value.paused,
            kill_switch: (risks.kill_switch as boolean | undefined) ?? status.value.kill_switch,
            runner_running: data.runner_running ?? status.value.runner_running,
            daily_pnl: (risks.daily_pnl as number | undefined) ?? status.value.daily_pnl,
            consecutive_losses:
              (risks.consecutive_losses as number | undefined) ?? status.value.consecutive_losses,
            last_price: data.last_price ?? status.value.last_price,
            last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
            last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
            last_action_message: data.last_action_message ?? status.value.last_action_message,
            trading_session_mode:
              (data.trading_session_mode as StatusData['trading_session_mode'] | undefined)
              ?? status.value.trading_session_mode
              ?? 'ANY',
            is_trading_hours: data.is_trading_hours ?? status.value.is_trading_hours ?? false,
          }
        }
      } catch (exc) {
        console.warn('WebSocket message parse failed:', exc)
      }
    }

    ws.onclose = (event) => {
      useWebSocket = false
      ws = null
      const authRejected = event.code === 1008 || event.code === 4401 || event.code === 4403
      if (authRejected) {
        wsAuthRejected = true
        realtimeStatus.value = 'polling'
        return
      }
      if (wsAuthRejected) {
        realtimeStatus.value = 'polling'
        return
      }
      realtimeStatus.value = 'reconnecting'
      scheduleReconnect()
    }

    ws.onerror = () => {
      useWebSocket = false
      realtimeStatus.value = 'reconnecting'
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
      if (hasFreshWebSocketStatus() || statusPollInFlight) return
      statusPollInFlight = true
      try {
        const st = await getStatus()
        status.value = st
        if (!hasFreshWebSocketStatus()) {
          realtimeStatus.value = 'polling'
        }
      } catch (exc) {
        console.warn('Status polling failed:', exc)
      } finally {
        statusPollInFlight = false
      }
    }, 3000)
  }

  function reconnectNow() {
    if (isCypress) {
      realtimeStatus.value = 'polling'
      return
    }
    // Reset the auth-rejected latch so the next handshake gets a fresh attempt.
    // Without this, a single 1008 (e.g. transient expired key) leaves the user
    // stuck on polling with no way back to the WebSocket path.
    wsAuthRejected = false
    reconnectAttempts = 0
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
    if (isCypress) {
      realtimeStatus.value = 'polling'
    } else {
      connectWebSocket()
    }
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
