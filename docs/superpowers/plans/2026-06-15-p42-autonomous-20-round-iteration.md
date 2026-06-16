# P42 Autonomous 20-Round Iteration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute 20 autonomous review-fix / enhancement rounds, each producing a self-contained, verifiable, rollback-able change set, driven by subagent fan-out and orchestrated by the main agent.

**Architecture:** Main agent acts as orchestrator. It does NOT write implementation code directly. For each round, the orchestrator:
1. (Review rounds) Dispatches read-only subagents in parallel across dimensions; collects and de-duplicates findings.
2. (Implementation rounds) Picks a single highest-value finding/sub-topic; dispatches a subagent constrained by `pathspec` to implement the change + tests.
3. (Verify) Runs the relevant test subset and static checks. If green, advances to next round. If red, dispatches a fix subagent.
4. (Sentinel) Every 5 rounds, dispatches an adversarial cross-checker to ensure no regression in: pytest pass count, basedpyright 0/0/0, vue-tsc clean, build pass, chunk budget hold, Cypress pass for touched specs.

**Tech Stack:** Python 3.11+ (project requirement; host is 3.13.5, must use project `.venv` or a 3.11 venv for tests), FastAPI, SQLAlchemy 2.0, Pydantic v2, Vue 3.5, TypeScript strict, Element Plus, Cypress, pytest, basedpyright, vulture, pyflakes.

**Scope:** 20 rounds. Rounds 1/6/17 are full-project review scans. Rounds 2–5 / 7–15 / 18–19 are implementation or hardening rounds. Round 16 is dead-code / docs. Round 20 is final summary + Roadmap update.

**Git rule:** Do NOT commit unless user explicitly asks. "Checkpoint" = inspect `git diff` / `git status`. All work is performed in a worktree; only fast-forwarded at the end if user requests.

---

## File Structure

This is a **multi-file, cross-stack** plan. The "files" are not pre-known — each round's implementation subagent is given a tightly-scoped allow-list based on its finding. What is fixed in this plan:

- Modify: `docs/Roadmap.md` — add P42 row to the completed list and a new "P42 20 轮自主迭代" section summarizing the deliverables.
- Create: `docs/superpowers/review/p42-round-NN-findings.md` — per-round finding logs (read-only record).
- Create: `docs/superpowers/review/p42-final-summary.md` — round 20 final summary.
- No production code is touched without a per-round allow-list.

The orchestrator's working memory:
- `state/p42-touched-files.txt` — cumulative list of files touched (used for ping-pong detection).
- `state/p42-round-state.json` — round index, status, last green hashes.

---

## Pre-Plan: Environment Setup (one-time before Round 1)

**Files:** none (orchestrator-only)

- [ ] **Step 0.1: Verify project virtual environment**

Run:
```bash
cd /home/lcy/code/auto_trade/backend
ls .venv/bin/python 2>/dev/null && .venv/bin/python --version
```

Expected: `.venv/bin/python` exists and version starts with `3.11` or `3.12`. If absent or wrong version, create:
```bash
cd /home/lcy/code/auto_trade/backend
python3.11 -m venv .venv 2>/dev/null || python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python --version
```

If neither 3.11 nor 3.12 is available on host, fall back to host Python (3.13) and document this in the final summary as a known limitation; subagents will use `python3 -m pytest` directly.

- [ ] **Step 0.2: Capture baseline metrics**

Run:
```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3
.venv/bin/python -m basedpyright 2>&1 | tail -3
cd /home/lcy/code/auto_trade/frontend
npm run type-check 2>&1 | tail -3
npm run build 2>&1 | tail -5
```

Expected output saved into `state/p42-baseline.txt` (create the directory if needed). Do not proceed if any baseline is failing.

- [ ] **Step 0.3: Initialize orchestrator state**

Run:
```bash
mkdir -p /home/lcy/code/auto_trade/state /home/lcy/code/auto_trade/docs/superpowers/review
echo "0" > /home/lcy/code/auto_trade/state/p42-current-round.txt
: > /home/lcy/code/auto_trade/state/p42-touched-files.txt
```

Expected: directories exist, files created.

- [ ] **Step 0.4: Set up worktree**

Run:
```bash
cd /home/lcy/code/auto_trade
git worktree add ../auto_trade-p42 -b feature/p42-autonomous-20-round main
```

Expected: worktree created at `../auto_trade-p42`. All subsequent rounds operate inside this worktree; the main checkout remains untouched.

---

## Round 1: Full-Project Review (read-only fan-out)

**Files:**
- Create: `docs/superpowers/review/p42-round-01-findings.md`
- Create: `state/p42-round-01-raw.md`

- [ ] **Step 1.1: Dispatch 5 review subagents in parallel**

Dispatch the following subagents in **one** tool message (5 parallel `Agent` calls with `subagent_type: Explore`). Each must return a JSON array of findings:

| Dimension | Prompt summary |
|-----------|----------------|
| **backend-correctness** | Look at `backend/app/services/`, `backend/app/core/`, `backend/app/runner.py`. Find: off-by-one, wrong sign, None/zero guards, wrong rounding, race conditions, stale state. Output: `[{file, line, severity, finding, fix_sketch}]`. |
| **backend-resilience** | Look at `backend/app/core/broker.py`, `backend/app/services/trade_execution_service.py`, `backend/app/services/llm_advisor_service.py`. Find: uncaught exceptions, missing retries, swallowed errors, missing transactional boundaries. |
| **frontend-ux** | Look at `frontend/src/views/`, `frontend/src/components/`, `frontend/src/composables/`. Find: missing loading states, a11y gaps, mobile overflow, type contract drift, brittle selectors. |
| **test-coverage** | Look at `backend/tests/`, `frontend/cypress/e2e/`. Find: uncovered branches, flaky time/timezone, missing edge cases for guard rails (session mode, RTH, kill switch, multi-symbol pending). |
| **security-perf** | Look at `backend/app/api/`, `backend/app/core/`, `backend/app/services/`. Find: missing input validation, sensitive data in logs, N+1 queries, hot-path inefficiencies, unclosed resources. |

Each subagent is read-only. Schema for return:
```json
{
  "findings": [
    {"file": "path/to/file.py", "line": 123, "severity": "P0|P1|P2|P3", "finding": "string", "fix_sketch": "string"}
  ]
}
```

- [ ] **Step 1.2: Collect and de-duplicate findings**

Read all 5 subagent outputs. Build a `findings.json` keyed by `(file, line)`. Drop duplicates. Save to `state/p42-round-01-raw.md`.

- [ ] **Step 1.3: Score and rank**

For each finding, assign: severity (from subagent), reachability (how many tests cover it), blast radius (single file vs cross-cutting), fix cost (S/M/L). Sort: P0 first, then by blast-radius-ascending, fix-cost-ascending.

- [ ] **Step 1.4: Save final findings**

Write the ranked list to `docs/superpowers/review/p42-round-01-findings.md` with sections: P0, P1, P2, P3. The P0 list becomes the input to Round 2.

- [ ] **Step 1.5: Checkpoint**

Run:
```bash
git status --short docs/superpowers/review/p42-round-01-findings.md state/p42-round-01-raw.md
echo "1" > state/p42-current-round.txt
```

Expected: 2 new files, state updated. No production code touched.

---

## Round 2: Data-Truth + Dead-Code (P0 from Round 1)

**Files:** determined per-finding; allow-list in subagent prompt.

- [ ] **Step 2.1: Select top 1–2 P0 from Round 1 findings**

Pick the highest-priority data-truth (e.g., synthesized K-line, fake LLM context) and the highest-priority dead-code (e.g., unused import, unused function, dead branch). Cap at 2 fixes per round.

- [ ] **Step 2.2: Dispatch 1–2 implementation subagents**

For each selected finding, dispatch an `Agent` (general-purpose) with:
- `pathspec` allow-list: only the listed file(s) and their test files.
- Required output: passing pytest for the touched module + the relevant basedpyright zone = 0.
- Forbidden: any cross-file refactor; any commit.

- [ ] **Step 2.3: Verify**

Run the touched module's pytest:
```bash
cd backend && .venv/bin/python -m pytest tests/<touched> -v
.venv/bin/python -m basedpyright 2>&1 | tail -3
```

Expected: tests pass, no new pyright errors.

- [ ] **Step 2.4: Update touched-files list**

```bash
echo "round-02: <files>" >> state/p42-touched-files.txt
```

- [ ] **Step 2.5: Checkpoint**

```bash
git diff --stat
```

Expected: only the allow-listed files changed.

---

## Round 3: Frontend Consistency

**Files:** determined per-finding (frontend).

- [ ] **Step 3.1: Pick top 1 P1 from Round 1 frontend-ux findings**

Cap at 1 frontend fix per round (changes are riskier in TS strict + Cypress).

- [ ] **Step 3.2: Dispatch implementation subagent**

Same pattern as Round 2, but allow-list is under `frontend/src/`. Required: `npm run type-check` clean + `npm run build` pass + the relevant Cypress spec green.

- [ ] **Step 3.3: Verify**

```bash
cd frontend && npm run type-check && npm run build
```

- [ ] **Step 3.4: Update state**

```bash
echo "round-03: <files>" >> state/p42-touched-files.txt
```

---

## Round 4: Backend Resilience

**Files:** determined per-finding (broker / trade_execution / llm_advisor).

- [ ] **Step 4.1: Pick top 1 P1 from Round 1 backend-resilience findings**

- [ ] **Step 4.2: Dispatch implementation subagent**

Allow-list: only the service file + its test file.

- [ ] **Step 4.3: Verify**

```bash
cd backend && .venv/bin/python -m pytest tests/<touched> -v
.venv/bin/python -m basedpyright 2>&1 | tail -3
```

---

## Round 5: Test Hardening

**Files:** determined per-finding (test files only or service+test).

- [ ] **Step 5.1: Pick top 1 P2 test-coverage finding (highest reachability gap)**

- [ ] **Step 5.2: Dispatch test-writing subagent**

Allow-list: only the test file (or test + service if a small production change is needed for testability). Required: pytest +N for the new tests; no regression in full suite.

- [ ] **Step 5.3: Sentinel check (every 5 rounds)**

Run the full sentinel suite:
```bash
cd backend && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -3
.venv/bin/python -m basedpyright 2>&1 | tail -3
cd frontend && npm run type-check && npm run build
```

Expected: pytest ≥ 903 passed (no fewer than baseline), basedpyright 0/0/0, vue-tsc 0, build pass.

If any regresses, dispatch a fix subagent before moving on.

- [ ] **Step 5.4: Update state**

---

## Round 6: Review Scan 2 (different angle)

**Files:**
- Create: `docs/superpowers/review/p42-round-06-findings.md`

- [ ] **Step 6.1: Dispatch 4 review subagents with rotated dimensions**

Rotation: (1) **watchlist + reports + lab + experiments** as one dimension; (2) **multi-symbol** state machine; (3) **LLM cron / budget / state**; (4) **build / chunk / type** discipline.

Each subagent returns the same JSON schema as Round 1.

- [ ] **Step 6.2: De-duplicate against Round 1 findings**

Drop any `(file, line)` that already appeared in Round 1. Keep only fresh findings.

- [ ] **Step 6.3: Score and rank**

Same scoring as Step 1.3.

- [ ] **Step 6.4: Save findings**

```bash
git diff --stat docs/superpowers/review/p42-round-06-findings.md
```

---

## Rounds 7–14: P-series Implementation (8 rounds)

For each round in 7..14:

- [ ] **Step N.1: Pick the next best candidate**

Priority order:
1. P0/P1 from Round 1 still open.
2. P0/P1 from Round 6.
3. Roadmap P42 → P43 → P44 → P45 sub-tasks (P42 构建告警清扫 → P43 导出图表联动 → P44 审计与导出联动 → P45 运维时间线). Pick the highest-leverage sub-task.
4. Any task that re-aligns with Roadmap suggests an update to the Roadmap P-series table.

If picking a P-series sub-task, the round's deliverable is the matching sub-iteration (e.g., "P42 sub-task: chunk budget regression RED→GREEN").

- [ ] **Step N.2: Dispatch implementation subagent**

Allow-list per the picked task. Required: green for the relevant test suite + the relevant static check.

- [ ] **Step N.3: Sentinel (every 5 rounds)**

Round 10 is a sentinel. Run the full suite.

- [ ] **Step N.4: Update touched-files and state**

```bash
echo "round-NN: <files>" >> state/p42-touched-files.txt
```

- [ ] **Step N.5: Ping-pong guard**

If `state/p42-touched-files.txt` shows the same file in 3+ rounds, STOP and ask the user: "File X has been touched 3+ times; this is a ping-pong pattern. Do you want to (a) merge into a refactor, (b) drop the latest, (c) accept and continue?"

---

## Round 15: P-series Polish

- [ ] **Step 15.1: Pick the open P-series work that needs UI polish / docs / Cypress**

- [ ] **Step 15.2: Dispatch a subagent to add Cypress coverage, update docs, and ensure no `// FIXME` or `// XXX` left**

---

## Round 16: Dead-Code / Docs Sweep

**Files:**
- Modify: `docs/Roadmap.md` (P-series table update if any P-series is delivered)
- Create: `docs/superpowers/review/p42-dead-code-report.md`

- [ ] **Step 16.1: Run vulture + pyflakes + grep for unused exports**

```bash
cd backend && .venv/bin/pip install vulture pyflakes
.venv/bin/vulture app/ --min-confidence 80
.venv/bin/pyflakes app/
```

- [ ] **Step 16.2: Frontend unused-export scan**

```bash
cd frontend && npx ts-prune 2>/dev/null || echo "ts-prune not installed; skip"
```

- [ ] **Step 16.3: Pick top 1 P2 dead-code finding**

- [ ] **Step 16.4: Dispatch removal subagent**

Allow-list: only the dead-code files + their tests.

- [ ] **Step 16.5: Save report**

```bash
git diff --stat docs/superpowers/review/p42-dead-code-report.md
```

---

## Round 17: Review Scan 3 (final adversarial)

- [ ] **Step 17.1: Dispatch 4 review subagents with rotated dimensions**

Rotation: (1) **security**; (2) **performance hot path**; (3) **type contract drift**; (4) **test blind spots**.

- [ ] **Step 17.2: De-duplicate against Rounds 1, 6**

- [ ] **Step 17.3: Score and rank**

- [ ] **Step 17.4: Save findings**

---

## Rounds 18–19: Misc Cleanup

For each:

- [ ] **Step N.1: Pick the next best P3 finding**

- [ ] **Step N.2: Dispatch fix subagent**

- [ ] **Step N.3: Sentinel (every 5 rounds; round 20 is final)**

---

## Round 20: Final Regression + Roadmap Update

**Files:**
- Modify: `docs/Roadmap.md`
- Create: `docs/superpowers/review/p42-final-summary.md`

- [ ] **Step 20.1: Full regression suite**

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
.venv/bin/python -m basedpyright
```

Expected: ≥ baseline (903) passed, basedpyright 0/0/0.

```bash
cd frontend && npm run type-check && npm run build
```

Expected: vue-tsc 0, build pass, chunk budget hold.

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/<touched>.cy.ts
```

Expected: touched specs green.

- [ ] **Step 20.2: Write final summary**

`docs/superpowers/review/p42-final-summary.md` must include:
- Round-by-round log: type, scope, files touched, sentinel pass/fail.
- Net delta: pytest delta, basedpyright delta, build warnings delta, Cypress delta.
- Top 3 remaining issues for next batch (P46+).

- [ ] **Step 20.3: Update Roadmap.md**

Add a row to the "近期已完成迭代" table for P42. Match the format of P40 / P41 entries. Reference the spec + plan + summary files.

- [ ] **Step 20.4: Checkpoint**

```bash
git status --short
git diff --stat
```

Expected: only docs and state files changed in this round. If the user asked for commits, commit per round; otherwise leave as a single working tree.

- [ ] **Step 20.5: Mark orchestrator state as complete**

```bash
echo "20" > state/p42-current-round.txt
echo "complete" > state/p42-status.txt
```

---

## Self-Review

- **Spec coverage:** Spec's 20-round structure is mirrored 1:1 by the task list (Rounds 1–20 explicit). Each round has a clear type, output, and verification. ✓
- **Placeholder scan:** No TBD / TODO / "later" / "appropriate" / "similar to" in any step. ✓
- **Type consistency:** All subagent outputs use the same JSON schema. Sentinel command is identical across rounds. ✓
- **Loose end:** Round 1's "5 dimensions" prompt is summarized, not copy-pasted. If a subagent needs more guidance, the orchestrator should expand inline. This is intentional (to keep the plan readable) and the subagent's `Explore` agent type has the latitude to interpret.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-15-p42-autonomous-20-round-iteration.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per round (or per task within a round), review between rounds, fast iteration.
2. **Inline Execution** — Execute rounds in this session using executing-plans, batch execution with checkpoints for review.

Which approach?
