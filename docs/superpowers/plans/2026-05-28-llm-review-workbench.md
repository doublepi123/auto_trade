# P7 策略复盘与 LLM 优化工作台 Implementation Plan

> **For agentic workers:** 按任务串行推进，每个任务含验收标准。

**Goal:** 利用已保存的 LLM 交互、订单、交易事件数据，构建复盘工作台，分析哪些 LLM 建议真正赚钱，并支持导出。

**Architecture:** 后端新增 `/api/review` 路由，按日期/标的聚合 `LLMInteraction` + `OrderRecord` + `TradeEvent` + `RuntimeStateSnapshot`；前端新增 `Review.vue` 页面，展示时间线、价格走势、LLM 建议、执行结果、真实 PnL。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / Vue 3 + TypeScript + Element Plus / Cypress

**Baseline (2026-05-28):** `pytest 487 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

**Estimated Effort:** 5–6 天

**Target Completion:** 2026-06-04

---

## 迭代目标 (Sprint Goal)

> 用户可以在复盘页面按日期/标的查看价格走势、LLM 建议、执行结果、真实 PnL；支持导出复盘数据为 JSON/CSV；后端测试覆盖聚合 API，前端 Cypress 覆盖筛选与导出。

**完成定义 (Definition of Done):**
1. `GET /api/review` 返回按日期聚合的复盘数据（LLM 建议、订单、事件、PnL）。
2. `GET /api/review/export?format=json|csv` 支持导出。
3. 前端 Review 页面可筛选日期/标的，展示时间线卡片、PnL 汇总、错误类型标签。
4. `pytest` 新增 ≥15 项通过，`npm run type-check` 0 errors，`npm run build` 通过。
5. Cypress 新增 `review.cy.ts` 通过。

---

## Task 拆分

### T1：后端 API — 复盘数据聚合 (2 天)

**目标：** 新建 `ReviewService` + `GET /api/review` + `GET /api/review/export`。

**Files:**
- Create: `backend/app/services/review_service.py`
- Create: `backend/app/api/review.py`
- Modify: `backend/app/main.py`（mount router）
- Modify: `backend/app/schemas.py`（Review schemas）
- Test: `backend/tests/test_review.py`（新）

#### T1.1 — `ReviewService` 聚合逻辑

数据关联路径：
- `LLMInteraction` → `OrderRecord`（`LLMInteraction.order_id = OrderRecord.broker_order_id`）
- `TradeEvent` → `OrderRecord`（`TradeEvent.broker_order_id = OrderRecord.broker_order_id`）
- `RuntimeStateSnapshot` → 提供历史价格和 PnL

```python
class ReviewDay:
    date: str
    symbol: str
    llm_interactions: list[LLMInteraction]
    orders: list[OrderRecord]
    events: list[TradeEvent]
    snapshots: list[RuntimeStateSnapshot]
    daily_pnl: float
    trade_count: int
    error_tags: list[str]  # 过早买入、过早卖出、错过止损、频繁重挂、收益不足
```

聚合逻辑（按 `symbol` + `created_at` 日期分组）：
1. 查询 LLMInteraction（时间范围 + 标的）
2. 查询 OrderRecord（时间范围 + 标的）
3. 查询 TradeEvent（时间范围 + 标的）
4. 查询 RuntimeStateSnapshot（时间范围）
5. 按日期分组，计算 daily_pnl、trade_count、error_tags

**error_tags 计算规则：**
- `过早买入`：LLM 建议买入后，价格继续下跌 ≥2%
- `过早卖出`：LLM 建议卖出后，价格继续上涨 ≥2%
- `错过止损`：持仓亏损超过 `max_daily_loss` 的 50% 但未触发风控
- `频繁重挂`：同一标的一天内 ≥3 次 `ORDER_CANCELLED`
- `收益不足`：`ORDER_SKIPPED` with `skip_category='FEE'`

#### T1.2 — API 路由

```python
@router.get("/api/review")
def get_review(
    symbol: str = Query(...),
    from_date: str = Query(...),  # YYYY-MM-DD
    to_date: str = Query(...),
    db: Session = Depends(get_db),
) -> ReviewResponse

@router.get("/api/review/export")
def export_review(
    symbol: str = Query(...),
    from_date: str = Query(...),
    to_date: str = Query(...),
    format: Literal["json", "csv"] = Query("json"),
    db: Session = Depends(get_db),
) -> StreamingResponse
```

#### T1.3 — 测试

- `test_review.py`：mock 数据覆盖聚合逻辑、error_tags 计算、导出格式验证。

---

### T2：前端 — Review.vue 复盘页面 (2 天)

**目标：** 新建复盘页面，展示时间线、筛选、导出。

**Files:**
- Create: `frontend/src/views/Review.vue`
- Create: `frontend/src/api/review.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`（导航增加复盘入口）
- Modify: `frontend/src/types/index.ts`（Review types）
- Test: `frontend/cypress/e2e/review.cy.ts`

#### T2.1 — 页面布局

```
┌─────────────────────────────────────┐
│ 复盘工作台                            │
│ 标的 [AAPL.US ▼] 日期 [2026-05-01~05-28] │
├─────────────────────────────────────┤
│ 汇总卡片                              │
│ 总盈亏 +$1,234  交易次数 8  错误标签 [过早买入] │
├─────────────────────────────────────┤
│ 时间线（按天倒序）                      │
│ ┌─────────────────────────────────┐  │
│ │ 05-28 价格 $220.5 → $225.0      │  │
│ │ LLM: 建议买入 $220.0 (置信度 0.85) │ │
│ │ 执行: BUY 100@220.5 → SELL 100@225 │ │
│ │ PnL: +$450  标签: [收益不足]      │  │
│ └─────────────────────────────────┘  │
├─────────────────────────────────────┤
│ [导出 JSON] [导出 CSV]                │
└─────────────────────────────────────┘
```

#### T2.2 — 移动端适配

- 时间线卡片垂直堆叠
- 筛选器折叠为紧凑行
- 导出按钮位于底部固定区域

---

### T3：测试与验证 (1 天)

- `test_review.py` ≥15 项通过
- `review.cy.ts` 覆盖页面打开、筛选、导出
- `npm run type-check` + `npm run build` 通过
- 全量 `pytest` 不回退

---

## 建议执行顺序

| 天 | 任务 | 产出 |
|---|---|---|
| 1 | T1.1 ReviewService + schemas | 聚合逻辑可用，单元测试通过 |
| 2 | T1.2 API 路由 + T1.3 测试 | `/api/review` 可返回数据 |
| 3 | T2.1 Review.vue 页面骨架 | 页面可打开，展示时间线 |
| 4 | T2.1 时间线卡片 + 筛选 + 导出 | 功能完整，移动端适配 |
| 5 | T2.2 移动端 + T3 测试 | Cypress 通过，交付 commit |
| 6 | 缓冲 / PR 准备 | 更新 Roadmap.md |

---

## 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| `LLMInteraction.order_id` 为空导致无法关联 | 复盘数据不完整 | 左连接（LEFT JOIN），无 order_id 的 interaction 单独展示 |
| RuntimeStateSnapshot 数据量大 | 查询慢 | 按日期 + 标的索引；前端分页 |
| error_tags 规则争议 | 验收困难 | 规则文档化在代码注释中，测试覆盖每个规则 |
| 时间线 UI 复杂 | T2 延迟 | 先实现基础列表，再优化为时间线卡片 |

---

## 附录：数据关联图

```
LLMInteraction
  ├── order_id (可选) ──→ OrderRecord.broker_order_id
  ├── symbol
  ├── created_at
  ├── parsed_response (建议区间)
  └── applied (是否被采纳)

TradeEvent
  ├── broker_order_id (可选) ──→ OrderRecord.broker_order_id
  ├── event_type (ORDER_FILLED, ORDER_SKIPPED...)
  ├── symbol
  ├── created_at
  └── payload_json (skip_category, 原因)

OrderRecord
  ├── broker_order_id (PK)
  ├── symbol
  ├── side
  ├── quantity
  ├── executed_price
  ├── status
  └── created_at / filled_at

RuntimeStateSnapshot
  ├── daily_pnl
  ├── last_price
  └── created_at
```

**关联路径：** `LLMInteraction` → `OrderRecord` ← `TradeEvent`，`RuntimeStateSnapshot` 独立按时间匹配。
