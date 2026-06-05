# P40 Wave 1 Global Control Audit Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the affected symbol scope of global control actions in both audit logs and trade events.

**Architecture:** Keep control behavior unchanged. Add one helper in `api/trade.py` that snapshots current control scope from the runner (`primary_symbol`, `affected_symbols`, `runtime_count`, `global_scope=true`) and reuse it across start/stop/pause/resume/kill-switch endpoints. Persist this scope into `AuditLog.request_summary` and a matching `TradeEvent` payload for operator traceability.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy sync ORM, pytest, basedpyright.

---

## Task 1: Failing tests
- [x] Add API tests proving control audit rows include affected symbol scope.
- [x] Add API tests proving control trade events include the same payload.
- [x] Run focused tests and confirm RED.

## Task 2: Implementation
- [x] Add control scope snapshot helper in `backend/app/api/trade.py`.
- [x] Extend control endpoint audit payloads with scope metadata.
- [x] Record corresponding `TradeEvent` rows for control actions.

## Task 3: Verification and roadmap
- [x] Run focused API tests plus targeted backend verification.
- [x] Update roadmap to next iteration.
- [x] Report results.

---

## Self-review
- 无占位词。
- 不改变控制行为，只增强审计与事件追踪。
