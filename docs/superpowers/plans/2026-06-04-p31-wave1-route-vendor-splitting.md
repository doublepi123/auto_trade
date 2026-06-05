# P31 Wave 1 Route And Vendor Splitting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the production JS bundle below the current Vite chunk warning threshold by lazy-loading routes and splitting heavy vendor chunks.

**Architecture:** Keep runtime behavior unchanged. Add a build-budget check script, lazy-load non-dashboard routes and notification settings, and configure Rollup manual chunks so Element Plus and other heavy dependencies stop shipping as a single mega chunk.

**Tech Stack:** Vue 3, Vue Router 4, Vite 5, Cypress, Node.js.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `frontend/scripts/check-build-chunks.mjs` | Fail when any built JS chunk exceeds the threshold. | Create |
| `frontend/package.json` | Add a reusable chunk-budget verification script. | Modify |
| `frontend/src/router/index.ts` | Lazy-load non-dashboard routes. | Modify |
| `frontend/src/App.vue` | Lazy-load notification settings dialog component. | Modify |
| `frontend/vite.config.ts` | Split vendor chunks with `manualChunks`. | Modify |
| `frontend/cypress/e2e/navigation.cy.ts` | Verify lazy-loaded routes still navigate correctly. | Modify |
| `docs/Roadmap.md` | Mark P31 Wave 1 complete and update next plan. | Modify |

---

## Task 1: Failing chunk-budget test

**Files:**
- Create: `frontend/scripts/check-build-chunks.mjs`
- Modify: `frontend/package.json`

- [x] Add chunk budget script and npm script
- [x] Run `npm run build && node scripts/check-build-chunks.mjs` and confirm RED on current 1.1MB+ JS chunk

---

## Task 2: Implementation

**Files:**
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/vite.config.ts`
- Modify: `frontend/cypress/e2e/navigation.cy.ts`

- [x] Lazy-load non-dashboard routes via dynamic imports
- [x] Lazy-load `NotificationSettings`
- [x] Add `manualChunks` splitting for Element Plus, Vue core, and network deps
- [x] Extend navigation Cypress coverage to lazy-loaded routes

---

## Task 3: Verification and roadmap

**Files:**
- Modify: `docs/Roadmap.md`

- [x] Run focused frontend checks (`type-check`, `build`, chunk-budget script, navigation Cypress)
- [x] Update roadmap to next iteration
- [x] Report results

---

## Self-review

### Spec coverage

- Route splitting: covered.
- Vendor splitting: covered.
- Build threshold enforcement: covered by script.
- Behavior preservation: covered by navigation Cypress.

### Placeholder scan

- 无未定占位词。
- 无待办占位词。
- 无延后实现占位词。
