# P10：LLM 优化工作台前端化 设计

> **日期：** 2026-05-29
> **代号：** P10（Roadmap 续 P9）
> **基线：** `pytest 549 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过（P9 交付后状态，commit `7abcf36`；后续以 CI 实际结果为准）
> **目标分支：** `main`
> **前置阅读：**
> - `docs/Roadmap.md`（P9 已交付，本迭代为续作）
> - `docs/superpowers/specs/2026-05-28-llm-prompt-engineering-optimization-design.md`（P9 设计，本迭代暴露其后端能力）

## 1. 背景与动机

P9（2026-05-29 交付）完成了 LLM prompt 工程优化，但**全部为后端能力**，前端零暴露：

- **A/B 实验**：`ABTestManager` + `PromptVersion` / `ExperimentResult` 表 + `/api/experiments/*`（版本 CRUD、激活、实验摘要）。
- **性能追踪**：`PerformanceTracker` + `/api/performance/*`（总览统计、变体对比、优化建议）。
- **扩展指标**：RSI(14)、MACD(12,26,9)、成交量分析、市场情绪、多时间框架对齐——计算于 `DataAggregator.fetch_market_data`，但仅注入 prompt，**不在任何 API 响应中暴露**。

经核查（2026-05-29），`frontend/src/` 对 `experiment` / `performance` / `sentiment` / `rsi` / `macd` 等关键字**零引用**。这些后端能力无法被用户使用：无法管理 prompt 版本、无法查看哪个变体胜率高、无法看到当前标的的技术指标。

P10 在**不改任何写路径、不改 LLM 决策逻辑**的前提下，新增一个只读「LLM 优化工作台」页面，把上述三类能力暴露给用户，形成「调 prompt → 看实验胜率 → 看当前指标」的闭环。

## 2. 范围与切除

### 范围

| Task | 主题 | 主要交付 | 依赖 |
|------|------|----------|------|
| **T1** | 后端只读端点补强 | `GET /api/experiments`（distinct 实验名）；`GET /api/indicators`；`/api/performance/*` schema 化 | — |
| **T2** | 前端 Lab 页骨架 | `/lab` 路由 + 导航项 + `Lab.vue`（el-tabs）+ `src/api/lab.ts` + types | T1 |
| **T3** | Tab 1 实验与版本管理 | 版本列表 / 新建 / 激活 / 实验摘要 | T2 |
| **T4** | Tab 2 性能看板 | 实验下拉 + 总览卡 + 变体对比表/柱状 + 建议列表 | T2 |
| **T5** | Tab 3 指标面板 | 指标卡片网格 + `available=false` 水印 | T2 |
| **T6** | 测试与文档 | pytest（~12 新增）+ Cypress（1 spec）+ README/CLAUDE/Roadmap 同步 | 全部 |

### 显式 YAGNI 切除

- ❌ **指标历史时间序列** —— 指标面板是「实时快照」；历史走势归 Review / Dashboard，本迭代不做指标的时间序列图。
- ❌ **实验变体的自动调度/灰度比例 UI** —— `ABTestManager.select_variant` 的分流逻辑不暴露配置；仅做版本激活（已存在的写端点）。
- ❌ **prompt 模板可视化编辑器 / 语法高亮** —— `template` 用等宽 textarea 全文编辑即可。
- ❌ **指标阈值可配置** —— RSI 超买超卖等色带阈值为前端常量。
- ❌ **新增数据库表 / 迁移** —— 全程只读复用现有表；性能端点 schema 化不改数据形状。
- ❌ **写路径改造** —— 不动 `TradeExecutionService` / `AppRunner` / `LLMAdvisorService` / `DataAggregator` 计算逻辑。
- ❌ **额外鉴权层** —— 沿用内网假设；写端点（版本创建/激活）与现有 strategy/credentials 写端点一致，不加新鉴权。

## 3. 架构与服务边界

```
前端 /lab (新路由 + 导航项)
  └── Lab.vue  (el-tabs)
        ├── Tab 1: 实验与 Prompt 版本   → /api/experiments/*
        ├── Tab 2: 性能追踪看板          → /api/performance/* + GET /api/experiments
        └── Tab 3: 指标面板             → GET /api/indicators
  └── src/api/lab.ts   (新 api 模块)
  └── src/types/index.ts  (追加类型)

后端补强 (全只读，除已存在的版本写端点)
  ├── GET /api/experiments              # 新增：distinct experiment_name 列表
  ├── GET /api/indicators?symbol=       # 新增：跑 DataAggregator.fetch_market_data，零 LLM 成本
  └── /api/performance/* response_model  # 新增 Pydantic schema，形状不变
```

**服务边界（单一职责、可独立测试）：**

- **`GET /api/indicators`**：仅调用现有 `DataAggregator(broker).fetch_market_data(symbol, market)`，把已算好的指标块原样转 schema 返回。**不**新建计算逻辑、**不**调 `LLMAdvisorService`、**不**写任何表。因此零 LLM 成本，仅消耗 broker 行情 quota（走 P5+ retry）。`symbol` 缺省时取 `StrategyConfig.symbol`；`market` 同源。broker 不可用/抛错 → `available=false` + 指标为 None，记 warning 不抛。
- **`GET /api/experiments`（无参）**：返回 `ExperimentResult.experiment_name` 去重列表，供 Tab 2 发现可用实验。与已有 `GET /api/experiments/{name}/summary` 路由区分（前者无路径参数）。
- **`/api/performance/*` schema 化**：`/stats`、`/compare`、`/recommendations` 当前返回裸 dict / list；补 `response_model`（`PerformanceStats`、`list[PerformanceVariant]`、`list[str]`），dict 形状已稳定，仅做类型固化，**行为不变**。

## 4. API 与 Schema

### 4.1 新增 / 改造端点

| Method | Path | Query | 返回 | 状态 |
|---|---|---|---|---|
| `GET` | `/api/experiments` | — | `list[str]`（实验名） | **新增** |
| `GET` | `/api/indicators` | `symbol`（可选） | `IndicatorsResponse` | **新增** |
| `GET` | `/api/performance/stats` | `experiment`（必填） | `PerformanceStats` | 加 response_model |
| `GET` | `/api/performance/compare` | `experiment`（必填） | `list[PerformanceVariant]` | 加 response_model |
| `GET` | `/api/performance/recommendations` | `experiment`（必填） | `list[str]` | 加 response_model |

**复用既有端点（不改）**：`GET /api/experiments/versions`、`POST /api/experiments/versions`、`POST /api/experiments/versions/{id}/activate`、`GET /api/experiments/versions/active`、`GET /api/experiments/{name}/summary`。

**状态码约定**（与 Replay 一致）：
- `422`：参数非法（`/api/performance/*` 缺 `experiment`；`/api/indicators` symbol 无法解析且配置缺省）。
- `200`：参数合法即使空数据也返回空结构（`stats` 零值、`compare`/列表为 `[]`、指标 `available=false`）；不为「合法但空」使用 `404`。

**鉴权**：GET 全部不挂 `require_api_key`（与 `/api/events`、`/api/status`、`/api/replay` 一致）。版本写端点维持现状。

### 4.2 Schemas（追加 `backend/app/schemas.py`）

```python
class PerformanceStats(BaseModel):
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float

class PerformanceVariant(BaseModel):
    variant: str
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float

class MacdValue(BaseModel):
    macd: float
    signal: float
    histogram: float

class VolumeAnalysis(BaseModel):
    avg_volume: float
    volume_ratio: float
    trend: str

class SentimentValue(BaseModel):
    sentiment: str          # bearish | neutral | bullish
    score: float
    description: str

class IndicatorsResponse(BaseModel):
    available: bool
    symbol: str
    market: str
    atr: float | None = None
    rsi: float | None = None
    macd: MacdValue | None = None
    volume_analysis: VolumeAnalysis | None = None
    sentiment: SentimentValue | None = None
    multi_timeframe: dict[str, Any] | None = None  # 透传 DataAggregator 的对齐结果
    bollinger: dict[str, float] | None = None        # 若 fetch_market_data 提供
```

> 注：`multi_timeframe` / `bollinger` 的内部结构透传自 `DataAggregator.fetch_market_data` 现有输出，实施时按其实际键名固定字段；若形状复杂保留 `dict[str, Any]` 透传，前端按已知键渲染。

## 5. 前端

### 5.1 路由与导航

- `frontend/src/router/index.ts`：在 `/watchlist` 之后增 `/lab` 路由（lazy）。
- `frontend/src/App.vue`：导航栏末尾插入「Lab」（或「优化工作台」）入口。

### 5.2 `Lab.vue` 结构

```text
<el-tabs v-model="activeTab" lazy>
  <el-tab-pane label="实验与版本" name="experiments"> ... </el-tab-pane>
  <el-tab-pane label="性能看板"   name="performance"> ... </el-tab-pane>
  <el-tab-pane label="指标面板"   name="indicators">  ... </el-tab-pane>
</el-tabs>
```

Tab 切换懒加载，不在挂载时一次性全拉；各 Tab 内做请求去抖 / 防重复。

### 5.3 Tab 1 — 实验与 Prompt 版本管理

- **版本列表**：`GET /api/experiments/versions` → 表格（name / version / description / is_active / created_at），激活版本高亮。
- **新建版本**：表单（name / version / description / template 等宽多行 textarea）→ `POST /api/experiments/versions`；失败时 toast 展示后端 `detail`（400）。
- **激活**：行内「设为激活」→ `POST /api/experiments/versions/{id}/activate`，成功后刷新列表。
- **实验摘要**：实验名下拉（数据源 = `GET /api/experiments`）→ `GET /api/experiments/{name}/summary` → 各变体 `total_count / profitable_count / avg_pnl / win_rate` 表格。

### 5.4 Tab 2 — 性能追踪看板

- 顶部实验名下拉（`GET /api/experiments`）；空 → 提示「暂无实验数据」。
- 选定后并发拉：
  - `/api/performance/stats` → 总览卡片（total_trades / win_rate / total_pnl / avg_pnl）。
  - `/api/performance/compare` → 变体对比表 + 胜率/PnL 柱状（**复用现有 SVG 图表栈，不引新库**）。
  - `/api/performance/recommendations` → 后端生成的中文建议，原样渲染为 list。

### 5.5 Tab 3 — 指标面板

- symbol 输入（默认取 `StrategyConfig.symbol`）→ `GET /api/indicators?symbol=`。
- 卡片网格：
  - **RSI(14)**：数值 + 超买(≥70)/超卖(≤30)色带（阈值前端常量）。
  - **MACD**：macd / signal / histogram。
  - **成交量分析**：均量 / 量比 / 趋势。
  - **市场情绪**：bearish/neutral/bullish + score + 描述。
  - **多时间框架**：日/周趋势 + alignment 信号。
  - **ATR / 布林带**。
- `available=false` → 整页水印「行情不可用（broker 凭证缺失或限流）」。
- 文案明示「实时快照」，与 Review（历史复盘）语义区分。

### 5.6 API Client 与 Types

`frontend/src/api/lab.ts`：

```ts
export async function listPromptVersions(): Promise<PromptVersion[]>
export async function createPromptVersion(payload: PromptVersionCreate): Promise<PromptVersion>
export async function activatePromptVersion(id: number): Promise<void>
export async function listExperimentNames(): Promise<string[]>
export async function getExperimentSummary(name: string): Promise<ExperimentSummary[]>
export async function getPerformanceStats(experiment: string): Promise<PerformanceStats>
export async function comparePerformanceVariants(experiment: string): Promise<PerformanceVariant[]>
export async function getPerformanceRecommendations(experiment: string): Promise<string[]>
export async function getIndicators(symbol?: string): Promise<IndicatorsResponse>
```

`frontend/src/types/index.ts` 追加：`PromptVersion`、`PromptVersionCreate`、`ExperimentSummary`、`PerformanceStats`、`PerformanceVariant`、`IndicatorsResponse`、`MacdValue`、`VolumeAnalysis`、`SentimentValue`。

## 6. 错误处理与容错

| 情况 | 行为 |
|------|------|
| broker 凭证缺失 / 限流 / 抛错（指标） | `200 + {available:false, indicators:null}`，记 warning 不抛；前端水印 |
| 空实验数据（stats） | 后端返回零值结构；前端展示「暂无实验数据」 |
| 空变体 / 空摘要 | 返回 `[]`；前端空态提示 |
| `/api/performance/*` 缺 `experiment` | `422` |
| `/api/indicators` symbol 缺省且配置无 symbol | `422` |
| 版本创建失败 | 后端 `400 + detail`；前端 toast |

## 7. 测试与验证

### 7.1 Backend pytest（~12 新增）

| 文件 | 覆盖 |
|------|------|
| `test_indicators_api.py` | mock broker 返回 K 线 → 指标块字段齐全且 `available=true`；broker=None → `available=false`、指标 None；symbol 缺省且配置缺失 → 422；symbol 取配置默认值路径 |
| `test_experiments_api.py` | 新增 `GET /api/experiments`（distinct names，含空库 → `[]`）；复用版本 CRUD + activate 回归；summary 空/非空 |
| `test_performance_api.py` | `/stats`、`/compare`、`/recommendations` 的 `response_model` 校验（schema 化后形状不变）；缺 `experiment` → 422；空实验 → 零值/空数组 |

### 7.2 Cypress（1 spec）

`frontend/cypress/e2e/lab.cy.ts`：
- 进入页三 Tab 可切换。
- Tab 1：版本列表渲染；新建版本表单提交（stub）；激活按钮触发 `POST .../activate`。
- Tab 2：实验下拉切换触发 `/stats` + `/compare` + `/recommendations`（stub）；空实验空态。
- Tab 3：指标卡片渲染；`available=false` 时水印。

### 7.3 Lint

- `basedpyright` 0 / 0。
- `npm run type-check` 通过；`npm run build` 通过。

### 7.4 手工验证

- [ ] 旧 DB（含 P9 实验数据）：Tab 2 下拉列出实验名，选中后三块数据正确。
- [ ] broker 凭证空：Tab 3 展示水印，不报错；其他 Tab 正常。
- [ ] 新建一个 prompt 版本 → 激活 → `GET /api/experiments/versions/active` 反映新版本。
- [ ] 指标面板 symbol 默认取当前 `StrategyConfig.symbol`，手动改 symbol 重新拉取。

## 8. 风险与回滚

| 风险 | 缓解 |
|------|------|
| `DataAggregator.fetch_market_data` 返回结构与 schema 不符 | 实施 T1 时先核对实际键名再固定 `IndicatorsResponse` 字段；复杂结构用 `dict[str, Any]` 透传 |
| 指标端点消耗 broker quota | 仅在 Tab 3 打开且手动触发时调用；不在页面挂载时自动拉 |
| 版本激活误操作影响线上 LLM | 激活前二次确认弹窗；激活是已存在的写端点，行为不变 |
| 性能 dict → schema 形状漂移 | schema 字段与 `PerformanceTracker` 返回 key 一一对应，加测试守护 |

**回滚**：新增端点全只读 + 复用既有写端点；删 `/api/indicators`、`/api/experiments`（list）、`/lab` 路由、`Lab.vue`、`lab.ts`、导航项即回滚。性能 schema 化可单独保留（向后兼容）。无数据库迁移，无 `_ensure_*` 改动。

## 9. 交付顺序

1. **T1** — 后端只读端点（`/api/indicators`、`/api/experiments` list、performance schema 化）+ 单元测试
2. **T2** — 前端 `/lab` 路由 + `Lab.vue` 骨架 + `lab.ts` + types
3. **T3** — Tab 1 实验与版本管理
4. **T4** — Tab 2 性能看板
5. **T5** — Tab 3 指标面板
6. **T6** — Cypress + README / CLAUDE / Roadmap 文档同步 + 全量 lint

## 10. 不在本迭代

- 指标历史时间序列图（归 Dashboard / Review）
- A/B 变体分流比例 / 灰度调度 UI
- prompt 模板可视化编辑器 / 语法高亮
- 指标阈值环境变量化或 UI 配置
- 新增数据库表 / 迁移
- 任何写路径 / LLM 决策逻辑改造
