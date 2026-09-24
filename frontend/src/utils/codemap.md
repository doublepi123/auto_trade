# frontend/src/utils/

## Responsibility

Nine dependency-free leaf helpers (~630 lines total) shared by views,
composables, and `App.vue`: display copy, formatting, CSV export, clipboard,
error extraction, runtime payload validation, time labels, enums, and the
app-version manifest fetcher. Nothing here imports from `views/`, `api/`, or
`components/` — utils are the bottom of the import graph.

## Design

| File | Role |
|---|---|
| `labels.ts` | **Single source of Chinese enum copy** — `engineStateLabel`, `marketLabel`, `orderStatusLabel`, `skipCategoryLabel`, … New UI copy for backend enums must go here, never hardcoded in components (only ~8 consumers today). |
| `format.ts` | Number/currency formatting: `currencySymbol` (`USD → $`, `HKD → HK$`, `CNY/CNH → CN¥`; blank code → `''`, never a guessed symbol), market-aware currency strings, null-safe so template bindings never throw. |
| `time.ts` | Compact Chinese relative-time labels (`relativeTime`/`relativeAgeLabel`: `刚刚`, `3s前`, `2m前`, `1h前`, `3d前`) and `ageFreshnessClass` for data-freshness coloring; input clamped to ≥ 0. |
| `validator.ts` | `defineValidator` — tiny runtime type-validators for API response shapes; returns the narrowed object or a `ValidationError` listing every discrepancy. Used by `useConnectionHealth` to validate WS payloads before they reach state. Deliberately no zod/valibot. |
| `csv.ts` | `buildCsv` — client-side CSV serialization of already-loaded rows: RFC 4180 quoting, formula-injection guard (cells starting `= + - @` get a `'` prefix). No backend round-trip. |
| `clipboard.ts` | `copyText` — async Clipboard API with `execCommand` fallback for plain-http LAN deployments; never throws, returns boolean. |
| `error.ts` | `resolveErrorMessage` — best-effort extraction of a human-readable message from Axios-like errors (`response.data.detail`, including FastAPI validation arrays), with a caller-supplied fallback. |
| `constants.ts` | Frozen const maps for cross-app event/order/runner status strings (`EVENT_TYPE`, `ORDER_STATUS`, `RUNNER_STATUS`, `PROMISE_STATUS`). |
| `appVersion.ts` | `parseAppBuildId` + fetch helper for `/version.json` (emitted by the Vite version-manifest plugin) — powers the `useAppVersion` stale-deployment watchdog; strict shape/length checks on `build_id`. |

## Flow

Views/composables call these directly at render or event-handler time; they are
pure functions over their arguments (no Vue reactivity, no module state).

## Integration

- `labels.ts` ← every view needing enum copy (rule-enforced by convention, see
  `frontend/AGENTS.md` anti-patterns).
- `validator.ts` ← `composables/useConnectionHealth.ts`.
- `appVersion.ts` ← `composables/useAppVersion.ts`; pairs with
  `__AUTO_TRADE_BUILD_ID__` from `src/env.d.ts` and `vite.config.ts`.
- `time.ts`, `error.ts`, `format.ts` ← `App.vue` health badge and most views.
- `csv.ts` ← views with export buttons (`utils/csv.ts` keeps exports
  dependency-free so any view can serialize without a backend call).
