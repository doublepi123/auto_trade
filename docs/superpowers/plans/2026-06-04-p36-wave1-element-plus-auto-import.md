# P36 Wave 1 Element Plus Auto-import And Chunk Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop shipping dozens of per-component Element Plus chunks and reduce build-time circular chunk noise without changing UI behavior.

**Architecture:** Replace global `app.use(ElementPlus)` registration with auto-imported Element Plus components/APIs through Vite plugins, keep CSS imported on demand, and simplify manual chunking so Element Plus resolves to a single shared vendor chunk plus icons.

**Tech Stack:** Vue 3, Vite 5, Element Plus 2.8, unplugin-auto-import, unplugin-vue-components, Cypress.

---

## Task 1: Failing build check
- [x] Add script that fails when build emits too many `el-*.js` chunks.
- [x] Run build + script and confirm RED on current dozens of Element Plus chunks.

## Task 2: Implementation
- [x] Install/configure Element Plus auto-import plugins.
- [x] Remove global `ElementPlus` plugin usage from `main.ts`.
- [x] Simplify Element Plus manual chunking to a single `el-core` vendor chunk.
- [x] Keep existing Cypress navigation/dashboard coverage green.

## Task 3: Verification and roadmap
- [x] Run frontend type-check, build, chunk checks, and focused Cypress.
- [x] Update roadmap to next iteration.
- [x] Report results.

---

## Self-review
- 无占位词。
- 不改变页面行为，只改变依赖装配和打包结构。
