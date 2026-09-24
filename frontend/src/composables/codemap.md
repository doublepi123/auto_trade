# frontend/src/composables/

## Responsibility

20 composables (~1.7k lines) that hold all cross-view shared state and the app's realtime plumbing. No Pinia — the singleton pattern below is the store. Also owns localStorage persistence (9 composables read/write it) and the cross-component "hand-off" channels (symbol requests, view-refresh registration).

## Design

- **Module-level `ref()` singletons.** Shared refs are declared at *module top level*, not inside the composable function; the exported `useX()` just returns them (plus methods). Every caller shares one instance across route changes. Declaring a shared ref inside the function body breaks the singleton — this is the load-bearing convention of the whole frontend.
- **Connection ownership is centralized.** `useConnectionHealth` owns the WebSocket + polling fallback + staleness ticker at the app shell, so the connection survives navigation and the global health badge is correct on every page. `useDashboardData` *delegates* status to it instead of duplicating. Other data-fetching composables (`useAccountRefresh`, `useMarketSession`, `useNotificationBadge`) poll their own endpoint but do not touch the WS.
- **One-shot hand-offs.** `useSymbolStore`: a writer (command palette, pinned-symbols bar) sets `requestedSymbol`; the Dashboard consumes it via `consumeRequestedSymbol()` which reads *and clears*. `useViewRefreshRegistry`: the active view registers its reload fn on mount and clears it on unmount only if it still owns the slot (`useRegisterViewRefresh`), letting the palette's "reload" act on whatever view is mounted — a bridge that `provide/inject` cannot express from child up to shell.
- **Persistence pattern.** Keys are prefixed (`auto_trade.theme.dark`, …). `usePersistedColumns` merges stored overrides onto declared defaults so newly added columns stay visible until explicitly hidden; mutations persist automatically via `watch`.
- **HMR hygiene.** `useConnectionHealth` registers `import.meta.hot.dispose` to tear down WS + timers in dev; polling composables clean up in `onUnmounted`.
- **Cypress-aware.** `useConnectionHealth` detects `window.Cypress` and skips the WebSocket entirely (falls to `polling`), so E2E drives status via stubbed REST only.

## Flow

Realtime path (the important one):

1. `App.vue` mounts → `useConnectionHealth()` → `ensureStarted()` (once) → `connectWebSocket()` to `ws(s)://<host>/ws`.
2. Inbound frames are JSON-validated against `wsStatusMessageSchema` via `utils/validator.ts` (`safeValidate`); invalid payloads are dropped, keeping previous values. `ping`/`pong` frames are ignored. Only `state` is required; every other field coalesces onto the existing `status` ref.
3. Close: auth-rejected codes (1008/4401/4403) latch into permanent `polling`; otherwise exponential backoff reconnect (5s → 60s cap) with `reconnecting` status.
4. Parallel 3s REST poll of `getStatus()` runs always but yields when WS data is <10s fresh (`hasFreshWebSocketStatus`) — polling is a fallback, never a duplicate feed.
5. A 1s ticker derives `ageSeconds` from `lastDataAt` so any page can surface data staleness.
6. Consumers read `status` / `realtimeStatus` (`connecting|connected|reconnecting|polling`) / `connectionLabel` / `connectionTagType`; `reconnectNow()` and `refreshNow()` are wired to header affordances.

Notification path: `useNotificationStream` (mounted in `App.vue`) polls `/api/events` on a shared interval, filters by `useNotificationBadge`-style preferences, and raises `ElNotification`/`ElMessage` toasts + sound for CRITICAL.

## Integration

| Composable | State / job | Consumers |
|---|---|---|
| `useConnectionHealth` | WS + REST fallback status singleton, `ageSeconds`, connection badge | `App`, `Dashboard` |
| `useDashboardData` | strategy config + status bundle for Dashboard (delegates status to health) | `Dashboard` |
| `useSymbolStore` | one-shot requested-symbol hand-off | `Dashboard` (consume), palette/pinned bar (write) |
| `useCommandPalette` | palette `open`/`query`/`activeIndex` singleton | `App`, palette hotkey |
| `useViewRefreshRegistry` | active view's reload fn (register-on-mount) | `Dashboard`, `Reports`, `Watchlist` |
| `usePersistedColumns` | per-view column visibility → localStorage (merge-on-defaults) | `NotificationCenter` (and any table-heavy view) |
| `useTheme` / `useDensity` | dark-mode toggle / Element Plus size, persisted, applied at root | `App` |
| `useRecentPages` | recent routes for palette ranking, persisted | `App` |
| `usePinnedSymbols` | pinned symbol chips → hands off via `useSymbolStore` | `Dashboard` |
| `useMarketSession` | market session clock polling | `App` |
| `useAppVersion` | build-id drift check (5 min) → reload prompt | `App` |
| `useNotificationStream` | event polling → toasts/sound + preferences | `App` |
| `useNotificationBadge` | unread badge count | `App`, `NotificationCenter` |
| `useAccountRefresh` | account info refresh (10s default) | `Dashboard` |
| `useReconciliationStatus` | reconcile gate status + force-resume | `Dashboard` |
| `useDiagnosticsSnapshot` | `/api/diagnostics` funnel snapshot per symbol | `Dashboard`, `Review` |
| `useStatusHistorySeries` | status history query → series | `Dashboard`, `Review` |
| `useMultiSymbolSnapshots` | watchlist snapshots for multi-symbol strip | `Dashboard` |
| `useFormState` | generic dirty/reset form-state helper (typed) | `Strategy` |

Downstream: all fetch through `src/api` clients. `utils/validator.ts` is the schema gate for WS payloads. Views consume composables directly; composables never import views.
