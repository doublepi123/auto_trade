# P7：策略复盘与 LLM 优化工作台 设计

> **日期：** 2026-05-26
> **代号：** P7（Roadmap 2026-05-25 排序）
> **基线：** `pytest 485 collected`，`basedpyright` 0 errors / 0 warnings（P5+ 交付后状态，commit `323743b`；后续以 CI 实际结果为准）
> **目标分支：** `main`
> **前置阅读：**
> - `docs/Roadmap.md`（P5+ 已交付，P7 当前迭代）
> - `docs/superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md`（P5+ 设计，跨表 union 设计可借鉴）
> - `docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md`（LLM 顾问数据流）

## 1. 背景与动机

经过 P1–P5+ 迭代，系统已沉淀以下数据：

- `llm_interactions`：每次 DeepSeek 调用的 prompt、原始响应、解析结果、`order_action`、`order_id`（broker_order_id）、`applied`。
- `orders` / `trade_events`：成交、撤单、跳过原因（`skip_category`）、`broker_order_id` 关联。
- `runtime_state_snapshots`：引擎状态、价格、PnL、连损计数的时间序列。

但这些数据被分散展示在 Dashboard、TradeHistory、DecisionTimeline 三个页面，没有"按日复盘"的视角。复盘人无法快速回答：

1. 昨天的策略表现是否符合预期？
2. LLM 建议中哪些被采纳、哪些被拒绝、哪些"建议止损但没止住"？
3. 如何把这些信号反哺给 prompt 调优？

P7 在不改写路径、不新增表的前提下，提供单一标的的**按日复盘视图**与**结构化导出**。

## 2. 范围与切除

### 范围

| Task | 主题 | 主要交付 | 依赖 |
|------|------|----------|------|
| **T1** | ReplayService 基础 | `ReplayService.list_days` / `aggregate_day`；跨表 JOIN；Trip 构造 | — |
| **T2** | 标签算法 | 5 个 `ReplayTag` 分类器；阈值常量；测试覆盖 | T1 |
| **T3** | API 端点 | `/api/replay/days`、`/api/replay/{date}`、`/api/replay/{date}/export` | T1, T2 |
| **T4** | 前端复盘页 | `Replay.vue` + `ReplayPriceChart.vue` + 路由 + API client | T3 |
| **T5** | 导出格式 | JSON 细粒度 + CSV 扁平 | T3 |
| **T6** | 测试与文档 | pytest（~25 新增）+ Cypress（1 spec）+ README/CLAUDE 同步 | 全部 |

### 显式 YAGNI 切除

- ❌ **多标的复盘** —— 单标的引擎前提下，本轮仅当前 `StrategyConfig.symbol`；`?symbol=` 仅在 `/days` / `/{date}` 上支持，避免暴露全库 enumerate。
- ❌ **标签可配置阈值** —— 阈值为算法版本一部分，写代码常量；调整须改代码 + 加测试。
- ❌ **复盘数据物化** —— 不引 `replay_sessions` 表；查询时聚合，单标的 SQLite 量级可接受。
- ❌ **反向关联字段** —— 不给 `TradeEvent` / `OrderRecord` 加 `interaction_id` FK；用 `broker_order_id` 在内存 JOIN。
- ❌ **历史 StrategyConfig 快照** —— 标签阈值参考"当前" `min_profit_amount`，导出 metadata 标注；不引 `strategy_history`。
- ❌ **PWA / 离线导出** —— 浏览器原生下载即可。
- ❌ **写路径改造** —— P7 全程只读，不动 `TradeExecutionService` / `AppRunner` / `LLMAdvisorService`。

## 3. 数据流与服务边界

### 3.1 现有表与关联键（不改）

```text
LLMInteraction.order_id (= broker_order_id, string)
    ↓ JOIN by broker_order_id
OrderRecord.broker_order_id ─────┐
    ↑ JOIN by broker_order_id    │ 同 broker_order_id
TradeEvent.broker_order_id ──────┘
```

`RuntimeStateSnapshot.engine_state` 变化序列驱动 trip 划分（`flat / long / short`）。

### 3.2 服务层职责

```python
# backend/app/services/replay_service.py
class ReplayService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        broker: BrokerGateway | None,
        strategy_service: StrategyService,
    ) -> None: ...

    def list_days(self, symbol: str | None, limit: int) -> list[ReplayDaySummary]: ...
    def aggregate_day(self, symbol: str | None, trade_day: date) -> ReplayDayResponse: ...
    def export_day(self, symbol: str | None, trade_day: date, format: str) -> bytes | str: ...
```

**`symbol` 解析**：`symbol or StrategyConfig.symbol`；空则抛 `ValueError`（API 层转 422）。

**`list_days` 规则**（固定，避免前后端/测试歧义）：

1. 候选日期来自三类数据源的并集（按 market-aware trade day 归一）：`orders.created_at`、`llm_interactions.created_at`、`runtime_state_snapshots.created_at`。
2. 仅返回“至少命中一类数据”的交易日；不补齐空白日历日。
3. 按 `trade_day DESC` 排序，应用 `limit`（1–90）。
4. `/api/replay/days` 的“无数据”响应为 `200` + `days=[]`，不返回 404。

**`aggregate_day` 流程**：

1. 取 `market = StrategyConfig.market`，用 `market_calendar.trade_day_for` 反推该日 RTH 起止 UTC 时间窗 `[t_open, t_close]`。
2. 查 K 线：`broker.get_candlesticks(symbol, "Min_1", count=400)`，过滤落在窗口内的点；若空（broker 不可用 / 历史超出保留窗口），改读 `RuntimeStateSnapshot.created_at ∈ [t_open, t_close]` 形成 close-only 序列。`price_source` 标注 `candles | snapshots | none`。
3. 查 orders / trade_events / llm_interactions：所有 `created_at ∈ [t_open, t_close]` 且 `symbol` 命中（trade_events / orders 直接过滤；llm_interactions 用 `symbol == symbol`）。
4. 重算 realized PnL：调 `DailyPnlService(db).calculate(trade_day=trade_day, symbol=symbol, to_trade_day=trade_day_for_market)`，取返回 `DailyPnlResult.trades`（`list[RealizedTrade]`）；建立 `realized_by_broker_id: dict[str, list[float]]`（同 broker_order_id 可有多笔部分成交，pop 顺序按 `filled_at`）。
5. 内存 JOIN：
   - `LLMInteraction.order_id → OrderRecord.broker_order_id`（保留 LLM 调用未下单 / order_id 为 None 的情况）
   - `TradeEvent.broker_order_id → OrderRecord.broker_order_id`
   - `OrderRecord.broker_order_id → realized_by_broker_id`（开仓单 / 未匹配 → `realized_pnl=None`）
6. 划 trip：用 `RuntimeStateSnapshot.engine_state` 序列变化点切分；每段记录 `entry_order_id` / `exit_order_id` / `direction` / `realized_pnl`（取该 trip 平仓单的 `realized_pnl`）。
7. 计算标签（§4）。
8. 聚合 summary：`trip_count`、`realized_pnl = sum(matched RealizedTrade.pnl)`、`llm_call_count`、`llm_applied_count`、`tag_counts`。

**与现有服务的边界**：
- 不调用 `LLMAdvisorService`（避免误触发分析）。
- 不写 `trade_events` / `audit_logs`（只读复盘，不留痕）。
- 复用 `BrokerGateway.get_candlesticks`，可注入 `None`（测试与无凭证场景）。
- 复用 `DailyPnlService.calculate` 重算 realized PnL，**不重写台账逻辑**；`to_trade_day` 注入 market-aware 版本与 runner 一致。
- `StrategyService` 仅用于读 `symbol` / `market` / `min_profit_amount`。

### 3.3 容错策略

| 情况 | 行为 |
|------|------|
| broker 凭证缺失或抛错 | `price_source="snapshots"`，记 warning，不抛 |
| 当日无 snapshots 与 K 线 | `price_source="none"`，`prices=[]`，前端展示"无价格数据" |
| 当日无 orders 与 llm_interactions | summary 全 0，前端展示"当日无交易" |
| `RuntimeStateSnapshot` 缺失 | trip 列表退化为基于 `OrderRecord.side` 配对推断 entry/exit，并在 metadata 标注 `trip_inference_mode="order_inferred"`（否则为 `snapshot_based`） |
| broker `get_candlesticks` 抛 longport 限流 | 走 P5+ retry；retry 耗尽降级 snapshots |

## 4. 标签算法

5 个标签按**优先级**评估，命中即返回：

| 优先级 | Tag | 触发条件 |
|---|---|---|
| 1 | `MISSED_STOP` | LLM 建议 EXIT（`order_action ∈ {SUBMIT, CANCEL_REPLACE}` 且方向为平仓 / 反向开仓）且 `applied=False` 或 `order_status ∈ {SKIPPED, REJECTED, CANCELLED}`；且该 LLM 调用之后 `MISSED_STOP_WINDOW_SECONDS` 内同 trip 出现 `realized_pnl < 0` 的平仓 |
| 2 | `PREMATURE_ENTRY` | LLM 调用对应入场单（`applied=True`，方向为开仓）；所属 trip `realized_pnl < 0` 且 trip 持仓时长 `< PREMATURE_WINDOW_SECONDS` |
| 3 | `FREQUENT_REPRICE` | 所属 trip 内累计 `order_action == "CANCEL_REPLACE"` 次数 + `TradeEvent.payload.skip_category == "REPRICING"` 次数 `≥ REPRICE_THRESHOLD` |
| 4 | `LOW_PROFIT` | LLM 对应平仓单 `realized_pnl > 0` 且 `realized_pnl < min_profit_amount × LOW_PROFIT_RATIO` |
| 5 | `NORMAL` | 兜底 |

**`order_action` 判定口径（固定到现有写入语义）**：

- 仅将 `order_action in {"SUBMIT", "CANCEL_REPLACE"}` 视为“产生了可执行交易意图”的决策。
- `order_action == "NONE"`（或解析失败降级值）不参与规则 1/2/4，只能命中规则 3（若同 trip 有重挂证据）或 `NORMAL`。
- “EXIT 倾向”由“关联订单方向与当时 trip 持仓方向相反”判断，而不是仅凭 action 字面值。

**阈值常量**（`ReplayService` 类常量，不可配置）：

```python
class _ReplayTagParams:
    MISSED_STOP_WINDOW_SECONDS = 14400  # 4 小时
    PREMATURE_WINDOW_SECONDS   = 1800   # 30 分钟
    REPRICE_THRESHOLD          = 3
    LOW_PROFIT_RATIO           = 1.2
```

**locate_trip**：LLM 时间 ∈ `[trip.open_at, trip.close_at or +∞)`；不命中任一 trip（如开盘前 preview）→ `trip=None`，仅规则 1 与 `NORMAL` 适用。

**realized_pnl 来源**：`OrderRecord` 上**不直接存** `realized_pnl`（`raw_response` 是 broker 原始响应）。ReplayService 在 `aggregate_day` 阶段调用 `DailyPnlService.calculate(trade_day=trade_day, symbol=symbol, to_trade_day=trade_day_for_market)`，使用 `result.trades`（按 `broker_order_id + filled_at` 标识）；按 `broker_order_id` 反查回 `ReplayOrderItem.realized_pnl`。未在台账匹配到的开仓单 / 部分成交 → `realized_pnl=None`，相关规则跳过。

**`min_profit_amount` 来源**：当前 `StrategyConfig.min_profit_amount`；导出 metadata 携带，复盘人感知"这是用现行阈值评估历史"。

## 5. API 与 Schema

### 5.1 端点

| Method | Path | Query | 返回 |
|---|---|---|---|
| `GET` | `/api/replay/days` | `limit=30`（1–90） | `ReplayDaysResponse` |
| `GET` | `/api/replay/{trade_day}` | `symbol`（可选） | `ReplayDayResponse` |
| `GET` | `/api/replay/{trade_day}/export` | `format=json\|csv`、`symbol` | 流式下载 |

`trade_day` 用 `YYYY-MM-DD`，FastAPI 自动转 `date`。

**状态码约定**（固定）：
- `422`：参数非法（`trade_day` 无法解析、`limit` 越界、symbol 无法解析且配置缺省）。
- `200`：参数合法，即使该日无数据也返回空结构（如 `prices=[]`、`orders=[]`、`trips=[]`）。
- 不为“合法但空数据”使用 `404`。

**鉴权**：全部 GET，不挂 `require_api_key`（与 `/api/events`、`/api/status` 一致）。

**导出响应头**：
- JSON：`application/json`，`Content-Disposition: attachment; filename=replay-{symbol}-{date}.json`
- CSV：`text/csv; charset=utf-8`，同上但 `.csv`

### 5.2 Schemas（追加 `backend/app/schemas.py`）

```python
class ReplayTag(str, Enum):
    PREMATURE_ENTRY  = "PREMATURE_ENTRY"
    MISSED_STOP      = "MISSED_STOP"
    FREQUENT_REPRICE = "FREQUENT_REPRICE"
    LOW_PROFIT       = "LOW_PROFIT"
    NORMAL           = "NORMAL"

class ReplayDaySummary(BaseModel):
    trade_day: date
    symbol: str
    market: str
    trip_count: int
    realized_pnl: float
    llm_call_count: int
    llm_applied_count: int
    tag_counts: dict[ReplayTag, int]

class ReplayDaysResponse(BaseModel):
    symbol: str
    days: list[ReplayDaySummary]

class ReplayPricePoint(BaseModel):
    ts: datetime
    open: float; high: float; low: float; close: float
    volume: float | None = None

class ReplayOrderItem(BaseModel):
    id: int
    broker_order_id: str
    side: str
    status: str
    quantity: float
    price: float
    executed_quantity: float | None
    executed_price: float | None
    created_at: datetime
    filled_at: datetime | None
    realized_pnl: float | None

class ReplayLLMItem(BaseModel):
    id: int
    interaction_type: str
    created_at: datetime
    applied: bool
    order_action: str
    order_status: str | None
    suggested_buy_low: float | None
    suggested_sell_high: float | None
    confidence: float | None
    summary: str          # parsed_response 截断 240
    prompt_excerpt: str   # prompt 截断 480
    linked_order_id: int | None
    tag: ReplayTag

class ReplayTradeEventItem(BaseModel):
    id: int
    event_type: str
    side: str
    status: str
    message: str
    broker_order_id: str
    skip_category: str | None
    linked_order_id: int | None
    created_at: datetime

class ReplayTrip(BaseModel):
    open_at: datetime
    close_at: datetime | None
    direction: Literal["long", "short"]
    entry_order_id: int | None
    exit_order_id: int | None
    quantity: float
    entry_price: float
    exit_price: float | None
    realized_pnl: float | None
    llm_interaction_ids: list[int]

class ReplayDayResponse(BaseModel):
    summary: ReplayDaySummary
    price_source: Literal["candles", "snapshots", "none"]
    prices: list[ReplayPricePoint]
    orders: list[ReplayOrderItem]
    trade_events: list[ReplayTradeEventItem]
    llm_interactions: list[ReplayLLMItem]
    trips: list[ReplayTrip]
    rth_window: list[datetime] | None  # len == 2, [open_utc, close_utc]
    metadata: dict[str, Any]  # {"min_profit_amount": ..., "tag_thresholds": {...}, "trip_inference_mode": "..."}
```

### 5.3 导出格式

**JSON（细粒度）**：

```json
{
  "metadata": {
    "symbol": "AAPL.US",
    "trade_day": "2026-05-20",
    "min_profit_amount": 50.0,
    "tag_thresholds": {
      "MISSED_STOP_WINDOW_SECONDS": 14400,
      "PREMATURE_WINDOW_SECONDS": 1800,
      "REPRICE_THRESHOLD": 3,
      "LOW_PROFIT_RATIO": 1.2
    }
  },
  "summary": { ... },
  "trips": [ ... ],
  "llm_interactions": [
    {
      "id": 123,
      "created_at": "...",
      "prompt": "...完整 prompt...",
      "raw_response": "...完整响应...",
      "parsed_response": { ... },
      "applied": true,
      "linked_order": { ...完整 OrderRecord 字段... },
      "tag": "PREMATURE_ENTRY"
    }
  ]
}
```

**CSV（扁平）**：

```csv
llm_id,created_at,interaction_type,applied,order_action,suggested_buy_low,suggested_sell_high,confidence,prompt_summary,linked_order_id,linked_side,linked_status,linked_quantity,linked_price,linked_executed_price,linked_realized_pnl,tag
123,2026-05-20T13:45:00Z,decide,true,SUBMIT,148.5,158.0,0.82,"建议买入...",456,BUY,FILLED,100,148.5,148.55,-23.0,PREMATURE_ENTRY
```

- CSV 字段固定，方便 Excel 透视；prompt 仅 summary（截断 200），如需全文用 JSON。
- LLM 未关联订单时 `linked_*` 留空。

## 6. 前端

### 6.1 路由与导航

- `frontend/src/router/index.ts`：在 `/backtest` 之后增 `/replay` 路由（lazy）。
- `frontend/src/App.vue`：导航栏在 "Backtest" 之后插入 "Replay"。
- URL hash 同步：`#/replay?d=2026-05-20`，前端打开时自动选中（无参数 → 选 days 列表第一项）。

### 6.2 `Replay.vue` 结构

```text
<Header>
  <DatePicker bind days[]/>                  <!-- el-select 显示 days[i].trade_day -->
  <ExportButtonGroup>JSON | CSV</ExportButtonGroup>
</Header>

<el-row>
  <el-col span=6>
    <SummaryCard>                            <!-- 桌面端左侧；移动端折叠为顶部 chip -->
      Trips / Net PnL / LLM Calls / Apply%
      TagCounts (5 个 chip)
    </SummaryCard>
  </el-col>
  <el-col span=18>
    <ReplayPriceChart prices trips llm_marks/>
  </el-col>
</el-row>

<el-row>                                     <!-- 时间线区 -->
  <el-col span=12>
    <LLMTimeline llm_interactions/>          <!-- 每张卡片：tag 色条 + summary + 关联订单 link -->
  </el-col>
  <el-col span=12>
    <OrderEventTimeline orders trade_events/>
  </el-col>
</el-row>
```

### 6.3 `ReplayPriceChart.vue`

- 复用 `PriceChart.vue` 的 SVG 折线/烛线技术栈，**不引新图表库**。
- 输入：`prices`、`trips`、`llm_marks: { ts, tag, summary }[]`。
- Trip 阴影：`long → 蓝 50% 不透明度`、`short → 橙`；横向覆盖 `[trip.open_at, trip.close_at]`。
- LLM 标记：竖线 + 顶部圆点，颜色按 tag；hover tooltip 显示 `summary` 前 120 字。

### 6.4 标签视觉

`frontend/src/utils/replay_tags.ts`（新文件，与 `labels.ts` 解耦）：

```ts
export const REPLAY_TAG_COLOR: Record<ReplayTag, string> = {
  MISSED_STOP:      '#dc2626', // red-600
  PREMATURE_ENTRY:  '#ea580c', // orange-600
  FREQUENT_REPRICE: '#ca8a04', // yellow-600
  LOW_PROFIT:       '#facc15', // yellow-400
  NORMAL:           '#9ca3af', // gray-400
}
export const REPLAY_TAG_LABEL: Record<ReplayTag, string> = {
  MISSED_STOP:      '错过止损',
  PREMATURE_ENTRY:  '过早进场',
  FREQUENT_REPRICE: '频繁重挂',
  LOW_PROFIT:       '收益不足',
  NORMAL:           '正常交易',
}
```

### 6.5 移动端（< 768px）

- 顶部 chip 行替代摘要卡。
- 价格图与时间线纵向堆叠。
- LLM / 订单事件时间线切 tab。
- 导出按钮收入溢出菜单。

### 6.6 API Client

`frontend/src/api/replay.ts`：

```ts
export interface ReplayDaysResponse { ... }
export interface ReplayDayResponse { ... }

export async function listReplayDays(symbol?: string, limit = 30): Promise<ReplayDaysResponse>
export async function fetchReplayDay(tradeDay: string, symbol?: string): Promise<ReplayDayResponse>
export function buildReplayExportUrl(tradeDay: string, format: 'json' | 'csv', symbol?: string): string
```

导出直接 `window.open(buildReplayExportUrl(...))`，不在 axios 链路中转流。

### 6.7 Types

`frontend/src/types/index.ts` 追加 `ReplayTag`、`ReplayDaySummary`、`ReplayDayResponse`、`ReplayLLMItem`、`ReplayOrderItem`、`ReplayTradeEventItem`、`ReplayTrip` 等。

## 7. 测试与验证

### 7.1 Backend pytest（~25 新增）

| 文件 | 覆盖 |
|------|------|
| `test_replay_service_aggregate.py` | `aggregate_day` 完整路径：K 线优先 → 回退 snapshots → 全无数据。Trip 划分正确。 |
| `test_replay_service_trips.py` | trip 划分边界：跨日 trip、未平仓 trip、entry_order=None。 |
| `test_replay_tagger.py` | 5 个 tag 各 ≥1 正例 + 1 反例 + 边界相等用例；优先级正确。 |
| `test_replay_api.py` | 三端点 200 / 422（trade_day 非法、limit 越界、symbol 缺失）；“合法但空数据”返回 200 空结构；CSV / JSON 导出 Content-Type & filename。 |
| `test_replay_export.py` | CSV 字段完整、JSON metadata 含 tag_thresholds；LLM 未关联订单时 `linked_*=NaN/null`。 |

### 7.2 Cypress（1 spec）

`frontend/cypress/e2e/replay.cy.ts`：
- 进入页加载 days；选中第一项；摘要卡渲染；LLM 卡片与订单时间线渲染。
- 切日触发新请求并替换内容。
- 导出按钮（JSON / CSV）触发 `cy.intercept` 命中带 `format=json|csv` 的 URL。
- 标签色条与 `REPLAY_TAG_LABEL` 文案匹配。

### 7.3 Lint

- `basedpyright` 0 / 0。
- `npm run type-check` 通过；`npm run build` 通过。

### 7.4 手工验证

- [ ] 旧 DB（含历史 LLM/订单数据）：`/api/replay/days` 返回最近 30 日，未交易日不入列表。
- [ ] broker 凭证空：`price_source="snapshots"`；前端展示水印；不报错。
- [ ] 单标的当日无 trip：summary 全 0；前端展示"当日无交易"。
- [ ] 合法但空数据日期：`GET /api/replay/{trade_day}` 返回 200 + 空数组结构（非 404）。
- [ ] CSV 在 Excel 打开列对齐；JSON 含 prompt 全文。
- [ ] 移动视口（iPhone 14 Pro）：时间线切 tab 正常；导出按钮可触发。

## 8. 风险与回滚

| 风险 | 缓解 |
|------|------|
| broker 历史 K 线超出保留窗口 | 自动回退 snapshots；前端水印告知 |
| LLM 调用未携带 `order_id` | tag 计算允许 `linked_order_id=None`，规则 2/4 跳过 |
| `realized_pnl` 解析失败 | `realized_pnl=None`；规则 1/2/4 跳过；NORMAL 兜底 |
| 单日聚合慢（大量 snapshots） | 内存 JOIN 单标的量级可控；后续大数据可加 `RuntimeStateSnapshot` 索引 |
| 标签阈值争议 | 阈值是代码常量，PR review 时辩论；用户可在导出 metadata 看到当时阈值 |
| 切日时浏览器历史污染 | URL hash 同步 `?d=`，可前后导航；前端拦截重复请求 |

**回滚**：
- 三端点全部 GET，不写 DB、不发请求外部，**直接删除路由 + 服务即回滚**。
- 前端路由删除即可，不影响其他页面。
- 无数据库迁移，无 `_ensure_*` 改动。

## 9. 交付顺序

1. **T1** — `ReplayService.aggregate_day` 主路径（K 线 + snapshots fallback + trip 划分）
2. **T2** — 标签分类器 + 单元测试（5 个 tag）
3. **T3** — API 端点 + schemas
4. **T5** — JSON / CSV 导出
5. **T4** — 前端页面 + 图表 + cypress
6. **T6** — README / CLAUDE / Roadmap 文档同步 + 全量 lint

## 10. 不在本迭代

- P6 移动端整体改造（仅本页移动适配）
- P8 Watchlist / 多标的引擎
- 节假日历（依然 `RTH_ONLY` 仅周末 + 常规时段）
- 审计导出（P5+ YAGNI 范围）
- 标签阈值的环境变量化或 UI 配置
- 历史 `StrategyConfig` 快照表
