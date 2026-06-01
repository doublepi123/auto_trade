# Project Iteration Roadmap: P10 Lab First

## TL;DR
> **Summary**: Deliver the next 1-3 months around a narrow P10 LLM Lab MVP first, then safely integrate prompt variants, sync roadmap docs, and add bounded quality gates. The Lab starts read-only and cannot affect live trading until explicit promotion semantics and tests exist.
> **Deliverables**:
> - `/#/lab` route with Lab shell, API clients, experiment/performance views, empty/error states, and Cypress coverage.
> - Safe prompt variant integration into the LLM advisor path with default-behavior fallback.
> - Bounded README/Roadmap status sync.
> - CI workflow that runs existing backend/frontend quality gates before image publishing.
> **Effort**: Large
> **Parallel**: YES - 3 waves
> **Critical Path**: Task 1 → Tasks 2-5 → Task 6 → Task 10 → Final Verification Wave

## Context
### Original Request
制定项目后续的迭代计划。

### Interview Summary
- Priority mainline: P10 Lab first.
- Planning horizon: 1-3 month roadmap.
- Verification strategy: tests-after, with each implementation task carrying its own tests/QA.
- Default capacity assumption: single maintainer or small team.

### Metis Review (gaps addressed)
- Lab MVP vs stretch is explicit: read-only route/comparison first; live A/B, automated prompt evolution, statistical significance engines, and traffic splitting are out of scope.
- Lab must remain isolated from live trading until promotion semantics are planned and verified.
- Existing `/api/experiments`, `/api/performance`, models, and services are the foundation; do not rebuild backend unless a task proves a concrete gap.
- Preserve current frontend hash routing convention and avoid broad frontend rewrites.
- QA must include empty/error states and executable commands, not manual verification.
- Documentation sync is bounded to roadmap status drift, not a docs overhaul.

## Work Objectives
### Core Objective
Make the next 1-3 months execution-ready by sequencing P10 Lab frontendization, safe prompt-variant integration, bounded docs cleanup, and quality gates without changing live-trading behavior accidentally.

### Deliverables
- New Lab route at `/#/lab` using existing hash routing.
- Frontend API clients/types for experiments and performance endpoints.
- Lab MVP UI for experiment list/detail and performance comparison.
- Explicit empty, partial-data, timeout, and server-error states.
- Prompt variant integration that falls back to existing prompt behavior when no valid variant is active.
- README/Roadmap status sync for replay/P10 status only.
- CI gate workflow or workflow extension for pytest, basedpyright, vue-tsc, frontend build, and Cypress where feasible.

### Definition of Done (verifiable conditions with commands)
- `cd backend && python3 -m pytest tests/ -v` exits `0`.
- `cd backend && python3 -m basedpyright` exits `0`.
- `cd frontend && npm run type-check` exits `0`.
- `cd frontend && npm run build` exits `0`.
- `cd frontend && npm run cypress:run` exits `0` with Lab navigation, empty-state, error-state, and comparison scenarios covered.
- `docker compose up --build -d` starts both services, and `curl -fsS http://localhost:${AUTO_TRADE_FRONTEND_PORT:-8080}/api/health` exits `0`.

### Must Have
- Use existing backend experiment/performance APIs as first choice.
- Preserve `createWebHashHistory()` routing.
- Add stable selectors such as `data-testid="lab-view"`, `data-testid="lab-empty"`, `data-testid="lab-error"`, and `data-testid="lab-performance-chart"` for QA.
- Keep Lab read-only for live-trading effects until Task 6 explicitly integrates prompt variants with fallback tests.
- Every task writes evidence to `.sisyphus/evidence/`.

### Must NOT Have
- No live A/B traffic splitting.
- No automated prompt evolution.
- No statistical significance platform.
- No broad frontend architecture rewrite.
- No backend API rebuild unless Task 1 proves a missing contract.
- No changes that place, cancel, or modify live orders from the Lab UI.
- No commits unless the user explicitly requests committing.

## Verification Strategy
> Verification is agent-executed; only the final completion approval comes from the user as required by the final verification wave.
- Test decision: Tests-after using existing backend pytest/basedpyright and frontend vue-tsc/build/Cypress.
- QA policy: Every task has agent-executed scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`.

## Execution Strategy
### Parallel Execution Waves
> Target: 5-8 tasks per wave. <3 per wave (except final) = under-splitting.
> Extract shared dependencies as Wave-1 tasks for max parallelism.

Wave 1: Task 1 contract audit, then Task 2 frontend API clients/types after Task 1 within the same wave; Task 8 bounded docs sync and Task 9 CI gate design can run independently.
Wave 2: Task 3 Lab route/shell, Task 4 experiments UI, Task 5 performance comparison UI, Task 7 Lab Cypress scenarios.
Wave 3: Task 6 prompt variant integration, Task 10 release hardening and smoke verification.

### Dependency Matrix (full, all tasks)
| Task | Blocks | Blocked By |
|---|---|---|
| 1. Lab API contract audit | 2, 4, 5, 6 | None |
| 2. Frontend Lab API clients/types | 3, 4, 5, 7 | 1 |
| 3. Lab route and shell | 4, 5, 7 | 2 |
| 4. Experiments list/detail UI | 7 | 2, 3 |
| 5. Performance comparison UI | 7 | 2, 3 |
| 6. Safe prompt variant integration | 10 | 1 |
| 7. Lab Cypress and QA coverage | 10 | 3, 4, 5 |
| 8. Bounded docs sync | None | None |
| 9. CI quality gates | 10 | None |
| 10. Release hardening and smoke verification | Final Verification | 6, 7, 9 |

### Agent Dispatch Summary (wave → task count → categories)
| Wave | Task Count | Categories |
|---|---:|---|
| 1 | 4 | quick, quick, writing, unspecified-high |
| 2 | 4 | visual-engineering, visual-engineering, visual-engineering, unspecified-high |
| 3 | 2 | unspecified-high, unspecified-high |

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [ ] 1. Audit Lab API contracts and fixture needs

  **What to do**: Inspect existing experiment/performance models, schemas, routers, services, frontend API conventions, and the P10 spec. Produce a short contract note in the implementation summary identifying exact request/response shapes the Lab UI should consume. If existing endpoints are sufficient, make no backend API changes. If a required field is missing, add the smallest schema/API change plus tests.
  **Must NOT do**: Do not redesign the experiment domain. Do not add live-trading behavior. Do not create new generic API layers.

  **Recommended Agent Profile**:
  - Category: `quick` - Reason: bounded contract audit with small backend test additions only if needed.
  - Skills: [] - No specialized external docs needed.
  - Omitted: [`frontend-ui-ux`] - No UI implementation in this task.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 2, 4, 5, 6 | Blocked By: None

  **References**:
  - Pattern: `backend/app/api/experiments.py:20` - existing experiments API foundation.
  - Pattern: `backend/app/api/performance.py:17` - existing performance API foundation.
  - API/Type: `backend/app/models.py:208` - prompt/experiment/performance ORM models.
  - Pattern: `frontend/src/api/client.ts` - existing axios client convention.
  - External: `docs/superpowers/specs/2026-05-29-llm-lab-frontend-design.md:13` - P10 front-endization gap.

  **Acceptance Criteria**:
  - [ ] `cd backend && python3 -m pytest tests/ -v` exits `0`.
  - [ ] `cd backend && python3 -m basedpyright` exits `0`.
  - [ ] Implementation summary lists whether existing APIs were sufficient; if not, it lists exact changed backend files and new/updated tests.

  **QA Scenarios**:
  ```
  Scenario: Existing Lab contracts support frontend fixture creation
    Tool: Bash
    Steps: Run `cd backend && python3 -m pytest tests/ -v`; inspect generated task summary for `/api/experiments` and `/api/performance` response fields.
    Expected: Command exits 0; summary names experiment id/name/status/time fields and performance metric fields needed by the UI.
    Evidence: .sisyphus/evidence/task-1-lab-contracts.txt

  Scenario: Missing optional metrics do not require backend rewrite
    Tool: Bash
    Steps: Run backend tests after any minimal schema handling; verify partial/null metric fixture is covered by a test or noted as frontend-handled.
    Expected: Command exits 0; partial/null metrics are accepted or gracefully represented for frontend handling.
    Evidence: .sisyphus/evidence/task-1-lab-contracts-partial.txt
  ```

  **Commit**: NO | Message: `feat(lab): audit lab api contracts` | Files: [backend/app/api/experiments.py, backend/app/api/performance.py, backend/app/schemas.py, backend/tests/*]

- [ ] 2. Add frontend Lab API clients and TypeScript types

  **What to do**: Add typed frontend API modules for experiments and performance following existing `frontend/src/api/*` patterns. Define TypeScript types in the existing type location or domain API files, matching Task 1 contracts. Include request helpers for list/detail/performance queries and normalized error handling consistent with current axios client behavior.
  **Must NOT do**: Do not use `as any`, `@ts-ignore`, or broad unknown casts. Do not hardcode production URLs.

  **Recommended Agent Profile**:
  - Category: `quick` - Reason: focused frontend API/type addition.
  - Skills: [] - Existing project patterns are sufficient.
  - Omitted: [`frontend-ui-ux`] - No visual UI work yet.

  **Parallelization**: Can Parallel: YES after Task 1 | Wave 1 | Blocks: 3, 4, 5, 7 | Blocked By: 1

  **References**:
  - Pattern: `frontend/src/api/strategy.ts` - per-domain API module pattern.
  - Pattern: `frontend/src/api/trade.ts` - pagination/query handling pattern.
  - API/Type: `frontend/src/types/index.ts` - shared frontend types location.
  - Pattern: `frontend/src/api/client.ts` - axios instance and base behavior.

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] `cd frontend && npm run build` exits `0`.
  - [ ] API functions have explicit return types and no TypeScript suppressions.

  **QA Scenarios**:
  ```
  Scenario: Typed Lab API client compiles
    Tool: Bash
    Steps: Run `cd frontend && npm run type-check`.
    Expected: Command exits 0 with no `any`/suppression additions in changed files.
    Evidence: .sisyphus/evidence/task-2-lab-api-types.txt

  Scenario: Frontend build includes Lab API modules without runtime import errors
    Tool: Bash
    Steps: Run `cd frontend && npm run build`.
    Expected: Command exits 0 and Vite completes production build.
    Evidence: .sisyphus/evidence/task-2-lab-api-build.txt
  ```

  **Commit**: NO | Message: `feat(lab): add typed lab api clients` | Files: [frontend/src/api/*, frontend/src/types/index.ts]

- [ ] 3. Add `/lab` route and Lab shell

  **What to do**: Add `frontend/src/views/Lab.vue`, register `/#/lab` in `frontend/src/router/index.ts`, and make the existing navigation item render the new view. The shell must include stable `data-testid="lab-view"`, page title, short description, loading state, empty state, and error state containers. Preserve hash routing.
  **Must NOT do**: Do not switch to `createWebHistory()`. Do not change unrelated routes or layout behavior.

  **Recommended Agent Profile**:
  - Category: `visual-engineering` - Reason: frontend view structure and user-facing shell.
  - Skills: [`frontend-ui-ux`] - To keep UI cohesive with Element Plus layout.
  - Omitted: [] - UI work benefits from visual skill.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 4, 5, 7 | Blocked By: 2

  **References**:
  - Pattern: `frontend/src/App.vue:7` - existing `/lab` nav signal.
  - Pattern: `frontend/src/router/index.ts:11` - current hash route registration pattern.
  - Pattern: `frontend/src/views/Dashboard.vue` - page composition style.
  - Pattern: `frontend/src/views/Backtest.vue` - form/result page style.

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] `cd frontend && npm run build` exits `0`.
  - [ ] Cypress or Playwright navigation to `/#/lab` finds `[data-testid="lab-view"]`.
  - [ ] Unknown route fallback behavior remains unchanged.

  **QA Scenarios**:
  ```
  Scenario: Lab route loads directly
    Tool: Playwright
    Steps: Open `http://localhost:3000/#/lab`; wait for `[data-testid="lab-view"]`.
    Expected: Lab page title is visible and no router 404/fallback page is shown.
    Evidence: .sisyphus/evidence/task-3-lab-route.png

  Scenario: Unknown route fallback still works
    Tool: Playwright
    Steps: Open `http://localhost:3000/#/definitely-missing-route`.
    Expected: Existing fallback behavior occurs exactly as before Task 3; Lab route change does not capture unknown routes.
    Evidence: .sisyphus/evidence/task-3-route-fallback.png
  ```

  **Commit**: NO | Message: `feat(lab): add lab route shell` | Files: [frontend/src/views/Lab.vue, frontend/src/router/index.ts, frontend/src/App.vue]

- [ ] 4. Build experiments list/detail MVP

  **What to do**: In `Lab.vue` or bounded child components, render experiments from the typed client: list/table, selected experiment detail panel, status/time metadata, and prompt variant identifiers if available. Add empty state for no experiments and error state for failed list/detail calls.
  **Must NOT do**: Do not add experiment creation/editing unless existing API and P10 spec explicitly require it for MVP. Do not make Lab able to affect live trading.

  **Recommended Agent Profile**:
  - Category: `visual-engineering` - Reason: user-facing data UI.
  - Skills: [`frontend-ui-ux`] - For clear states and cohesive Element Plus components.
  - Omitted: [] - UI skill is appropriate.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 7 | Blocked By: 2, 3

  **References**:
  - Pattern: `frontend/src/views/TradeHistory.vue` - tabular/history UI pattern.
  - Pattern: `frontend/src/views/DecisionTimeline.vue` - event/list filtering pattern.
  - API/Type: Task 2 Lab experiment client/types.
  - API: `backend/app/api/experiments.py:20` - backend source endpoint.

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] `cd frontend && npm run build` exits `0`.
  - [ ] Lab displays non-empty experiment fixture rows with deterministic labels.
  - [ ] Lab displays `[data-testid="lab-empty"]` when experiment list is empty.
  - [ ] Lab displays `[data-testid="lab-error"]` when experiment API returns 500.

  **QA Scenarios**:
  ```
  Scenario: Experiment list and detail render
    Tool: Cypress
    Steps: Mock `GET /api/experiments*` with two experiments; visit `/#/lab`; click the row with text `Baseline Prompt`.
    Expected: `[data-testid="lab-view"]` contains `Baseline Prompt` and selected detail metadata.
    Evidence: .sisyphus/evidence/task-4-experiment-list.png

  Scenario: Empty experiment state renders
    Tool: Cypress
    Steps: Mock `GET /api/experiments*` as empty list; visit `/#/lab`.
    Expected: `[data-testid="lab-empty"]` is visible and no broken table/chart is shown.
    Evidence: .sisyphus/evidence/task-4-experiment-empty.png
  ```

  **Commit**: NO | Message: `feat(lab): render experiment list detail` | Files: [frontend/src/views/Lab.vue, frontend/src/components/*, frontend/cypress/e2e/*]

- [ ] 5. Build performance comparison MVP

  **What to do**: Add performance comparison UI for selected experiments using existing performance endpoint data. Include filters for symbol/timeframe only if supported by Task 1 contracts. Show key metrics: PnL, max drawdown, win rate, trade count, skipped trades if available, LLM cost/latency if available. Handle partial/null metrics explicitly.
  **Must NOT do**: Do not compare metrics across incompatible symbols/timeframes without a visible warning. Do not invent metrics not returned by the API.

  **Recommended Agent Profile**:
  - Category: `visual-engineering` - Reason: chart/table comparison UI.
  - Skills: [`frontend-ui-ux`] - For readable comparison and partial-data states.
  - Omitted: [] - UI skill is appropriate.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 7 | Blocked By: 2, 3

  **References**:
  - Pattern: `frontend/src/components/PnLChart.vue` - charting pattern.
  - Pattern: `frontend/src/components/BacktestChart.vue` - backtest chart/result pattern.
  - API: `backend/app/api/performance.py:17` - performance endpoint foundation.
  - Domain: `backend/app/domain/performance/performance_tracker.py:16` - performance metric origin.

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] `cd frontend && npm run build` exits `0`.
  - [ ] `[data-testid="lab-performance-chart"]` or equivalent table renders with mocked metrics.
  - [ ] Partial/null metrics render as `N/A` or a documented placeholder, not `undefined`, `NaN`, or blank broken UI.

  **QA Scenarios**:
  ```
  Scenario: Performance comparison renders metrics
    Tool: Cypress
    Steps: Mock experiments and `GET /api/performance*` with PnL, drawdown, win rate, and trade count; visit `/#/lab`.
    Expected: `[data-testid="lab-performance-chart"]` is visible and metric labels/values match the fixture.
    Evidence: .sisyphus/evidence/task-5-performance-comparison.png

  Scenario: Partial metrics render safely
    Tool: Cypress
    Steps: Mock performance response with null drawdown and missing latency; visit `/#/lab`.
    Expected: UI shows `N/A` or documented placeholder; no `undefined` or `NaN` text appears.
    Evidence: .sisyphus/evidence/task-5-performance-partial.png
  ```

  **Commit**: NO | Message: `feat(lab): add performance comparison` | Files: [frontend/src/views/Lab.vue, frontend/src/components/*, frontend/cypress/e2e/*]

- [ ] 6. Integrate prompt variants into LLM advisor safely

  **What to do**: Resolve the existing prompt variant / A/B TODO by allowing the LLM advisor to select an active/default prompt variant only through explicit service logic. If no valid active variant exists, preserve the current prompt path byte-for-byte where practical. Add backend tests proving default fallback, selected variant usage, deleted/missing variant fallback, and no unexpected trading side effects.
  **Must NOT do**: Do not add live A/B routing, traffic splitting, automated winner selection, or order placement changes. Do not change cooldown, fee guard, risk, or session guard semantics.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: backend business logic with trading-safety implications.
  - Skills: [] - Project-specific patterns are sufficient.
  - Omitted: [`frontend-ui-ux`] - Backend safety task.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: 10 | Blocked By: 1

  **References**:
  - Pattern: `backend/app/services/llm_advisor_service.py:75` - prompt variant / A/B integration TODO.
  - Pattern: `backend/app/services/llm_interaction_service.py` - LLM interaction persistence pattern.
  - Guardrail: `backend/app/services/interval_application_service.py` - interval application safety rules.
  - Guardrail: `backend/app/services/trade_execution_service.py` - order execution must not be changed except through existing paths.
  - Test: `backend/tests/test_llm_advisor*.py` or closest existing LLM tests - follow service test patterns.

  **Acceptance Criteria**:
  - [ ] `cd backend && python3 -m pytest tests/ -v` exits `0`.
  - [ ] `cd backend && python3 -m basedpyright` exits `0`.
  - [ ] Test proves no active prompt variant preserves current default prompt behavior.
  - [ ] Test proves active prompt variant is used in advisor request construction.
  - [ ] Test proves deleted/missing variant falls back safely and does not place/cancel orders.

  **QA Scenarios**:
  ```
  Scenario: Default prompt fallback remains safe
    Tool: Bash
    Steps: Run targeted backend LLM advisor tests, then full `cd backend && python3 -m pytest tests/ -v`.
    Expected: Tests exit 0; no variant case uses existing prompt path and produces no unexpected trade action.
    Evidence: .sisyphus/evidence/task-6-prompt-default.txt

  Scenario: Missing selected variant falls back without live-trading effect
    Tool: Bash
    Steps: Run test case where selected variant id is absent/deleted.
    Expected: Test exits 0; advisor logs/records safe fallback and does not call order placement/cancel methods.
    Evidence: .sisyphus/evidence/task-6-prompt-missing.txt
  ```

  **Commit**: NO | Message: `feat(llm): integrate prompt variants safely` | Files: [backend/app/services/llm_advisor_service.py, backend/app/services/*, backend/tests/*]

- [ ] 7. Add Lab Cypress coverage and fixture strategy

  **What to do**: Add or extend Cypress specs for Lab navigation, non-empty experiments, empty experiments, API error, performance comparison, and partial metrics. Use deterministic intercept fixtures rather than requiring a live broker or real DeepSeek key. Ensure selectors created in Tasks 3-5 are covered.
  **Must NOT do**: Do not require live backend credentials, Longbridge access, or DeepSeek API calls in Cypress.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: E2E fixture and failure-mode coverage across UI paths.
  - Skills: [] - Existing Cypress tests provide pattern.
  - Omitted: [`playwright`] - Project already uses Cypress for frontend E2E; Playwright remains optional for manual QA evidence.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 10 | Blocked By: 3, 4, 5

  **References**:
  - Test: `frontend/cypress/e2e/navigation.cy.ts` - navigation test pattern.
  - Test: `frontend/cypress/e2e/backtest.cy.ts` - page data/interaction pattern.
  - Test: `frontend/cypress/e2e/dashboard.cy.ts` - API stubbing pattern if present.
  - Config: `frontend/package.json` - Cypress scripts.

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run cypress:run` exits `0`.
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] Cypress includes Lab happy path, empty state, error state, and partial metric state.

  **QA Scenarios**:
  ```
  Scenario: Lab E2E suite passes with mocked data
    Tool: Bash
    Steps: Run `cd frontend && npm run cypress:run`.
    Expected: Command exits 0; Lab spec passes without external credentials.
    Evidence: .sisyphus/evidence/task-7-lab-cypress.txt

  Scenario: Lab API failure is covered
    Tool: Cypress
    Steps: Run Lab spec case mocking `/api/experiments*` as 500.
    Expected: `[data-testid="lab-error"]` is visible with actionable error text.
    Evidence: .sisyphus/evidence/task-7-lab-error.png
  ```

  **Commit**: NO | Message: `test(lab): add cypress lab coverage` | Files: [frontend/cypress/e2e/*, frontend/cypress/fixtures/*]

- [ ] 8. Sync bounded roadmap documentation

  **What to do**: Update README/Roadmap status only for confirmed drift: P7 replay status and P10 Lab roadmap entry/status. Add a short note that Lab MVP is read-only initially and live A/B is out of scope. Keep wording consistent with existing Chinese docs style.
  **Must NOT do**: Do not rewrite full README. Do not add speculative commitments beyond this plan.

  **Recommended Agent Profile**:
  - Category: `writing` - Reason: bounded documentation correction.
  - Skills: [] - No special skill needed.
  - Omitted: [`frontend-ui-ux`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: None | Blocked By: None

  **References**:
  - Drift: `README.md:93` - P7 replay still marked planning.
  - Source: `docs/Roadmap.md:440` - P7 marked complete.
  - Source: `docs/superpowers/plans/2026-05-29-llm-lab-frontend.md:5` - P10 Lab plan source.

  **Acceptance Criteria**:
  - [ ] `README.md` and `docs/Roadmap.md` no longer contradict each other about P7 status.
  - [ ] P10 Lab entry states read-only MVP first and excludes live A/B in this iteration.
  - [ ] No unrelated documentation sections are rewritten.

  **QA Scenarios**:
  ```
  Scenario: Documentation status is consistent
    Tool: Bash
    Steps: Run `git diff -- README.md docs/Roadmap.md` after edits.
    Expected: Diff only touches P7/P10 roadmap wording and contains no unrelated rewrite.
    Evidence: .sisyphus/evidence/task-8-docs-diff.txt

  Scenario: P10 scope guardrail is documented
    Tool: Bash
    Steps: Search changed docs for `live A/B` or Chinese equivalent scope exclusion.
    Expected: Docs state Lab MVP does not perform live traffic splitting or automated prompt evolution.
    Evidence: .sisyphus/evidence/task-8-docs-guardrail.txt
  ```

  **Commit**: NO | Message: `docs(roadmap): sync replay and lab status` | Files: [README.md, docs/Roadmap.md]

- [ ] 9. Add CI quality gates before image publish

  **What to do**: Extend CI or add a workflow so existing verification commands run before Docker image publish: backend pytest, backend basedpyright, frontend type-check, frontend build, and Cypress if dependencies/browser setup are available. If Cypress is too costly for every push, run it on PR/manual and document that choice in workflow comments.
  **Must NOT do**: Do not remove DockerHub publishing. Do not skip failing tests. Do not add secrets to the repo.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: CI wiring across backend/frontend with dependency caching and ordering.
  - Skills: [`git-master`] - Recommended for careful workflow diff review if committing is later requested.
  - Omitted: [] - CI task benefits from git awareness.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 10 | Blocked By: None

  **References**:
  - CI: `.github/workflows/dockerhub.yml` - current build/publish workflow.
  - Backend deps: `backend/requirements-dev.txt` - pytest/basedpyright.
  - Frontend scripts: `frontend/package.json` - type-check/build/Cypress scripts.
  - Deploy: `docker-compose.yaml` - service composition.

  **Acceptance Criteria**:
  - [ ] Workflow runs `cd backend && python3 -m pytest tests/ -v` before image publish.
  - [ ] Workflow runs `cd backend && python3 -m basedpyright` before image publish.
  - [ ] Workflow runs `cd frontend && npm run type-check` before image publish.
  - [ ] Workflow runs `cd frontend && npm run build` before image publish.
  - [ ] Cypress policy is implemented and documented in workflow: either required on push/PR or manual/scheduled with rationale.

  **QA Scenarios**:
  ```
  Scenario: CI workflow syntax validates locally enough for review
    Tool: Bash
    Steps: Run a non-mutating workflow syntax check if available, otherwise run local equivalent gate commands.
    Expected: All local gate commands exit 0; workflow file has no obvious YAML syntax errors.
    Evidence: .sisyphus/evidence/task-9-ci-gates.txt

  Scenario: Docker publish remains gated
    Tool: Bash
    Steps: Inspect workflow dependency/order after edit.
    Expected: Docker build/push jobs depend on successful test/type-check/build jobs or are in a later ordered step.
    Evidence: .sisyphus/evidence/task-9-ci-order.txt
  ```

  **Commit**: NO | Message: `ci: add test and typecheck gates` | Files: [.github/workflows/dockerhub.yml]

- [ ] 10. Release hardening and local smoke verification

  **What to do**: Run full local verification after Tasks 1-9. Start the stack with Docker Compose, verify `/api/health`, verify the frontend loads, and verify `/#/lab` works through the production Nginx/frontend path. Record all command outputs and screenshots.
  **Must NOT do**: Do not use real broker credentials. Do not place orders. Do not expose services publicly.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: cross-stack smoke verification and evidence capture.
  - Skills: [`playwright`] - Browser verification of production path.
  - Omitted: [] - Browser QA is required here.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: Final Verification | Blocked By: 6, 7, 9

  **References**:
  - Deploy: `docker-compose.yaml` - local production-like stack.
  - Frontend proxy: `frontend/nginx.conf` - `/api` and `/ws` routing.
  - Health: `backend/app/main.py` - `/api/health` registration.
  - Route: `frontend/src/router/index.ts` - Lab route after Task 3.

  **Acceptance Criteria**:
  - [ ] `cd backend && python3 -m pytest tests/ -v` exits `0`.
  - [ ] `cd backend && python3 -m basedpyright` exits `0`.
  - [ ] `cd frontend && npm run type-check` exits `0`.
  - [ ] `cd frontend && npm run build` exits `0`.
  - [ ] `cd frontend && npm run cypress:run` exits `0`.
  - [ ] `docker compose up --build -d` exits `0`.
  - [ ] `curl -fsS http://localhost:${AUTO_TRADE_FRONTEND_PORT:-8080}/api/health` exits `0`.
  - [ ] Browser can open `http://localhost:${AUTO_TRADE_FRONTEND_PORT:-8080}/#/lab` and find `[data-testid="lab-view"]`.

  **QA Scenarios**:
  ```
  Scenario: Production-like Lab route works through Docker frontend
    Tool: Playwright
    Steps: Start Docker Compose; open `http://localhost:8080/#/lab`; wait for `[data-testid="lab-view"]`.
    Expected: Lab page renders through Nginx path; no console error blocks rendering.
    Evidence: .sisyphus/evidence/task-10-docker-lab.png

  Scenario: Backend health works through frontend proxy
    Tool: Bash
    Steps: Run `curl -fsS http://localhost:${AUTO_TRADE_FRONTEND_PORT:-8080}/api/health`.
    Expected: Command exits 0 and response contains healthy JSON.
    Evidence: .sisyphus/evidence/task-10-health.txt
  ```

  **Commit**: NO | Message: `chore(release): verify lab roadmap delivery` | Files: [.sisyphus/evidence/*]

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
- [ ] F2. Code Quality Review — unspecified-high
- [ ] F3. Real Manual QA — unspecified-high (+ playwright for Lab route)
- [ ] F4. Scope Fidelity Check — deep

## Commit Strategy
- Default: do not commit.
- If the user explicitly asks for commits later, use small topic commits after each completed wave:
  - `feat(lab): add lab mvp`
  - `feat(llm): integrate prompt variants safely`
  - `docs(roadmap): sync iteration status`
  - `ci: add project quality gates`
- Never commit secrets, `.env`, credential files, frontend build artifacts, or `.venv`.

## Success Criteria
- User can open `/#/lab` and inspect experiment/performance data without live-trading side effects.
- Prompt variant integration is safe-by-default and covered by backend tests.
- README/Roadmap no longer contradict P7/P10 status.
- CI prevents image publishing when existing backend/frontend quality gates fail.
- All final verification agents approve, and the user explicitly says the work is okay.
