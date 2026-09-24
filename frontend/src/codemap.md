# frontend/src/

## Responsibility

The SPA source tree: bootstrap, shell layout, and every feature module
(views, api clients, composables, router, types, utils, styles). ~46k lines;
`views/` dominates (~32k across 65 pages).

## Design

Bootstrap (`main.ts`): `createApp(App)` → `app.use(router)` → mount. Before
mounting, `initializeTheme()` applies the persisted dark/light choice, and two
global stylesheets load: `element-plus/theme-chalk/dark/css-vars.css` (dark-mode
CSS variables — the auto-import resolver only injects base styles) and
`./styles/theme.css`.

`App.vue` is the shell, not a routed page: desktop/mobile navigation with
router-links and an unread-notification badge, and the app-wide chrome —
- command palette (`useCommandPalette` + `CommandPalette.vue`, `data-testid="nav-command-palette"`),
- theme toggle (`useTheme`, `isDark`/`toggleTheme`),
- density control (`useDensity`, feeding `el-config-provider :size`),
- market session clock (`useMarketSession`),
- realtime health badge (`useConnectionHealth` — booted here, not in Dashboard,
  so the badge is accurate on every page; degrades WS → polling),
- live notification stream + unread badge (`useNotificationStream`,
  `useNotificationBadge`),
- app-version watchdog (`useAppVersion` → prompts reload when `version.json`
  reports a newer `build_id` than `__AUTO_TRADE_BUILD_ID__`),
- recent-page tracking for the palette (`useRecentPages`).

`env.d.ts`: `vite/client` reference plus the `__AUTO_TRADE_BUILD_ID__` global;
`ImportMetaEnv` is intentionally empty (no VITE_* secrets exist).

`styles/theme.css`: the only stylesheet — light/dark palettes as CSS variables
(`:root` / `html.dark`, chart surface/axis/marker tokens) plus base layout.
No codemap; it is a single flat CSS file consumed by `main.ts`.

No Pinia: cross-page state lives in composables as module-level `ref()`
singletons.

## Flow

`main.ts` mounts `App.vue`; `App.vue` hosts `<router-view>` so `router/` decides
which `views/*.vue` renders; views fetch through `api/*` (axios clients over the
proxy), subscribe to realtime state via `composables/*`, and render with
`components/*` + Element Plus. Types come from `types/index.ts`, display copy
and formatting from `utils/*`.

## Integration

Subfolder deep-dives (each has its own `codemap.md` except `styles/`):

| Folder | Role |
|---|---|
| `api/` | One axios client file per backend domain; `client.ts` is the only `axios.create` |
| `components/` | Shared presentational components (DataState, MetricStat, SVG charts, palette…) |
| `composables/` | Module-level `ref()` singleton state (connection health, theme, notifications…) |
| `router/` | Flat hash-history route table, 65 lazy routes |
| `types/` | Single `index.ts` (~3.2k lines) of shared interfaces |
| `utils/` | 9 leaf helpers: labels, format, time, csv, validator, clipboard, … |
| `views/` | 65 routed pages: 13 core operating + ~51 read-only analytics |
| `styles/` | `theme.css` only — light/dark CSS variables + base layout (no codemap) |
