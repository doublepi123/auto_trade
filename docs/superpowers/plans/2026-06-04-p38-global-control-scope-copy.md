# P38 Global Control Scope Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Dashboard clearly state that start/stop/pause/kill-switch controls are global and affect all tracked symbol runtimes.

**Architecture:** Keep behavior unchanged. Add explicit copy in the control panel, risk summary, and diagnostics summary so operators understand global scope before using controls.

**Tech Stack:** Vue 3, Element Plus, Cypress, Vite.

---

## Task 1: Failing test
- [x] Add Dashboard Cypress assertion for global control scope copy and runtime count
- [x] Run focused Cypress and confirm RED

## Task 2: Implementation
- [x] Add global-scope explanatory copy to Dashboard control panel
- [x] Add diagnostics runtime count summary
- [x] Clarify risk section labels as global

## Task 3: Verification and roadmap
- [x] Run focused Cypress + frontend build checks
- [x] Update roadmap with next iteration order
- [x] Report results

---

## Self-review
- 无占位词。
- 不改变控制行为，只改变运维可见文案。
