# LLM Intelligent Interval Implementation Plan

> **状态：🗄️ 已存档 / 暂不推进**（2026-06-01 整理）
>
> 本计划是 6/2 写的设计对应的 11-task 实施计划，与配套 spec [`2026-06-02-llm-intelligent-interval-design.md`](../specs/2026-06-02-llm-intelligent-interval-design.md) 一起保留。
>
> **决策依据：**
> - 核心 LLM 区间调整能力已通过 P0/P9/P11 完整交付；现有 `LLMAdvisorService` 已具备区间推荐、置信度校验、IntervalApplication 渐进式过渡规则（含 6/2 路线图决策的"追价加仓"行为）。
> - P22（commit `02ff712`）已补全波动率触发（`_should_run_llm_analysis` 双门控：时间间隔 OR 价格波动 ≥ `llm_interval_volatility_threshold_pct`），无需重建三件套。
> - 详情见 [2026-05-31-next-iteration-roadmap.md](./2026-05-31-next-iteration-roadmap.md) 的"显式暂不做"段，以及 [主 Roadmap.md](../../Roadmap.md) 的"下一步建议"（P23 实时通知中心）。
>
> **保留原因：** 作为设计探索性文档与配套实施计划，保留供未来参考；若业务优先级变化，可基于此计划重启。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement DeepSeek LLM-powered intelligent price interval adjustment that automatically recommends and applies buy_low/sell_high based on market analysis, with progressive smooth transition when positions are held.

**Architecture:** Three new backend services (DataAggregator -> LLMAdvisorService -> IntervalApplicationService) work together to fetch market data, call DeepSeek API, and safely apply new intervals. The frontend gets LLM status cards on Strategy and Dashboard pages. A cron job triggers analysis every 4 hours.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, DeepSeek API, Vue 3, TypeScript, Element Plus, pytest, Cypress

---

## File Structure

### Backend - New Files

| File | Responsibility |
|------|---------------|
| `backend/app/services/data_aggregator.py` | Aggregate market data into LLM prompt format |
| `backend/app/services/llm_advisor_service.py` | Call DeepSeek API, parse response, enforce 30-min throttle |
| `backend/app/services/interval_application_service.py` | Apply suggestion with progressive transition and risk guardrails |
| `backend/app/api/llm_advisor.py` | FastAPI routes for LLM interval endpoints |
| `backend/tests/test_llm_advisor.py` | Tests for LLM advisor service and data aggregator |
| `backend/tests/test_interval_application.py` | Tests for interval application service |

### Backend - Modified Files

| File | Changes |
|------|---------|
| `backend/app/config.py` | Add DEEPSEEK_API_KEY, DEEPSEEK_API_URL, LLM_INTERVAL_* settings |
| `backend/app/models.py` | Add 11 LLM-related columns to StrategyConfig |
| `backend/app/schemas.py` | Add LLMAnalyzeRequest, LLMAnalyzeResponse, LLMIntervalStatus |
| `backend/app/main.py` | Register llm_advisor router, start cron task in lifespan |
| `backend/requirements.txt` | Add `apscheduler>=3.10.0` |

### Frontend - New/Modified Files

| File | Responsibility |
|------|---------------|
| `frontend/src/api/llm_advisor.ts` | API client for LLM endpoints |
| `frontend/src/types/index.ts` | Add LLMIntervalStatus interface |
| `frontend/src/views/Strategy.vue` | Add LLM card (toggle, suggestion, manual analyze) |
| `frontend/src/views/Dashboard.vue` | Add LLM status indicator |
| `frontend/cypress/e2e/strategy_llm.cy.ts` | Cypress test for Strategy LLM card |
| `frontend/cypress/e2e/dashboard_llm.cy.ts` | Cypress test for Dashboard indicator |

### Database

| File | Changes |
|------|---------|
| `backend/alembic/versions/20260602_add_llm_interval_fields.py` | Migration adding 11 columns |

---

## Verification Commands

| What | Command |
|------|---------|
| Backend tests | `cd backend && python -m pytest tests/test_llm_advisor.py tests/test_interval_application.py -v` |
| All backend tests | `cd backend && python -m pytest tests/ -v --tb=short` |
| Frontend build | `cd frontend && npm run build` |
| Cypress E2E | `cd frontend && npx cypress run --spec "cypress/e2e/strategy_llm.cy.ts,cypress/e2e/dashboard_llm.cy.ts"` |

---

### Task 1: Add Dependencies and Configuration

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/config.py`

**Rationale:** Backend needs APScheduler for cron jobs and DeepSeek API configuration.

- [ ] **Step 1: Add APScheduler to requirements**

Add `apscheduler>=3.10.0` to `backend/requirements.txt`

- [ ] **Step 2: Add LLM config to settings**

Modify `backend/app/config.py`, add after `api_key: str = ""`:
- deepseek_api_key: str = ""
- deepseek_api_url: str = "https://api.deepseek.com/v1/chat/completions"
- llm_interval_cron_minutes: int = 240
- llm_interval_volatility_threshold_pct: float = 5.0
- llm_min_confidence: float = 0.7
- llm_max_stripe_width_pct: float = 20.0

- [ ] **Step 3: Commit**

```bash
git add backend/requirements.txt backend/app/config.py
git commit -m "deps+config: add APScheduler and DeepSeek LLM settings"
```

---

### Task 2: Database Migration

**Files:**
- Create: `backend/alembic/versions/20260602_add_llm_interval_fields.py`
- Modify: `backend/app/models.py`

**Rationale:** StrategyConfig needs 11 new columns for LLM tracking.

- [ ] **Step 1: Write migration**

Create Alembic migration adding: auto_interval_enabled, llm_suggested_buy_low, llm_suggested_sell_high, llm_confidence_score, llm_analysis, llm_last_analysis_at, llm_next_analysis_at, llm_applied_buy_low, llm_applied_sell_high, llm_applied_at, llm_reject_reason.

- [ ] **Step 2: Update models.py**

Add corresponding Mapped columns to StrategyConfig.

- [ ] **Step 3: Verify migration**

Run: `cd backend && alembic upgrade head`

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/20260602_add_llm_interval_fields.py backend/app/models.py
git commit -m "db: add LLM interval columns to strategy_config"
```

---

### Task 3: Data Aggregator Service

**Files:**
- Create: `backend/app/services/data_aggregator.py`
- Create: `backend/tests/test_llm_advisor.py`

**Rationale:** Fetches market data, computes ATR/Bollinger, builds LLM prompt.

- [ ] **Step 1: Write failing tests**

Tests for _compute_atr, _compute_bollinger_bands, build_prompt structure.

- [ ] **Step 2: Implement DataAggregator**

Create service with fetch_market_data, _compute_atr, _compute_bollinger_bands, build_prompt methods.

- [ ] **Step 3: Run tests**

`cd backend && python -m pytest tests/test_llm_advisor.py::TestDataAggregator -v`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/data_aggregator.py backend/tests/test_llm_advisor.py
git commit -m "feat: add DataAggregator service for LLM market data"
```

---

### Task 4: LLM Advisor Service

**Files:**
- Create: `backend/app/services/llm_advisor_service.py`
- Modify: `backend/tests/test_llm_advisor.py`

**Rationale:** Calls DeepSeek API with 30-min throttle, parses JSON response.

- [ ] **Step 1: Write failing tests**

Tests for analyze, _parse_response, _is_throttled.

- [ ] **Step 2: Implement LLMAdvisorService**

Create service with analyze, _call_deepseek, _parse_response, _is_throttled, _record_analysis methods.

- [ ] **Step 3: Run tests**

`cd backend && python -m pytest tests/test_llm_advisor.py -v`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/llm_advisor_service.py backend/tests/test_llm_advisor.py
git commit -m "feat: add LLMAdvisorService with DeepSeek integration"
```

---

### Task 5: Interval Application Service

**Files:**
- Create: `backend/app/services/interval_application_service.py`
- Create: `backend/tests/test_interval_application.py`

**Rationale:** Core business logic - progressive smooth transition + risk guardrails.

- [ ] **Step 1: Write failing tests**

Tests for FLAT, LONG_sell_high_higher, LONG_sell_high_lower, SHORT_buy_lower, SHORT_buy_higher, confidence_rejection, price_proximity_rejection, width_rejection.

- [ ] **Step 2: Implement IntervalApplicationService**

Create service with apply_suggestion, _apply_flat, _apply_long, _apply_short, _validate_guardrails methods.

- [ ] **Step 3: Run tests**

`cd backend && python -m pytest tests/test_interval_application.py -v`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/interval_application_service.py backend/tests/test_interval_application.py
git commit -m "feat: add IntervalApplicationService with progressive transition"
```

---

### Task 6: API Routes

**Files:**
- Create: `backend/app/api/llm_advisor.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Rationale:** Expose LLM endpoints to frontend.

- [ ] **Step 1: Add schemas**

Add LLMAnalyzeRequest, LLMAnalyzeResponse, LLMIntervalStatus to schemas.py.

- [ ] **Step 2: Create router**

Create `backend/app/api/llm_advisor.py` with POST /analyze, GET /status, PUT /enable, PUT /disable.

- [ ] **Step 3: Register router**

Import and include router in `backend/app/main.py`.

- [ ] **Step 4: Run backend tests**

`cd backend && python -m pytest tests/ -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/llm_advisor.py backend/app/schemas.py backend/app/main.py
git commit -m "feat: add LLM advisor API routes"
```

---

### Task 7: Cron Job Integration

**Files:**
- Modify: `backend/app/main.py`

**Rationale:** Trigger LLM analysis every 4 hours via APScheduler.

- [ ] **Step 1: Add cron task to lifespan**

Add APScheduler background job in lifespan that runs every llm_interval_cron_minutes.

- [ ] **Step 2: Commit**

```bash
git add backend/app/main.py
git commit -m "feat: add APScheduler cron job for periodic LLM analysis"
```

---

### Task 8: Frontend Types and API Client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/api/llm_advisor.ts`

**Rationale:** TypeScript types and API client for LLM endpoints.

- [ ] **Step 1: Add types**

Add LLMSuggestion and LLMIntervalStatus interfaces.

- [ ] **Step 2: Create API client**

Create `frontend/src/api/llm_advisor.ts` with getLLMIntervalStatus, analyzeLLMInterval, enableLLMInterval, disableLLMInterval.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/llm_advisor.ts
git commit -m "feat(frontend): add LLM advisor types and API client"
```

---

### Task 9: Strategy Page LLM Card

**Files:**
- Modify: `frontend/src/views/Strategy.vue`

**Rationale:** Allow users to enable/disable LLM, view suggestions, and trigger manual analysis.

- [ ] **Step 1: Add LLM card**

Add card with toggle switch, suggestion display, applied values, manual analyze button, next analysis time.

- [ ] **Step 2: Commit**

```bash
git add frontend/src/views/Strategy.vue
git commit -m "feat(frontend): add LLM intelligent interval card to Strategy page"
```

---

### Task 10: Dashboard LLM Indicator

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

**Rationale:** Show LLM status in real-time dashboard.

- [ ] **Step 1: Add LLM indicator**

Add section showing auto interval enabled status, next analysis time, last result.

- [ ] **Step 2: Commit**

```bash
git add frontend/src/views/Dashboard.vue
git commit -m "feat(frontend): add LLM status indicator to Dashboard"
```

---

### Task 11: Frontend Build Verification

- [ ] **Step 1: Build**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: verify frontend build after LLM feature integration" --allow-empty
```

---

### Task 12: Cypress E2E Tests

**Files:**
- Create: `frontend/cypress/e2e/strategy_llm.cy.ts`
- Create: `frontend/cypress/e2e/dashboard_llm.cy.ts`

- [ ] **Step 1-3: Write and run tests**

Test LLM toggle, manual analyze, suggestion display, indicator presence.

Run: `cd frontend && npx cypress run --spec "cypress/e2e/strategy_llm.cy.ts,cypress/e2e/dashboard_llm.cy.ts"`

- [ ] **Step 4: Commit**

```bash
git add frontend/cypress/e2e/strategy_llm.cy.ts frontend/cypress/e2e/dashboard_llm.cy.ts
git commit -m "test(e2e): add Cypress tests for LLM intelligent interval"
```

---

### Task 13: Full Backend Test Suite

- [ ] **Step 1: Run all tests**

`cd backend && python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

- [ ] **Step 2: Commit if clean**

```bash
git commit -m "test: verify full backend test suite passes" --allow-empty
```

---

## Self-Review

### Spec Coverage
- [x] Data aggregation (Task 3)
- [x] DeepSeek API integration (Task 4)
- [x] Progressive smooth transition (Task 5)
- [x] Risk guardrails (Task 5)
- [x] Cron job scheduling (Task 7)
- [x] Frontend UI (Tasks 9-10)
- [x] Database migration (Task 2)
- [x] API routes (Task 6)
- [x] Testing (Tasks 3-6, 12-13)

### Placeholder Scan
- [x] No TBD, TODO, or incomplete sections
- [x] All tasks have exact file paths
- [x] All verification steps have exact commands

### Type Consistency
- [x] Column names in migration match model fields
- [x] Schema field names match API response structure
- [x] Frontend types match backend schema names
