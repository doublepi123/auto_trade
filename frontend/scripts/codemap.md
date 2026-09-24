# frontend/scripts/

## Responsibility

Post-build chunk-budget guards (plain Node ESM, no dependencies) that keep the
`dist/` bundle honest. Both read `dist/assets/` and must run **after**
`npm run build`.

## Design

| File | Check | Failure mode |
|---|---|---|
| `check-build-chunks.mjs` | No JS chunk in `dist/assets` exceeds 500 KB; also fails if no JS chunks exist (build not run). | `Chunk budget exceeded:` + per-file sizes. |
| `check-element-plus-chunks.mjs` | Count of `el-*.js` chunks ≤ 20. Excess chunks are the signature of manual element-plus splitting in `vite.config.ts`, which breaks because Element Plus internals have circular imports — Rollup must own those boundaries. | `Element Plus chunk count too high:` + listing. |

Run via `npm run build:check-chunks` / `npm run build:check-element-plus`.
Neither script mutates anything; they are read-only assertions suitable for CI.

## Flow

`vite build` → `dist/assets/*.js` → each script scans the directory → exit 0
with a one-line OK summary, or non-zero with the offending files.

## Integration

Configured as npm scripts in `frontend/package.json`; guard the chunking policy
documented in `vite.config.ts` (`manualChunks` returns `undefined` for
element-plus on purpose) and the anti-pattern list in `frontend/AGENTS.md`.
