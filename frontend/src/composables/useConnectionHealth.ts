import { computed, ref } from 'vue'
import { getStatus } from '../api'
import {
  object,
  optionalBoolean,
  optionalNumber,
  optionalObject,
  optionalString,
  safeValidate,
  string,
} from '../utils/validator'
import type { StatusData } from '../types'

// Runtime schema for the WebSocket status payload. ``state`` is the only field a
// WS message must carry; the rest are optional and coalesce onto whatever the
// local ref already holds. Mirrors the previous useStatusStream contract.
const wsStatusMessageSchema = object({
  type: optionalString,
  state: string,
  risks: optionalObject,
  runner_running: optionalBoolean,
  last_price: optionalNumber,
  last_trigger_price: optionalNumber,
  last_trigger_at: optionalString,
  last_action_message: optionalString,
  trading_session_mode: optionalString,
  is_trading_hours: optionalBoolean,
  execution_state: optionalString,
  reduction_reason: optionalString,
  reduction_started_at: optionalString,
})

type RealtimeStatus = 'connecting' | 'connected' | 'reconnecting' | 'polling'
type CypressWindow = Window & { Cypress?: unknown }

export const defaultStatus: StatusData = {
  engine_state: 'flat',
  paused: false,
  kill_switch: false,
  protective_exit_permitted: false,
  runner_running: false,
  daily_pnl: 0,
  consecutive_losses: 0,
  last_price: 0,
  last_trigger_price: 0,
  last_trigger_at: null,
  last_action_message: '',
  trading_session_mode: 'ANY',
  is_trading_hours: true,
  execution_state: 'IDLE',
  reduction_reason: '',
  reduction_started_at: null,
}

// ---------------------------------------------------------------------------
// Module-level singletons.
//
// The realtime connection is owned here, at the app shell, rather than in the
// Dashboard component. This keeps the WebSocket alive across navigation so the
// global health badge reflects the true connection state on every page (not
// only when the Dashboard happens to be mounted). Every caller of
// `useConnectionHealth()` shares these same refs.
// ---------------------------------------------------------------------------
const status = ref<StatusData>({ ...defaultStatus })
const realtimeStatus = ref<RealtimeStatus>('connecting')
// Epoch ms of the last *fresh* status observation — either an inbound WS
// message or a successful REST poll/manual refresh. `ageSeconds` is derived
// from this on a 1s tick so the UI can surface data staleness.
const lastDataAt = ref(0)
const ageSeconds = ref(0)

let started = false
let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let tickerTimer: ReturnType<typeof setInterval> | null = null
let reconnectAttempts = 0
let useWebSocket = false
let wsAuthRejected = false
let statusPollInFlight = false

const cypressWindow = typeof window !== 'undefined' ? (window as CypressWindow) : null
const isCypress = Boolean(cypressWindow?.Cypress)

function markFresh(): void {
  lastDataAt.value = Date.now()
}

function connectWebSocket(): void {
  if (isCypress || !cypressWindow) {
    realtimeStatus.value = 'polling'
    return
  }
  realtimeStatus.value = 'connecting'
  const protocol = cypressWindow.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${cypressWindow.location.host}/ws`
  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    useWebSocket = true
    realtimeStatus.value = 'connected'
    reconnectAttempts = 0
  }

  ws.onmessage = (event) => {
    try {
      const raw: unknown = JSON.parse(event.data)
      if (
        typeof raw === 'object' &&
        raw !== null &&
        (raw as { type?: string }).type === 'pong'
      ) {
        return
      }
      const data = safeValidate(wsStatusMessageSchema, raw)
      if (data === null) {
        // Validator logs in DEV; drop silently in prod, keep previous values.
        return
      }
      if (data.state !== undefined) {
        realtimeStatus.value = 'connected'
        const risks = (raw as { risks?: Record<string, unknown> })?.risks ?? {}
        status.value = {
          engine_state: data.state,
          paused: (risks.paused as boolean | undefined) ?? status.value.paused,
          kill_switch: (risks.kill_switch as boolean | undefined) ?? status.value.kill_switch,
          protective_exit_permitted:
            (risks.protective_exit_permitted as boolean | undefined) ??
            status.value.protective_exit_permitted,
          runner_running: data.runner_running ?? status.value.runner_running,
          daily_pnl: (risks.daily_pnl as number | undefined) ?? status.value.daily_pnl,
          consecutive_losses:
            (risks.consecutive_losses as number | undefined) ?? status.value.consecutive_losses,
          last_price: data.last_price ?? status.value.last_price,
          last_trigger_price: data.last_trigger_price ?? status.value.last_trigger_price,
          last_trigger_at: data.last_trigger_at ?? status.value.last_trigger_at,
          last_action_message: data.last_action_message ?? status.value.last_action_message,
          trading_session_mode:
            (data.trading_session_mode as StatusData['trading_session_mode'] | undefined) ??
            status.value.trading_session_mode ??
            'ANY',
          is_trading_hours: data.is_trading_hours ?? status.value.is_trading_hours ?? false,
          execution_state:
            (data.execution_state as StatusData['execution_state'] | undefined) ??
            status.value.execution_state,
          reduction_reason: data.reduction_reason ?? status.value.reduction_reason,
          reduction_started_at:
            data.reduction_started_at ?? status.value.reduction_started_at,
        }
        markFresh()
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

function scheduleReconnect(): void {
  if (reconnectTimer) return
  realtimeStatus.value = 'reconnecting'
  const delay = Math.min(5000 * Math.pow(2, reconnectAttempts), 60000)
  reconnectAttempts++
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connectWebSocket()
  }, delay)
}

function hasFreshWebSocketStatus(): boolean {
  return useWebSocket && Date.now() - lastDataAt.value < 10000
}

function startPolling(): void {
  pollTimer = setInterval(async () => {
    if (hasFreshWebSocketStatus() || statusPollInFlight) return
    statusPollInFlight = true
    try {
      const st = await getStatus()
      status.value = st
      markFresh()
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

function startTicker(): void {
  tickerTimer = setInterval(() => {
    if (lastDataAt.value === 0) {
      ageSeconds.value = 0
      return
    }
    ageSeconds.value = Math.max(0, Math.round((Date.now() - lastDataAt.value) / 1000))
  }, 1000)
}

function reconnectNow(): void {
  if (isCypress || !cypressWindow) {
    realtimeStatus.value = 'polling'
    return
  }
  // Reset the auth-rejected latch so the next handshake gets a fresh attempt.
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
  lastDataAt.value = 0
  ageSeconds.value = 0
  connectWebSocket()
}

function ensureStarted(): void {
  if (started) return
  started = true
  if (isCypress) {
    realtimeStatus.value = 'polling'
  } else {
    connectWebSocket()
  }
  startPolling()
  startTicker()
}

/** Force a status refresh from REST (used by manual "refresh" affordances). */
async function refreshNow(): Promise<void> {
  if (statusPollInFlight) return
  statusPollInFlight = true
  try {
    status.value = await getStatus()
    markFresh()
  } catch (exc) {
    console.warn('Status refresh failed:', exc)
  } finally {
    statusPollInFlight = false
  }
}

const connectionLabel = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected':
      return '实时连接正常'
    case 'reconnecting':
      return '实时重连中'
    case 'polling':
      return '轮询兜底'
    default:
      return '实时连接中'
  }
})

const connectionTagType = computed(() => {
  switch (realtimeStatus.value) {
    case 'connected':
      return 'success'
    case 'reconnecting':
      return 'warning'
    case 'polling':
      return 'info'
    default:
      return 'info'
  }
})

// HMR teardown: prevent accumulation of WebSocket connections / timers in dev.
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    started = false
    if (ws) {
      ws.onclose = null
      ws.onmessage = null
      ws.onerror = null
      ws.close()
      ws = null
    }
    useWebSocket = false
    wsAuthRejected = false
    reconnectAttempts = 0
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    if (tickerTimer) {
      clearInterval(tickerTimer)
      tickerTimer = null
    }
  })
}

export function useConnectionHealth() {
  ensureStarted()
  return {
    status,
    realtimeStatus,
    lastDataAt,
    ageSeconds,
    connectionLabel,
    connectionTagType,
    reconnectNow,
    refreshNow,
    markFresh,
  }
}
