# P11: 策略实验与验证平台 MVP 设计

> **日期：** 2026-05-29
> **代号：** P10
> **目标：** 把现有回测、LLM 交互、绩效追踪能力收敛成一个可比较、可导出、可复盘的策略实验平台。
> **前置阅读：** `docs/Roadmap.md`、`docs/superpowers/specs/2026-05-28-llm-prompt-engineering-optimization-design.md`

## 1. 背景与动机

当前系统已经具备实盘区间交易、CSV 回测、LLM 区间顾问、Prompt A/B 测试、PerformanceTracker 和复盘页面。下一阶段的主要问题不再是“能不能交易”，而是：

1. **参数是否可靠**：单次回测只能验证一组参数，无法比较不同 `buy_low` / `sell_high` / 手续费 / 滑点假设下的稳定性。
2. **LLM 是否有效**：系统已经保存 LLM 建议和后续交易事件，但缺少统一评分来判断建议是否带来收益或降低风险。
3. **实验不可复用**：回测结果、LLM 表现和人工分析分散在不同页面/API，难以沉淀为可导出的研究记录。

P10 的目标是建设一个 MVP 级“研究台”：先用批量回测和排行榜提升参数验证效率，再把 LLM 建议纳入事后评分，最后提供结构化导出。它不直接改变实盘交易行为。

## 2. 范围与切除

### 范围（Task 拆分）

| Task | 主题 | 主要交付 | 依赖 |
|------|------|----------|------|
| **T1** | 批量回测核心 | 参数网格生成器 + 批量执行服务 + 实验结果指标 | 现有 `BacktestEngine` |
| **T2** | 实验持久化与 API | `strategy_experiments` / `strategy_experiment_runs` 表 + CRUD / run / export API | T1 |
| **T3** | 排行榜 UI | Experiments 页面或 Backtest 页面扩展；排序、筛选、详情展开 | T2 |
| **T4** | LLM 建议事后评分 | 基于 `llm_interactions`、订单、事件、状态快照计算建议结果 | T2 |
| **T5** | 导出与推荐带回 | CSV/JSON 导出；将最佳参数填入 Strategy 表单草稿但不自动保存 | T2、T3 |
| **T6** | 测试与验证 | pytest + frontend type-check + Cypress 主流程 | 全部 |

### 显式 YAGNI 切除

- 不自动把实验最佳参数应用到实盘策略。
- 不做机器学习优化器、贝叶斯搜索或遗传算法；MVP 只做显式参数网格。
- 不新增多标的自动交易能力；多标的只能作为实验数据维度或观察对象。
- 不让 LLM 直接生成并上线实盘参数；LLM 最多用于总结实验结果。
- 不引入外部行情数据源；首版复用 CSV 回测输入和已有长桥 K 线能力。
- 不做权限系统扩展；继续沿用当前内网部署假设。

## 3. 用户流程

### 3.1 批量参数实验

1. 用户进入 Experiments 页面，选择历史数据来源：粘贴/上传 CSV，或复用 Backtest 页面已有输入。
2. 用户配置参数范围：`buy_low`、`sell_high`、`min_profit_amount`、`stop_loss_pct`、`fee_rate`、`slippage_pct`。
3. 系统展开参数网格，展示预计组合数；超过上限时要求缩小范围。
4. 用户运行实验，后端逐组调用现有 `BacktestEngine`。
5. 页面展示排行榜：总收益、最大回撤、胜率、交易次数、盈亏比、Sharpe（如已有指标不足则本迭代补齐）。

### 3.2 LLM 建议评分

1. 用户选择日期范围、symbol、prompt version / experiment variant。
2. 系统读取 LLM 建议，并在建议后固定窗口内评估结果。
3. 每条建议得到标签：`EFFECTIVE`、`INEFFECTIVE`、`TOO_EARLY`、`TOO_LATE`、`RISKY`、`INSUFFICIENT_DATA`。
4. 页面展示整体命中率、平均收益、最大不利波动、样本数和失败原因分布。

### 3.3 推荐带回

1. 用户在排行榜选择一组参数。
2. 点击“带回 Strategy 草稿”。
3. 前端跳转 Strategy 页面并预填表单，但不自动调用 `PUT /api/strategy`。
4. 用户必须手动确认保存。

## 4. 数据模型

### 4.1 新表：`strategy_experiments`

```python
class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="CSV")
    parameter_grid: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(_TZDateTime(), nullable=True)
```

- `status` 枚举：`PENDING / RUNNING / COMPLETED / FAILED`
- `parameter_grid` 存 JSON，记录用户输入的范围而非展开后的全部组合。

### 4.2 新表：`strategy_experiment_runs`

```python
class StrategyExperimentRun(Base):
    __tablename__ = "strategy_experiment_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("strategy_experiments.id"), index=True, nullable=False)
    parameters: Mapped[str] = mapped_column(Text, nullable=False)
    total_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
```

- `parameters` 存单次展开后的参数 JSON。
- `result_payload` 存 BacktestResult 的压缩摘要，不保存完整输入 CSV。
- 排行榜默认按 `total_return DESC, max_drawdown ASC` 排序。

### 4.3 运行时迁移补丁

`init_db()` 增加幂等补丁：

1. `_ensure_strategy_experiments_table`
2. `_ensure_strategy_experiment_runs_table`

`alembic/` 不作为生产迁移入口，保持项目现有约定。

## 5. 后端架构

### 5.1 参数网格生成器

新增 `backend/app/services/experiment_grid_service.py`：

```python
class ExperimentGridService:
    def expand(self, grid: StrategyExperimentGrid) -> list[BacktestParams]: ...
    def estimate_count(self, grid: StrategyExperimentGrid) -> int: ...
```

- 输入支持三种形态：固定值、列表值、范围值（`start/end/step`）。
- 默认组合上限为 500，超过返回 400，避免一次请求长时间占用进程。
- 展开时校验 `buy_low < sell_high`、价格与费率为正数、步长不为 0。

### 5.2 批量执行服务

新增 `backend/app/services/strategy_experiment_service.py`：

```python
class StrategyExperimentService:
    def create_experiment(self, request: StrategyExperimentCreate) -> StrategyExperimentRead: ...
    def run_experiment(self, experiment_id: int, price_points: list[PricePoint]) -> StrategyExperimentRead: ...
    def list_runs(self, experiment_id: int, sort: str, page: int, page_size: int) -> StrategyExperimentRunPage: ...
    def export_experiment(self, experiment_id: int, format: str) -> Response: ...
```

- 同步执行即可；MVP 不引入后台队列。
- 单组失败不终止整个实验，记录该 run 的错误摘要，并将实验状态标为 `COMPLETED`，除非全部失败。
- 服务层复用现有 `BacktestEngine`，不复制回测逻辑。

### 5.3 LLM 建议评分服务

新增或扩展 `PerformanceTracker`：

```python
class LLMRecommendationEvaluator:
    def evaluate(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        horizon_minutes: int,
    ) -> LLMRecommendationEvaluationSummary: ...
```

评分规则：

| 标签 | 判定 |
|------|------|
| `EFFECTIVE` | 建议方向后的窗口内达到目标价或收益为正，且最大不利波动在阈值内 |
| `INEFFECTIVE` | 窗口结束后收益不达标，且没有更具体失败原因 |
| `TOO_EARLY` | 建议后先产生显著不利波动，随后才达成方向 |
| `TOO_LATE` | 建议出现时目标走势已基本完成，后续收益空间不足 |
| `RISKY` | 最大不利波动超过配置阈值 |
| `INSUFFICIENT_DATA` | 缺少后续价格、订单或状态快照 |

首版阈值使用请求参数或安全默认值，不新增实盘配置字段。

## 6. API 设计

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/strategy-experiments` | 创建实验，保存参数网格 |
| `POST` | `/api/strategy-experiments/{id}/run` | 上传/提交价格序列并执行批量回测 |
| `GET` | `/api/strategy-experiments` | 分页列出实验 |
| `GET` | `/api/strategy-experiments/{id}` | 实验详情 |
| `GET` | `/api/strategy-experiments/{id}/runs` | 排行榜分页、排序 |
| `GET` | `/api/strategy-experiments/{id}/export?format=csv|json` | 导出实验结果 |
| `GET` | `/api/strategy-experiments/llm-evaluations` | LLM 建议评分汇总 |

请求/响应 schema 放在 `backend/app/schemas.py`，或按项目现有规模拆到专用 schema 文件时保持 API 层导入清晰。

## 7. 前端设计

### 7.1 页面入口

优先新增 `frontend/src/views/Experiments.vue`，路由为 `/#/experiments`。如果导航已过密，可以放在 Backtest 页面内作为“批量实验”Tab，但 API 和类型仍独立。

### 7.2 页面结构

1. **实验配置卡片**：名称、symbol、CSV 输入、参数网格编辑器、组合数预估。
2. **排行榜表格**：收益、回撤、胜率、交易次数、盈亏比、Sharpe；支持排序和分页。
3. **Run 详情抽屉**：展示参数、权益曲线摘要、交易明细摘要。
4. **LLM 评分卡片**：日期范围、horizon、标签分布、样本列表。
5. **操作区**：导出 CSV/JSON、带回 Strategy 草稿。

### 7.3 Strategy 草稿带回

- 使用 query 或临时 state 传递参数，例如 `/#/strategy?draftExperimentRunId=123`。
- Strategy 页面读取 run 参数后只填表单，不自动保存。
- 页面需显示提示：“来自实验结果，保存后才会影响实盘策略”。

## 8. 指标定义

| 指标 | 定义 |
|------|------|
| 总收益 | 回测最终权益 - 初始权益 |
| 最大回撤 | 权益曲线峰值到后续低点的最大跌幅 |
| 胜率 | 盈利交易数 / 已完成交易数 |
| 盈亏比 | 平均盈利 / 平均亏损绝对值；无亏损时使用 `null` 或约定上限 |
| Profit Factor | 毛利润 / 毛亏损绝对值；无亏损时使用 `null` |
| Sharpe | 基于权益曲线收益率的年化或非年化 Sharpe，首版必须在 UI 标注口径 |

如果现有 BacktestResult 已有同名指标，复用现有字段，避免重复计算口径漂移。

## 9. 错误处理

- 参数网格非法：返回 400，错误信息指出字段和原因。
- 组合数过大：返回 400，包含当前组合数和上限。
- CSV 解析失败：复用 Backtest API 的错误格式。
- 单组回测失败：记录到该 run 的 `result_payload.error`，排行榜默认隐藏失败 run，可切换显示。
- 导出空实验：返回 400，提示先运行实验。
- LLM 评分样本不足：返回空 summary，标签为 `INSUFFICIENT_DATA`，不伪造结论。

## 10. 测试计划

### 后端 pytest

- `test_experiment_grid_service.py`：固定值、列表值、范围值、组合数上限、非法参数。
- `test_strategy_experiment_service.py`：创建实验、批量运行、单组失败隔离、排序分页、导出。
- `test_strategy_experiments_api.py`：API schema、错误响应、CSV/JSON 导出。
- `test_llm_recommendation_evaluator.py`：六类评分标签、样本不足、窗口边界。

### 前端验证

- `npm run type-check`
- `npm run build`
- Cypress：创建实验、运行批量回测、排行榜排序、导出、带回 Strategy 草稿。

### 手工验证

- 使用一份小型 CSV 跑 3-5 组参数，确认排行榜与单次 Backtest 结果一致。
- 从最佳 run 带回 Strategy 页面，确认不自动保存、不触发实盘 API。

## 11. 分阶段交付建议

### Phase 1：批量回测与排行榜

- T1、T2、T3 的最小闭环。
- 交付后用户已经可以比较参数组合。

### Phase 2：LLM 建议评分

- T4 独立上线。
- 重点验证评分口径，避免过早把标签当作绝对真相。

### Phase 3：导出与 Strategy 草稿带回

- T5 完成研究记录沉淀和人工上线入口。
- 保持人工确认，不改变实盘安全边界。

## 12. 完成定义

1. 批量回测可基于同一份历史数据运行多组参数，并持久化实验与 run 结果。
2. 排行榜支持排序、分页、详情查看，核心指标口径明确。
3. LLM 建议评分可按 symbol / 时间范围 / horizon 输出 summary 和样本列表。
4. 实验结果支持 CSV/JSON 导出。
5. 最佳参数只能带回 Strategy 草稿，必须人工保存才影响实盘。
6. 后端新增测试覆盖核心服务和 API；前端 type-check、build、Cypress 主流程通过。
