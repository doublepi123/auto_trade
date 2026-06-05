# P39 Wave 1 Symbol Debug Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend Review export so a symbol export contains review summary, symbol-scoped runtime history, and diagnostics snapshot for offline debugging.

**Architecture:** Keep the existing `/api/review/export` entrypoint and export buttons. `ReviewService` will add symbol-scoped runtime history/markers into the export bundle; `api/review.py` will attach a filtered diagnostics snapshot from `get_runner().diagnostics()`. JSON export gets a nested debug bundle; CSV export gets multi-section rows (`review_day`, `history_point`, `history_marker`, `diagnostic_runtime`, `diagnostic_meta`).

**Tech Stack:** Python 3.11, FastAPI, pytest, SQLAlchemy sync ORM, basedpyright.

---

## Task 1: Failing tests
- [x] Add service-level JSON/CSV export tests for runtime history and diagnostics content.
- [x] Add API-level export test proving `/api/review/export?format=json` includes diagnostics for the requested symbol.
- [x] Run focused tests and confirm RED.

## Task 2: Implementation
- [x] Add `get_runtime_history()` to `ReviewService`.
- [x] Extend `export_review()` to include debug bundle for JSON and multi-section CSV.
- [x] Filter diagnostics snapshot to the requested symbol in `api/review.py` and pass it into export.

## Task 3: Verification and roadmap
- [x] Run focused tests plus targeted backend verification.
- [x] Update roadmap to next iteration.
- [x] Report results.

---

## Self-review
- 无占位词。
- 不新增写操作；仅扩展导出内容。
