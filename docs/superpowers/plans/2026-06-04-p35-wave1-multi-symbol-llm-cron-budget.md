# P35 Wave 1 Multi-symbol LLM Cron Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the periodic LLM cron analyze more than the primary symbol, capped by per-cycle and per-hour budgets.

**Architecture:** Keep primary strategy config as the only writable interval target. The cron derives candidate symbols from the runner’s symbol runtimes, prioritizes the primary symbol, enforces `llm_max_symbols_per_cycle` and `llm_max_analyses_per_hour`, applies interval suggestions only for the primary symbol, and executes secondary order actions via the existing symbol-aware `execute_llm_order_decision()` path.

**Tech Stack:** Python 3.11, asyncio, pytest, basedpyright.

---

## Task 1: Failing tests
- [x] Add focused `test_main.py` coverage for per-cycle symbol cap and secondary-symbol order execution.
- [x] Run focused tests and confirm RED.

## Task 2: Implementation
- [x] Add helper(s) in `main.py` to derive candidate runtimes and prune hourly budget timestamps.
- [x] Update `_llm_analysis_tick()` to iterate eligible symbols under budgets.
- [x] Keep direct suggestion apply primary-only; secondary symbols only emit order actions/events.

## Task 3: Verification and roadmap
- [x] Run focused main tests + targeted backend verification.
- [x] Update roadmap to the next iteration order.
- [x] Report results.

---

## Self-review
- 无占位词。
- 预算执行与实际交易执行分离：本轮只改 cron 分析分发，不改手动/单次 analyze API。
