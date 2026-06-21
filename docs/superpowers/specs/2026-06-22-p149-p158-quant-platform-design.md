# P149–P158：从区间交易系统到可插拔量化平台

> **日期：** 2026-06-22  
> **状态：** 设计待审  
> **作者：** Claude Code  

---

## 1. 背景与动机

当前 `auto_trade` 已经交付：

- 单标的区间交易策略（flat/long/short 状态机）
- 长桥 SDK 集成、风控、订单执行、审计、多渠道通知
- 回测引擎、参数扫描、walk-forward、stress test
- 多标的行情监控与 LLM 调度
- Dashboard / Strategy / History / Decision Timeline / Lab / Backtest / Credentials / AlertRules / NotificationCenter 等前端页面

系统运行稳定，但架构上仍是「一个策略 + 一个状态机 + 一个配置」的烟囱式结构。要继续扩展策略类型、支持组合交易、接入 ML 因子研究，必须先把核心抽象从「具体策略」升级为「策略插件 + 统一事件流 + 组合级执行」。

本规格定义 P149–P158 共 10 轮迭代，目标是把项目重构为类 Nautilus/Lean 的可插拔量化研究与执行平台。

---

## 2. 愿景

> **同一套策略代码、同一套事件语义、同一套撮合与风控，能在回测、paper、实盘三种模式下复用。**

用户未来可以：

1. 用 SDK 写一个双均线策略插件；
2. 在数据层研究因子并缓存特征；
3. 用 Paper Broker 验证滑点/部分成交/撤改单行为；
4. 把多个策略分配到不同标的，形成组合；
5. 用组合级风控约束总敞口；
6. 用 ML/LLM 生成候选因子或解释失败案例；
7. 通过版本管理与 A/B 实验灰度上线新策略。

---

## 3. 10 轮功能清单与依赖关系

```
P149 策略插件 SDK
   │
   ▼
P150 统一事件回放与实盘语义  ← 整个平台的地基
   │
   ├──▶ P153 数据与因子研究层
   │
   ├──▶ P154 ML/LLM 训练闭环
   │
   ├──▶ P152 Paper Broker / 真实成交仿真
   │        │
   │        ▼
   │    P155 高级执行算法
   │
   ├──▶ P151 组合级多标的交易
   │        │
   │        ▼
   │    P156 组合级风控与熔断
   │        │
   │        ▼
   │    P157 绩效归因与组合报告
   │
   └──▶ P158 策略版本、实验与灰度部署
```

| 代号 | 功能 | 一句话目标 |
|------|------|-----------|
| **P149** | 策略插件 SDK | 定义 `Strategy` 接口、信号、订单意图、参数 schema、注册表；把现有区间策略迁移为第一个插件 |
| **P150** | 统一事件回放与实盘语义 | 行情/信号/风控/下单/成交/状态快照统一成可 replay 的事件流，回测/paper/live 共用同一事件循环 |
| **P151** | 组合级多标的交易 | 在现有 watchlist/runtime 基础上实现资金分配、每标的风险预算、总持仓暴露、再平衡建议、组合级 kill switch |
| **P152** | Paper Broker / 真实成交仿真 | partial fill、滑点模型、撮合延迟、订单状态机、撤改单、成交概率；回测与演练都走它 |
| **P153** | 数据与因子研究层 | 建统一数据仓（K线/成交/订单/LLM/指标），加入数据质量检查、因子表达式、特征缓存、实验 artifact |
| **P154** | ML/LLM 训练闭环 | 模型注册、训练任务、walk-forward 训练、线上推理、模型漂移监控；LLM 从建议升级为生成候选因子/解释失败 |
| **P155** | 高级执行算法 | TWAP/VWAP、Iceberg、OCO、trailing stop、reduce-only/post-only 语义映射 |
| **P156** | 组合级风控与熔断 | 组合敞口、相关性风险、回撤熔断、VaR 风格指标，替代单标的 risk controller |
| **P157** | 绩效归因与组合报告 | Brinson/收益分解风格的组合绩效报告，替代现有单标的 PnL 面板 |
| **P158** | 策略版本、实验与灰度部署 | 策略版本注册、A/B 实验分配、灰度上线、一键回滚 |

---

## 4. 设计原则

1. **事件优先**：所有状态变更必须来自事件；事件必须可序列化、可回放、可审计。
2. **插件隔离**：策略插件只通过 SDK 接口与系统交互，不能直接操作 broker/DB/runner。
3. **模式统一**：回测、paper、live 三种模式共享事件流、撮合语义、风控语义。
4. **渐进替换**：先让新抽象与旧实现并存，再逐步迁移；不允许一次性大爆炸式重构。
5. **测试即文档**：每个新抽象必须有对应的单元测试 + 事件回放测试 + Cypress 端到端测试。
6. **生产就绪优先**：P149–P152 是地基，必须打牢后再进入 P153–P158 的研究/执行/部署层。

---

## 5. Phase 1：P149 + P150 — 地基

### 5.1 P149 策略插件 SDK

#### 5.1.1 核心抽象

```python
# app/platform/sdk/__init__.py

class Signal(NamedTuple):
    symbol: str
    timestamp: datetime
    signal_type: Literal["ENTRY", "EXIT", "MODIFY", "CANCEL"]
    side: Literal["BUY", "SELL"] | None
    price: Decimal | None
    quantity: int | None
    reason: str
    params: dict[str, Any]

class OrderIntent(NamedTuple):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    order_type: Literal["MARKET", "LIMIT", "STOP_LIMIT", "OCO"]
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_in_force: Literal["DAY", "GTC", "IOC", "FOK"]
    algo: str | None  # TWAP/VWAP/Iceberg/...
    reduce_only: bool
    post_only: bool
    reason: str

class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def parameter_schema(self) -> dict[str, Any]: ...

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[Signal]: ...

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[Signal]: ...

    def on_order_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[Signal]: ...

    def on_risk_event(self, ctx: StrategyContext, event: RiskEvent) -> list[Signal]: ...
```

#### 5.1.2 策略注册表

```python
# app/platform/registry.py

class StrategyRegistry:
    def register(self, strategy_class: type[Strategy]) -> None: ...
    def get(self, name: str) -> type[Strategy]: ...
    def list(self) -> list[StrategyMeta]: ...
```

- 启动时自动扫描 `app/strategies/` 目录下的模块。
- 内置策略：`IntervalStrategy`（现有区间策略迁移后的插件）。
- 后续可添加：`MovingAverageCrossStrategy`、`BreakoutStrategy`、`PairsStrategy`、`LLMHybridStrategy`。

#### 5.1.3 参数 schema

- 使用 JSON Schema 描述策略参数。
- 前端根据 schema 动态生成表单。
- 参数变更写审计，支持版本化（为 P158 做准备）。

#### 5.1.4 与现有代码的关系

- 新建 `app/platform/` 目录存放 SDK、注册表、事件定义、执行引擎。
- 现有 `app/core/engine.py` 的 flat/long/short 状态机逻辑提取到 `app/strategies/interval_strategy.py`。
- `AppRunner` 暂时保留，但新增 `PlatformRunner` 作为 P150 后的主入口；两者通过 feature flag 切换。

### 5.2 P150 统一事件回放与实盘语义

#### 5.2.1 事件类型

```python
# app/platform/events.py

class Event(ABC):
    id: UUID
    timestamp: datetime
    source: Literal["MARKET", "STRATEGY", "RISK", "BROKER", "EXECUTION", "SYSTEM"]
    symbol: str | None

class QuoteEvent(Event): ...
class BarEvent(Event): ...
class SignalEvent(Event): ...
class OrderEvent(Event): ...
class FillEvent(Event): ...
class RiskEvent(Event): ...
class SnapshotEvent(Event): ...
class ControlEvent(Event): ...  # START/STOP/PAUSE/KILL_SWITCH
```

#### 5.2.2 事件流架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────┐
│ 行情源       │────▶│ 事件总线     │────▶│ 策略插件         │
│ (broker/CSV) │     │ EventBus    │     │ (P149 SDK)      │
└─────────────┘     └──────┬──────┘     └────────┬────────┘
                            │                      │
                            ▼                      ▼
                    ┌─────────────┐        ┌─────────────┐
                    │ 风控引擎     │        │ 执行引擎     │
                    │ (P156)      │        │ (P155)      │
                    └──────┬──────┘        └──────┬──────┘
                           │                       │
                           ▼                       ▼
                    ┌─────────────┐        ┌─────────────┐
                    │ 状态快照     │        │ Broker/Paper│
                    │ SnapshotStore│       │ (P152)      │
                    └─────────────┘        └─────────────┘
```

#### 5.2.3 事件回放

- 所有事件按 `(timestamp, id)` 排序写入 `event_log` 表。
- `EventReplayer` 可以从 `event_log` 读取事件并重新驱动策略，用于：
  - 回测（从 CSV/历史行情生成事件）
  - 复盘（回放某日实盘事件）
  - 调试（重放导致 bug 的事件序列）
- 回放结果必须与实际运行结果逐事件一致（deterministic replay）。

#### 5.2.4 模式统一

| 模式 | 行情源 | 撮合/执行 | 风控 | 持久化 |
|------|--------|-----------|------|--------|
| 回测 | CSV/历史行情 | `PaperBroker` 简化撮合（P152 升级为完整仿真） | 与 live 相同 | 可选落 event_log |
| Paper | 实时行情 | `PaperBroker` 真实成交仿真（P152 实现） | 与 live 相同 | 落 event_log + 订单 |
| Live | 实时行情 | 真实 broker | 与 live 相同 | 落 event_log + 订单 |

---

## 6. Phase 2：P151 + P152 + P156 — 组合、仿真、风控

### 6.1 P151 组合级多标的交易

#### 6.1.1 组合定义

```python
class PortfolioConfig(BaseModel):
    symbols: list[str]
    allocations: dict[str, Decimal]  # 目标资金权重
    per_symbol_risk_budget: dict[str, Decimal]
    rebalance_threshold_pct: Decimal
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
```

#### 6.1.2 资金分配

- 每个 symbol 有独立的策略实例和运行时状态。
- `PortfolioAllocator` 根据目标权重和当前持仓计算再平衡订单。
- 再平衡建议以 `OrderIntent` 形式输出，需用户确认或配置自动执行。

#### 6.1.3 组合级 kill switch

- `PortfolioRiskController` 可一键平掉全部持仓。
- 写入 `CONTROL_KILL_SWITCH` 事件，影响所有 symbol runtime。

### 6.2 P152 Paper Broker / 真实成交仿真

#### 6.2.1 职责

- 替代当前回测引擎中的简化撮合，提供与真实 broker 尽可能一致的订单状态机。
- 支持 partial fill、滑点、撮合延迟、撤改单、成交概率。

#### 6.2.2 撮合模型

```python
class PaperBroker:
    def submit_order(self, intent: OrderIntent) -> OrderState: ...
    def cancel_order(self, order_id: str) -> OrderState: ...
    def modify_order(self, order_id: str, intent: OrderIntent) -> OrderState: ...
    def on_bar(self, bar: BarEvent) -> list[FillEvent]: ...
    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]: ...
```

- **价格模型**：Limit 单在价格触及时按 bar/quote 撮合；Market 单按下一 tick 撮合。
- **数量模型**：按挂单簿深度模拟 partial fill；可配置 `fill_probability`。
- **滑点模型**：固定滑点 / 波动率比例滑点 / 成交量影响模型。
- **延迟模型**：提交→ack、ack→fill 之间可配置随机延迟。

#### 6.2.3 与回测集成

- 回测不再直接驱动 `BacktestEngine`，而是驱动 `PlatformRunner(mode=BACKTEST)`。
- `PlatformRunner` 内部使用 `PaperBroker` 撮合。

### 6.3 P156 组合级风控与熔断

#### 6.3.1 风险维度

- **单标的风险**：保留现有 daily_pnl、consecutive_losses、max_daily_loss。
- **组合敞口**：总多头/空头/净敞口、行业/市场暴露。
- **回撤熔断**：组合累计回撤超过阈值时暂停或 flatten。
- **相关性风险**：监控组合内标的收益相关性，防止过度集中。
- **VaR 风格指标**：基于历史收益分布计算简单 VaR（可选，不引入复杂依赖）。

#### 6.3.2 事件驱动风控

- 风控引擎订阅 `FillEvent`、`BarEvent`、`QuoteEvent`。
- 触发风控时发出 `RiskEvent`，执行引擎根据事件决定暂停/flatten/限制新单。

---

## 7. Phase 3：P153 + P154 + P155 + P157 + P158 — 研究、ML、执行、归因、部署

### 7.1 P153 数据与因子研究层

#### 7.1.1 统一数据仓

- 新建 `app/platform/data/` 目录。
- 抽象 `DataSource`：行情、订单、事件、LLM 交互、指标快照。
- 统一按 `(symbol, timestamp, type)` 索引。

#### 7.1.2 因子表达式

```python
class Factor:
    name: str
    expression: str  # 如 "sma(close, 20) - sma(close, 60)"
    dependencies: list[str]
    cache_ttl: timedelta
```

- 内置简单因子：sma、ema、rsi、atr、bb、volume_ma。
- 因子结果缓存到 SQLite 或本地 parquet（后续可扩展）。

#### 7.1.3 数据质量检查

- 缺失 bar 检测、价格异常检测（high < low、跳空超过阈值）、重复检测。
- 质量问题写入 `DATA_QUALITY_ISSUE` 事件。

### 7.2 P154 ML/LLM 训练闭环

#### 7.2.1 模型注册

- `ModelRegistry`：记录模型版本、训练参数、特征集、性能指标。
- 模型文件存本地（`data/models/{name}/{version}/`）。

#### 7.2.2 训练任务

- 基于 walk-forward 切分训练/验证集。
- 支持 sklearn/lightgbm 等轻量模型；不强制使用神经网络。
- 训练任务异步执行，结果写入 `ModelTrainingRun`。

#### 7.2.3 线上推理

- 策略插件可以声明 `model_inputs`，运行时由 `FeatureEngine` 生成特征并调用模型。
- 推理结果作为 `SignalEvent` 的一部分。

#### 7.2.4 LLM 升级

- 从「给区间建议」升级为：
  - 生成候选因子（输出因子表达式 + 理由）
  - 解释策略失败案例（输入事件序列，输出归因分析）
  - 总结市场状态（输入多因子，输出简短判断）

#### 7.2.5 模型漂移监控

- 监控线上预测分布与训练分布的差异。
- 漂移超过阈值时发出 `MODEL_DRIFT` 警告。

### 7.3 P155 高级执行算法

#### 7.3.1 算法订单类型

- **TWAP**：按时间切片均匀下单。
- **VWAP**：按历史成交量分布下单。
- **Iceberg**：大单拆小单，隐藏真实数量。
- **OCO**：一对关联订单，一个成交另一个自动取消。
- **Trailing Stop**：跟踪最高价/最低价触发止损。

#### 7.3.2 语义映射

- 策略插件输出 `OrderIntent.algo`，执行引擎选择对应算法。
- 算法单内部拆分为多个子订单，子订单状态聚合为父订单状态。

### 7.4 P157 绩效归因与组合报告

#### 7.4.1 报告内容

- 组合总收益、年化收益、Sharpe、Sortino、Calmar、最大回撤。
- Brinson 归因：配置效应、选择效应、交互效应。
- 收益分解：市场 beta、行业、个股、汇率（多市场时）。
- 交易质量：滑点、部分成交率、撤单率、持仓时长分布。

#### 7.4.2 前端页面

- 新增 `/portfolio` 页面，替代现有单标的 PnL 面板。
- 支持选择时间区间、策略、标的组合。

### 7.5 P158 策略版本、实验与灰度部署

#### 7.5.1 策略版本

- 策略插件每次注册时记录版本哈希。
- `StrategyVersion` 表：name、version、params_schema、code_hash、created_at。

#### 7.5.2 A/B 实验

```python
class Experiment:
    name: str
    variants: list[Variant]  # 不同策略版本或参数
    allocation: dict[str, Decimal]  # 资金/标的分配比例
    start_at: datetime
    end_at: datetime | None
```

- 实验按 symbol 或资金维度分配。
- 实验结果写入 `ExperimentResult`，用于对比变体绩效。

#### 7.5.3 灰度部署

- 新策略版本先以 paper 模式在小比例资金上运行。
- 指标达标后切换到 live。
- 支持一键回滚到上一版本。

---

## 8. 架构演进路线图

### 8.1 当前架构（P148 之后）

```
前端 SPA
    │
    ▼
FastAPI ──▶ AppRunner ──▶ StrategyEngine (flat/long/short)
    │           │
    │           ▼
    │       BrokerGateway (longport)
    │           │
    ▼           ▼
SQLite (orders, runtime_state, audit_logs, ...)
```

### 8.2 目标架构（P158 之后）

```
前端 SPA
    │
    ▼
FastAPI ──▶ PlatformRunner ──▶ EventBus
    │                              │
    │    ┌─────────────┬───────────┼─────────────┐
    │    ▼             ▼           ▼             ▼
    │ StrategyPlugins  RiskEngine  ExecutionEngine  Data/FactorLayer
    │    │             │           │             │
    │    ▼             ▼           ▼             ▼
    │ SignalEvent   RiskEvent  OrderEvent  FeatureCache
    │                            │
    │                            ▼
    │                      BrokerGateway / PaperBroker
    │                            │
    ▼                            ▼
SQLite / Parquet (event_log, orders, models, ...)
```

---

## 9. 数据模型变化

### 9.1 新增表

| 表名 | 用途 |
|------|------|
| `event_log` | 统一事件流持久化 |
| `strategy_plugins` | 已注册策略插件元数据 |
| `strategy_versions` | 策略版本与参数 schema |
| `portfolio_configs` | 组合配置 |
| `portfolio_snapshots` | 组合级状态快照 |
| `paper_orders` | Paper Broker 订单状态 |
| `factor_definitions` | 因子定义 |
| `factor_values` | 因子计算结果缓存 |
| `model_registry` | 模型版本 |
| `model_training_runs` | 训练任务记录 |
| `model_drift_checks` | 模型漂移监控 |
| `experiment_variants` | 实验变体 |
| `experiment_results` | 实验结果 |

### 9.2 改造表

| 表名 | 改造 |
|------|------|
| `strategy_config` | 增加 `plugin_name`、`plugin_version`、`plugin_params_json` |
| `runtime_state` | 增加 `portfolio_snapshot_json`，逐步被 `portfolio_snapshots` 取代 |
| `audit_logs` | 增加 `event_id` 关联到 `event_log` |
| `llm_interactions` | 增加 `factor_suggestions`、`failure_analysis` 等字段 |

---

## 10. API  surface 变化

### 10.1 新增端点

```
GET    /api/platform/strategies                 # 列出已注册策略插件
GET    /api/platform/strategies/:name/schema    # 策略参数 schema
POST   /api/platform/strategies/:name/validate  # 验证参数

POST   /api/platform/runner/control             # 控制 PlatformRunner
GET    /api/platform/runner/status              # 平台级运行状态
GET    /api/platform/events                     # 查询事件流
POST   /api/platform/events/replay              # 回放事件

GET    /api/portfolio/config
PUT    /api/portfolio/config
GET    /api/portfolio/status
GET    /api/portfolio/report
POST   /api/portfolio/rebalance                 # 生成再平衡建议

POST   /api/paper/orders
GET    /api/paper/orders
POST   /api/paper/orders/:id/cancel
POST   /api/paper/orders/:id/modify

GET    /api/data/factors
POST   /api/data/factors
GET    /api/data/factors/:name/values
POST   /api/data/quality/check

GET    /api/models
POST   /api/models/train
POST   /api/models/:id/activate
GET    /api/models/:id/drift

POST   /api/orders/algo                         # 提交算法单
GET    /api/orders/algo/:id/children            # 子订单状态

GET    /api/experiments
POST   /api/experiments
POST   /api/experiments/:id/allocate
POST   /api/experiments/:id/rollback
```

### 10.2 兼容现有端点

- `/api/strategy`、`/api/status`、`/api/orders`、`/api/events`、`/api/backtest/*` 继续保留。
- 内部实现逐步迁移到 `PlatformRunner`，但 API 契约保持向后兼容至少 3 个月。
- 新增 feature flag `AUTO_TRADE_PLATFORM_MODE`（默认 `false`），启用后使用新架构。

---

## 11. 前端影响

### 11.1 新增页面

- `/platform/strategies`：策略插件市场/列表
- `/platform/runner`：新运行器控制台
- `/portfolio`：组合监控与报告
- `/research`：数据与因子研究
- `/models`：模型注册与训练
- `/experiments`：策略实验与灰度

### 11.2 改造页面

- `/strategy`：支持选择策略插件，根据 schema 动态渲染参数表单。
- `/backtest`：使用新的统一事件回放引擎。
- `/dashboard`：增加组合级指标卡片。

### 11.3 组件复用

- 复用现有 `PriceChart`、`PnLChart`、`DataState`、`MetricStat`、`CommandPalette` 等组件。
- 新增 `EventTimeline`、`PortfolioSummary`、`FactorChart`、`ModelCard` 等组件。

---

## 12. 测试策略

### 12.1 单元测试

- 每个 SDK 接口、事件类型、执行算法必须有单元测试。
- 使用 `pytest` + `freezegun` + 内存 SQLite。

### 12.2 事件回放测试

- 编写「golden event sequence」测试：给定一组事件，断言最终状态和中间信号。
- 同一组 golden sequence 在 backtest / paper / live 三种模式下运行，结果一致。

### 12.3 Paper Broker 测试

- 用程序化生成的 bar/quote 序列验证 partial fill、滑点、撤改单行为。
- 与真实 broker 的历史成交记录做对比（手动采样）。

### 12.4 Cypress E2E

- 新增 `platform.cy.ts`、`portfolio.cy.ts`、`research.cy.ts`、`models.cy.ts`、`experiments.cy.ts`。
- 由于架构变化大，优先覆盖「 happy path + 关键错误路径」。

### 12.5 性能测试

- 事件流吞吐：每秒处理 1000 个事件不丢序、不OOM。
- 回测吞吐：单标的 1 年分钟线 5 秒内完成。

---

## 13. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 架构重构导致现有功能回归 | 高 | feature flag 切换；旧 `AppRunner` 保留；每轮交付后全量 pytest + Cypress |
| 事件流设计不当导致性能瓶颈 | 高 | 先做单进程内存事件总线；后期再考虑持久化/异步化；设定吞吐基准 |
| Paper Broker 撮合与现实差异大 | 中 | 可配置滑点/延迟模型；提供真实成交校准工具；文档明确假设 |
| 组合级风控复杂度爆炸 | 中 | 先实现敞口/回撤熔断；VaR/相关性作为可选扩展 |
| ML/LLM 训练闭环引入重依赖 | 中 | 模型训练作为可选模块；核心运行时不依赖 ML；LLM 保持可插拔 |
| 前端页面大量新增导致维护困难 | 中 | 复用现有组件；按 phase 分批上线；每个页面有独立 Cypress spec |

---

## 14. 实施阶段建议

由于 10 轮总跨度很大，建议分为 3 个阶段，每个阶段内部按依赖顺序推进：

| 阶段 | 范围 | 目标 | 预估轮次 |
|------|------|------|----------|
| **Phase 1** | P149 + P150 | 完成 SDK + 事件流，现有区间策略作为第一个插件在新架构跑通 | 2 轮 |
| **Phase 2** | P151 + P152 + P156 | 组合交易 + Paper Broker + 组合风控，形成多标的闭环 | 3 轮 |
| **Phase 3** | P153 + P154 + P155 + P157 + P158 | 数据研究、ML、高级执行、绩效归因、版本部署 | 5 轮 |

每阶段交付后应有一次完整回归测试和 Roadmap 更新。

---

## 15. 成功标准

1. P149 + P150 完成后：现有区间策略能在 `PlatformRunner` 中以 backtest / paper / live 三种事件流模式下运行（撮合仿真在 P152 完善），事件流可回放，结果一致。
2. P151 + P152 + P156 完成后：能配置 2+ 标的组合，Paper Broker 能仿真 partial fill 与滑点，组合风控能在回撤时触发暂停。
3. P153–P158 完成后：能注册新策略插件、定义因子、训练轻量模型、运行 A/B 实验、生成组合归因报告。
4. 全过程中：`pytest` 不低于当前基线，`basedpyright` 0 errors，`vue-tsc` 0 errors，`npm run build` 通过。

---

## 16. 待明确问题

1. **feature flag 默认开启时间**：是 P150 后立即默认开启新架构，还是 P158 完成后再切换？
2. **Paper Broker 默认启用范围**：是只用于回测，还是也支持独立的 paper trading 模式？
3. **ML 依赖边界**：是否允许引入 `lightgbm`、`scikit-learn` 作为可选依赖？
4. **数据存储格式**：因子缓存和事件日志长期用 SQLite 还是引入本地 Parquet？
5. **组合交易的资金账户**：是否支持多币种保证金账户的抽象？

---

## 17. 下一步

本 spec 通过评审后，使用 `writing-plans` skill 把 P149–P158 拆分为可执行的实施计划，并从 **Phase 1（P149 + P150）** 开始第一轮开发。
