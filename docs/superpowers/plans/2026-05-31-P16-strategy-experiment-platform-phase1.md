# P16 Strategy Experiment Platform Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建策略实验平台 Phase 1：基于同一份历史价格数据批量运行参数网格回测，持久化实验与 run 结果，并在前端提供排行榜比较。

**Architecture:** 后端新增独立的 `strategy_experiments` 业务域，复用现有 `BacktestEngine` 与 `BacktestPricePoint`/`BacktestParams` schema，不复制回测逻辑。API 使用 `/api/strategy-experiments`，避免与现有 Prompt A/B 工作台的 `/api/experiments` 冲突。前端新增独立 `Experiments.vue` 页面和 `api/strategy_experiments.ts` 客户端，排行榜仅展示 Phase 1 指标，不做 LLM 评分、导出或 Strategy 草稿带回。

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 sync ORM, SQLite, Pydantic v2, pytest; Vue 3.5, TypeScript strict, Element Plus, Cypress.

---

## Scope

### 本轮做

1. 参数网格：固定值、列表值、范围值，组合数上限 `500`。
2. 后端持久化：`strategy_experiments` 与 `strategy_experiment_runs`。
3. 批量执行：同一份 CSV/price_points 展开多组 `BacktestParams`，逐组调用 `BacktestEngine`。
4. 排行榜：按 `total_return_pct` / `max_drawdown_pct` / `win_rate` / `trade_count` 排序、分页。
5. 前端页面：创建实验、运行实验、查看排行榜。
6. 测试：新增 pytest 覆盖核心服务与 API，新增 Cypress 覆盖主流程。

### 本轮不做

- 不实现 LLM 建议评分；留给 P17。
- 不实现 CSV/JSON 导出；留给 P17。
- 不实现“带回 Strategy 草稿”；留给 P17。
- 不使用 `/api/experiments`；该前缀已被 Lab / Prompt A/B 使用。
- 不引入后台队列；MVP 同步执行。
- 不自动应用任何实验参数到实盘策略。

---

## Files

### Backend create

- `backend/app/services/experiment_grid_service.py` — 参数网格校验与展开。
- `backend/app/services/strategy_experiment_service.py` — 实验创建、运行、run 查询、指标摘要。
- `backend/app/api/strategy_experiments.py` — `/api/strategy-experiments` 路由。
- `backend/tests/test_experiment_grid_service.py` — 网格展开单元测试。
- `backend/tests/test_strategy_experiment_service.py` — 服务层持久化与批量运行测试。
- `backend/tests/test_strategy_experiments_api.py` — API 集成测试。

### Backend modify

- `backend/app/models.py` — 增加 `StrategyExperiment`、`StrategyExperimentRun`。
- `backend/app/database.py` — `init_db()` 增加 `_ensure_strategy_experiments_table()` 与 `_ensure_strategy_experiment_runs_table()`。
- `backend/app/schemas.py` — 增加 strategy experiment 请求/响应 schema。
- `backend/app/main.py` — include 新 router。

### Frontend create

- `frontend/src/api/strategy_experiments.ts` — 前端 API client。
- `frontend/src/views/Experiments.vue` — 实验页面。
- `frontend/cypress/e2e/strategy_experiments.cy.ts` — E2E 主流程。

### Frontend modify

- `frontend/src/types/index.ts` — 增加 strategy experiment 类型。
- `frontend/src/router/index.ts` — 增加 `/experiments` 路由。
- `frontend/src/App.vue` — 桌面导航增加“策略实验”；移动端不加入口，避免底部导航过载。
- `frontend/cypress/support/e2e.ts` — 增加默认 intercept 数据。

### Docs modify

- `docs/Roadmap.md` — P16 完成后再更新状态；实施前不改完成状态。

---

## API Contract

### `POST /api/strategy-experiments`

Request:

```json
{
  "name": "AAPL May grid",
  "symbol": "AAPL.US",
  "base_params": {
    "symbol": "AAPL.US",
    "buy_low": 180,
    "sell_high": 190,
    "short_selling": false,
    "min_profit_amount": 5,
    "max_daily_loss": 5000,
    "max_consecutive_losses": 3,
    "quantity": 10,
    "initial_cash": 100000,
    "fee_rate": 0.0005,
    "fixed_fee": 0,
    "slippage_pct": 0,
    "stop_loss_pct": 0
  },
  "parameter_grid": {
    "buy_low": { "values": [178, 180] },
    "sell_high": { "range": { "start": 188, "end": 190, "step": 1 } },
    "min_profit_amount": { "value": 5 }
  }
}
```

Response: `StrategyExperimentResponse`，`status="PENDING"`，`estimated_runs=6`。

### `POST /api/strategy-experiments/{id}/run`

Request:

```json
{
  "csv_text": "timestamp,open,high,low,close,volume\n2026-05-01T09:30:00Z,180,181,179,180.5,1000\n2026-05-01T09:31:00Z,180.5,182,180,181.5,1200\n2026-05-01T09:32:00Z,181.5,190,181,189.5,1300"
}
```

Response: `StrategyExperimentResponse`，`status="COMPLETED"`，`completed_runs` 更新。

### `GET /api/strategy-experiments/{id}/runs?sort=total_return_pct&order=desc&page=1&page_size=20`

Response:

```json
{
  "items": [
    {
      "id": 1,
      "experiment_id": 1,
      "parameters": { "buy_low": 178, "sell_high": 189 },
      "total_pnl": 120.5,
      "total_return_pct": 0.12,
      "max_drawdown_pct": 0.02,
      "win_rate": 0.5,
      "trade_count": 4,
      "status": "COMPLETED",
      "error": null,
      "created_at": "2026-05-31T00:00:00Z"
    }
  ],
  "total": 6,
  "page": 1,
  "page_size": 20
}
```

---

## Data Model

Add to `backend/app/models.py`:

```python
class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    base_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_grid_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="PENDING")
    estimated_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(_TZDateTime(), nullable=True)


class StrategyExperimentRun(Base):
    __tablename__ = "strategy_experiment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="COMPLETED")
    total_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_return_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
```

Keep `experiment_id` as indexed integer only; existing project models do not consistently use ORM relationships, and this table is queried by service methods.

---

## Task 1: Backend schemas and table bootstrap

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/database.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_database.py`

- [ ] **Step 1: Add failing database migration test**

Add to `backend/tests/test_database.py` a test that creates an old database without the two tables, calls `init_db()`, then asserts both tables exist with required columns.

Run:

```bash
cd backend && python3 -m pytest tests/test_database.py -v
```

Expected: fail because `strategy_experiments` tables do not exist.

- [ ] **Step 2: Add ORM models**

Add `StrategyExperiment` and `StrategyExperimentRun` exactly as shown in Data Model. Use existing `_TZDateTime()` and `_utcnow()` helpers.

- [ ] **Step 3: Add idempotent table bootstraps**

In `init_db()`, after `_ensure_experiment_results_table(engine)`, call:

```python
_ensure_strategy_experiments_table(engine)
_ensure_strategy_experiment_runs_table(engine)
```

Add helpers that follow `_ensure_prompt_versions_table` style and create the two tables with `CREATE TABLE IF NOT EXISTS`.

- [ ] **Step 4: Add Pydantic schemas**

Add these schema names to `backend/app/schemas.py` near backtest schemas:

```python
StrategyExperimentGridValue
StrategyExperimentGridRange
StrategyExperimentGridItem
StrategyExperimentCreate
StrategyExperimentRunRequest
StrategyExperimentResponse
StrategyExperimentRunResponse
StrategyExperimentRunPage
```

Validation rules:

- `StrategyExperimentCreate.symbol` uses `_normalize_symbol`.
- `parameter_grid` keys are limited to `BacktestParams` numeric fields: `buy_low`, `sell_high`, `min_profit_amount`, `max_daily_loss`, `max_consecutive_losses`, `quantity`, `initial_cash`, `fee_rate`, `fixed_fee`, `slippage_pct`, `stop_loss_pct`.
- A grid item must have exactly one of `value`, `values`, `range`.
- Range `step` must be positive and `(end - start) / step` must not exceed `500` by itself.

- [ ] **Step 5: Verify database test passes**

Run:

```bash
cd backend && python3 -m pytest tests/test_database.py -v
```

Expected: pass.

---

## Task 2: ExperimentGridService

**Files:**
- Create: `backend/app/services/experiment_grid_service.py`
- Test: `backend/tests/test_experiment_grid_service.py`

- [ ] **Step 1: Write failing grid tests**

Cover:

1. fixed/list/range expansion produces deterministic combinations.
2. `buy_low >= sell_high` combinations are rejected.
3. over `500` combinations raises `ValueError` with current count and limit.
4. decimal steps avoid floating drift by rounding to 10 decimals.

Run:

```bash
cd backend && python3 -m pytest tests/test_experiment_grid_service.py -v
```

Expected: fail because service file is missing.

- [ ] **Step 2: Implement service**

Create:

```python
from __future__ import annotations

from itertools import product
from typing import Any

from app.schemas import BacktestParams, StrategyExperimentCreate, StrategyExperimentGridItem

GRID_LIMIT = 500

class ExperimentGridService:
    def estimate_count(self, request: StrategyExperimentCreate) -> int:
        lengths = [len(self._values_for_item(item)) for item in request.parameter_grid.values()]
        total = 1
        for length in lengths:
            total *= length
        if total > GRID_LIMIT:
            raise ValueError(f"parameter grid produced {total} combinations, limit is {GRID_LIMIT}")
        return total

    def expand(self, request: StrategyExperimentCreate) -> list[BacktestParams]:
        keys = list(request.parameter_grid.keys())
        value_lists = [self._values_for_item(request.parameter_grid[key]) for key in keys]
        combinations: list[BacktestParams] = []
        for values in product(*value_lists):
            raw = request.base_params.model_dump()
            raw.update(dict(zip(keys, values)))
            combinations.append(BacktestParams(**raw))
        if not combinations:
            raise ValueError("parameter grid produced no valid combinations")
        return combinations
```

Implementation requirements:

- Preserve key order from `request.parameter_grid` for deterministic output.
- Use `request.base_params.model_dump()` as the base dict.
- Convert each generated dict through `BacktestParams(**params)` so existing validation catches invalid strategy params.
- Filter invalid combinations only when `BacktestParams` validation fails due `sell_high <= buy_low`; other validation errors should propagate.
- Raise `ValueError("parameter grid produced no valid combinations")` if all combinations are invalid.

- [ ] **Step 3: Verify grid tests pass**

Run:

```bash
cd backend && python3 -m pytest tests/test_experiment_grid_service.py -v
```

Expected: pass.

---

## Task 3: StrategyExperimentService

**Files:**
- Create: `backend/app/services/strategy_experiment_service.py`
- Test: `backend/tests/test_strategy_experiment_service.py`

- [ ] **Step 1: Write failing service tests**

Use isolated SQLite setup following existing service tests. Cover:

1. `create_experiment()` stores base params/grid JSON and `estimated_runs`.
2. `run_experiment()` parses CSV once, creates one run per valid parameter combination, and updates `COMPLETED` status.
3. one failed run does not abort remaining runs; failed run has `status="FAILED"`, experiment has `failed_runs > 0`.
4. `list_runs()` sorts by `total_return_pct desc` and paginates.

Run:

```bash
cd backend && python3 -m pytest tests/test_strategy_experiment_service.py -v
```

Expected: fail because service file is missing.

- [ ] **Step 2: Implement create and serialization helpers**

Service constructor:

```python
class StrategyExperimentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.grid = ExperimentGridService()
```

Use `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))` for persisted JSON, where `payload` is the dict returned by `model_dump()`.

- [ ] **Step 3: Implement run_experiment**

Requirements:

- If experiment is missing, raise `ValueError("strategy experiment not found")`.
- If `csv_text` is present, parse with `parse_backtest_csv`.
- If `price_points` is present, convert to `BacktestBar`.
- For each expanded `BacktestParams`, construct `BacktestEngineParams` field-by-field from the Pydantic model (`symbol`, `buy_low`, `sell_high`, `short_selling`, `min_profit_amount`, `max_daily_loss`, `max_consecutive_losses`, `quantity`, `initial_cash`, `fee_rate`, `fixed_fee`, `slippage_pct`, `stop_loss_pct`) and call `BacktestEngine(engine_params).run(bars)` using the same mapping as `backend/app/api/backtest.py`.
- Store only summary fields plus a compact `result_summary_json` containing `metrics`, first 20 `trades`, and first 200 `equity_curve` points.
- Commit once after all runs are added.

- [ ] **Step 4: Implement list_runs**

Allowed sort fields:

```python
{"total_return_pct", "total_pnl", "max_drawdown_pct", "win_rate", "trade_count", "created_at"}
```

Invalid sort raises `ValueError("unsupported sort field")`. `page` starts at 1. `page_size` max is 100.

- [ ] **Step 5: Verify service tests pass**

Run:

```bash
cd backend && python3 -m pytest tests/test_strategy_experiment_service.py -v
```

Expected: pass.

---

## Task 4: API router

**Files:**
- Create: `backend/app/api/strategy_experiments.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_strategy_experiments_api.py`

- [ ] **Step 1: Write failing API tests**

Cover:

1. `POST /api/strategy-experiments` returns `PENDING` and estimated count.
2. `POST /api/strategy-experiments/{id}/run` returns `COMPLETED`.
3. `GET /api/strategy-experiments/{id}/runs` returns sorted page.
4. invalid grid returns 422 for schema errors.
5. unknown experiment returns 404.
6. unsupported sort returns 400.

Run:

```bash
cd backend && python3 -m pytest tests/test_strategy_experiments_api.py -v
```

Expected: fail because router is missing.

- [ ] **Step 2: Implement router**

Use:

```python
router = APIRouter(prefix="/api/strategy-experiments", tags=["strategy-experiments"])
```

Endpoints:

```python
@router.post("", response_model=StrategyExperimentResponse)
def create_strategy_experiment(
    payload: StrategyExperimentCreate,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    return StrategyExperimentService(db).create_experiment(payload)

@router.get("", response_model=list[StrategyExperimentResponse])
def list_strategy_experiments(db: Session = Depends(get_db)) -> list[StrategyExperimentResponse]:
    return StrategyExperimentService(db).list_experiments()

@router.get("/{experiment_id}", response_model=StrategyExperimentResponse)
def get_strategy_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    try:
        return StrategyExperimentService(db).get_experiment(experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.post("/{experiment_id}/run", response_model=StrategyExperimentResponse)
def run_strategy_experiment(
    experiment_id: int,
    payload: StrategyExperimentRunRequest,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    try:
        return StrategyExperimentService(db).run_experiment(experiment_id, payload)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "strategy experiment not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc

@router.get("/{experiment_id}/runs", response_model=StrategyExperimentRunPage)
def list_strategy_experiment_runs(
    experiment_id: int,
    sort: str = "total_return_pct",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> StrategyExperimentRunPage:
    try:
        return StrategyExperimentService(db).list_runs(experiment_id, sort, order, page, page_size)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "strategy experiment not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
```

Map `ValueError("strategy experiment not found")` to 404. Map other service `ValueError` to 400.

- [ ] **Step 3: Include router**

In `backend/app/main.py`, import and include `strategy_experiments_router` near existing API routers.

- [ ] **Step 4: Verify API tests pass**

Run:

```bash
cd backend && python3 -m pytest tests/test_strategy_experiments_api.py -v
```

Expected: pass.

---

## Task 5: Frontend types and API client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/api/strategy_experiments.ts`

- [ ] **Step 1: Add strict TypeScript types**

Add interfaces mirroring backend contract:

```ts
export interface StrategyExperimentGridRange { start: number; end: number; step: number }
export interface StrategyExperimentGridItem { value?: number; values?: number[]; range?: StrategyExperimentGridRange }
export type StrategyExperimentGrid = Partial<Record<keyof BacktestParams, StrategyExperimentGridItem>>
export interface StrategyExperimentCreate { name: string; symbol: string; base_params: BacktestParams; parameter_grid: StrategyExperimentGrid }
export interface StrategyExperimentRunRequest { csv_text?: string | null }
export interface StrategyExperiment { id: number; name: string; symbol: string; status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED'; estimated_runs: number; completed_runs: number; failed_runs: number; error: string; created_at: string; completed_at: string | null }
export interface StrategyExperimentRun { id: number; experiment_id: number; parameters: Partial<BacktestParams>; status: 'COMPLETED' | 'FAILED'; total_pnl: number; total_return_pct: number; max_drawdown_pct: number; win_rate: number; trade_count: number; closed_trade_count: number; error: string | null; created_at: string }
export interface StrategyExperimentRunPage { items: StrategyExperimentRun[]; total: number; page: number; page_size: number }
```

- [ ] **Step 2: Add API client**

Functions:

```ts
createStrategyExperiment(payload: StrategyExperimentCreate): Promise<StrategyExperiment>
listStrategyExperiments(): Promise<StrategyExperiment[]>
getStrategyExperiment(id: number): Promise<StrategyExperiment>
runStrategyExperiment(id: number, payload: StrategyExperimentRunRequest): Promise<StrategyExperiment>
listStrategyExperimentRuns(id: number, params: { sort: string; order: 'asc' | 'desc'; page: number; page_size: number }): Promise<StrategyExperimentRunPage>
```

Use shared `api` from `./client`.

- [ ] **Step 3: Type-check frontend**

Run:

```bash
cd frontend && npm run type-check
```

Expected: pass.

---

## Task 6: Experiments page and navigation

**Files:**
- Create: `frontend/src/views/Experiments.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Test: `frontend/cypress/e2e/strategy_experiments.cy.ts`
- Modify: `frontend/cypress/support/e2e.ts`

- [ ] **Step 1: Add Cypress failing test**

Test flow:

1. Visit `/#/experiments`.
2. Fill name/symbol/base params and CSV.
3. Add grid values for `buy_low` and `sell_high`.
4. Intercept create/run/runs APIs.
5. Click run.
6. Assert leaderboard rows render sorted metrics.

Run:

```bash
cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy_experiments.cy.ts
```

Expected: fail because route/page is missing.

- [ ] **Step 2: Implement route and nav**

- Import `Experiments` in `router/index.ts` and add `{ path: '/experiments', component: Experiments }`.
- Add desktop nav item `<el-menu-item index="/experiments">策略实验</el-menu-item>` after “回测”。
- Do not add bottom-nav item in this phase.

- [ ] **Step 3: Implement `Experiments.vue`**

Page structure:

1. `el-card` “创建实验”：name, symbol, base params fields, CSV textarea.
2. Grid editor MVP: text inputs for comma-separated `buy_low` and `sell_high`; numeric inputs for fixed `quantity`, `fee_rate`, `slippage_pct`.
3. `运行实验` button: create experiment, then call run, then load runs.
4. Leaderboard table: parameters, total PnL, return %, max drawdown %, win rate, trade count, status/error.
5. Sort controls: Element Plus select for sort field and order.

Error handling:

- API errors call `ElMessage.error` with backend detail if present.
- Button disables while running.
- Empty CSV shows local error and does not call API.

- [ ] **Step 4: Add support intercept defaults**

Update `frontend/cypress/support/e2e.ts` `stubApi()` so unrelated specs do not fail on new nav/route. Provide empty default responses for:

- `GET /api/strategy-experiments`
- `GET /api/strategy-experiments/*/runs*`

- [ ] **Step 5: Verify frontend checks**

Run:

```bash
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy_experiments.cy.ts
```

Expected: all pass.

---

## Task 7: Final verification and documentation

**Files:**
- Modify after implementation: `docs/Roadmap.md`

- [ ] **Step 1: Run targeted backend tests**

```bash
cd backend && python3 -m pytest \
  tests/test_experiment_grid_service.py \
  tests/test_strategy_experiment_service.py \
  tests/test_strategy_experiments_api.py \
  tests/test_database.py \
  -v
```

Expected: pass.

- [ ] **Step 2: Run backend type check**

```bash
cd backend && python3 -m basedpyright
```

Expected: `0 errors, 0 warnings`.

- [ ] **Step 3: Run frontend checks**

```bash
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run -- --spec cypress/e2e/strategy_experiments.cy.ts
```

Expected: all pass.

- [ ] **Step 4: Update Roadmap after verified implementation**

Only after all checks pass, update `docs/Roadmap.md`:

- Mark P16 as completed.
- Record exact observed pytest count and frontend command results.
- State P17 remains next and depends on P16.

- [ ] **Step 5: Inspect diff; do not commit unless explicitly requested**

Run:

```bash
git status --short
git diff --stat
```

Expected: changes limited to files listed in this plan plus generated lockfile changes only if npm actually changed dependencies. No commit is made without explicit user request.

---

## Self-review

- Spec coverage: Phase 1 covers T1/T2/T3 from `2026-05-29-strategy-experiment-validation-platform-design.md`; T4/T5 are explicitly deferred to P17.
- API conflict avoided: this plan uses `/api/strategy-experiments`, not existing `/api/experiments`.
- Safety boundary preserved: no auto-apply to Strategy, no change to live trading path.
- Test coverage: grid, service, API, database migration, frontend type/build, Cypress primary workflow.
