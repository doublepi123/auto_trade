# frontend/

## Responsibility

Vue 3 + TypeScript SPA that operates and observes the auto_trade backend: 13 core
operating pages (Dashboard, Watchlist, Review, Strategy, History, …) plus ~51
single-purpose read-only analytics views. Served by nginx in Docker; talks to the
FastAPI backend exclusively through `/api` and `/ws`.

## Design

- **Stack** (`package.json`): Vue 3.5, vue-router 4 (hash history), Element Plus
  2.8, axios. No Pinia, no chart library — charts are hand-written SVG. Dev-only:
  Vite 6, vue-tsc, Cypress 15, `unplugin-auto-import` + `unplugin-vue-components`
  (Element Plus components/styles are auto-imported via resolvers, `dts: false`).
- **Build** (`npm run build` = `vue-tsc && vite build`, so type check gates every
  build; `npm run type-check` runs vue-tsc alone).
- **vite.config.ts**: dev server on `:3000` proxying `/api` → `localhost:8000` and
  `/ws` → `ws://localhost:8000` (ws: true); when `AUTO_TRADE_API_KEY` is set in
  env, the proxy injects the `X-API-Key` header **server-side**, so the browser
  never holds the key. There is deliberately **no manual chunking for
  element-plus** — its internals have circular imports and Rollup must own those
  boundaries; other vendors get explicit chunks (`vue-core`, `vue-router`,
  `network`, `el-icons`). A small plugin emits `version.json` carrying a
  per-build `build_id`, also inlined as `__AUTO_TRADE_BUILD_ID__` (declared in
  `src/env.d.ts`) for the stale-deployment detector (`useAppVersion`).
- **Chunk guards**: `scripts/check-build-chunks.mjs` fails if any dist JS chunk
  exceeds 500 KB; `scripts/check-element-plus-chunks.mjs` fails if more than 20
  `el-*.js` chunks appear (proxy for accidental manual splitting). Run via
  `npm run build:check-chunks` / `build:check-element-plus`.
- **Dockerfile**: two stages — `node:20-alpine` runs `npm ci && npm run build`,
  then `nginx:alpine` ships `dist/`, `nginx.conf`, and `docker-entrypoint.sh`
  (port 80, wget healthcheck).
- **Runtime key injection**: `nginx.conf` proxies `/api/` and `/ws` to
  `http://backend:8000` with `proxy_set_header X-API-Key
  "__AUTO_TRADE_PROXY_API_KEY__"`. `docker-entrypoint.sh` sed-replaces that
  placeholder with `$AUTO_TRADE_API_KEY` (rejecting newline-containing values,
  escaping for nginx + sed) before starting nginx — same server-side-injection
  model as the dev proxy. `/assets/` is immutable-cached; `index.html` is
  no-cache; SPA fallback via `try_files ... /index.html`; WS upgrade headers for
  `/ws`.
- **public/runtime-config.js**: currently a reserved placeholder for non-secret
  runtime frontend config, shipped as-is into the image.
- **Cypress** (mention only): E2E specs under `cypress/e2e/`, API fully stubbed
  via `cy.stubApi()` in `cypress/support/e2e.ts`; `baseUrl` defaults to the
  Docker stack at `http://localhost:8080`. See `frontend/AGENTS.md`.

## Flow

Dev: browser → Vite `:3000` → proxy (injects `X-API-Key`) → backend `:8000`.
Prod: browser → nginx `:80` (static `dist/`, SPA fallback) → `/api`|`/ws`
proxied to the `backend` compose service with the entrypoint-injected key.
Build flow: `vue-tsc` type-check → `vite build` → `dist/` → chunk-guard scripts
(optional, separate npm scripts).

## Integration

- Backend contract: REST + WS under `/api` and `/ws`; the SPA never sees or
  sends the API key — both dev proxy and nginx inject it.
- Compose: this image is the `frontend` service; `backend:8000` hostname comes
  from the shared Docker network; `AUTO_TRADE_API_KEY` is a required compose
  variable.
- Subfolder deep-dives:

| Folder | Scope |
|---|---|
| `src/` | Application source — see `src/codemap.md` |
| `scripts/` | Post-build chunk-budget guards — see `scripts/codemap.md` |
| `cypress/` | E2E specs + centralized stubs (see `frontend/AGENTS.md`) |
