# P26: 多标的自动交易扩展评估与边界切分

> **日期：** 2026-06-04
> **状态：** 已交付
> **目标：** 在不改变实盘交易行为的前提下，明确单标的系统扩展到多标的的状态边界、分阶段顺序与禁止事项。

---

## 背景

当前系统仍是单交易标的架构：`StrategyConfig` 单行配置驱动 `AppRunner.engine`，`StrategyEngine` 内部只持有一个 `params.symbol`、一个 `EngineState`、一个 `last_price` 和一个 cooldown 时间。`AppRunner` 只订阅 `self.engine.params.symbol` 的 quote，并把 quote 推入单个 engine。`RuntimeState` / `RuntimeStateSnapshot` 也没有 `symbol` 字段。

同时，部分链路已经天然支持多标的：`OrderRecord.symbol`、`TradeEvent.symbol`、`TrackedEntry.symbol` 主键、`TradeExecutionService._entry_positions` 字典、`DailyPnlService` 的 symbol 过滤参数、`LLMInteraction.symbol`。这些能力应保留并复用，不应重写。

---

## 结论

推荐三阶段切分：

1. **P27：只读多标的监控 MVP**。复用 watchlist，新增聚合快照 API 和 Dashboard 多标的卡片；不新增自动下单，不迁移 runtime state，不改 runner 主循环。
2. **P28：多标的交易状态隔离**。引入每标的 engine/runtime/pending order 隔离；保留 `RiskController` 为全局组合级风控。
3. **P29：生产运维与可观测性补强**。在多标的交易前后补充诊断包、后台线程健康、通知健康检查。

这避免一次性重构 `AppRunner`、DB schema、前端状态和交易执行，降低实盘风险。

---

## 方案比较

### 方案 A：直接多标的交易改造

- **做法**：一次性把 `AppRunner.engine` 改成 `dict[str, StrategyEngine]`，把 runtime state 改成每标的，允许多个 watchlist item 同时自动交易。
- **优点**：最短路径到多标的下单。
- **缺点**：同时触碰 quote 订阅、状态持久化、pending order、风控、Dashboard 和重启对账；回归面过大，不适合实盘系统。

### 方案 B：只读监控先行，再交易隔离

- **做法**：P27 只新增 read-only multi-symbol snapshots；P28 再做交易状态隔离。
- **优点**：先验证多标的行情/API/UI 数据形态，不改变现有单标的交易行为；可用 Cypress 和 API 测试覆盖。
- **缺点**：需要两轮才能到多标的下单。

### 方案 C：多策略配置先行

- **做法**：先把 `StrategyConfig` 改成多行，再逐步接 runner。
- **优点**：配置模型更接近最终态。
- **缺点**：若 runner 仍单标的，多行配置会制造“看起来可交易但实际不会交易”的误导；不如先做只读快照安全。

**选择：方案 B。** 它最符合当前系统的风险边界：P27 可独立交付价值，P28 再承担交易状态隔离。

---

## 状态边界分类

| 组件 / 字段 | 当前假设 | 分类 | 阶段 | 决策 |
|-------------|----------|------|------|------|
| `AppRunner.engine` | 单个 `StrategyEngine` | per-symbol | P28 | 改为 `dict[str, StrategyEngine]` 或封装 `SymbolRuntime`。 |
| `AppRunner.risk` | 单个 `RiskController` | global | P28 | 保持组合级全局风控；daily loss / kill switch / pause 统一生效。 |
| `AppRunner._trade_svc` | 单个服务实例，内部 pending 单槽 | mixed | P28 | 服务可保留单实例；`_pending_order` 必须按 symbol 隔离或限制全局串行。 |
| `AppRunner._quotes_subscribed` | 单 boolean | per-symbol | P28 | 改为订阅集合或 subscription registry。 |
| `AppRunner._recent_quotes` | 单列表 | per-symbol | P28 | 改为 `dict[str, list[quote]]`，否则 LLM/图表上下文串线。 |
| `AppRunner._last_llm_action_at` | `dict[(symbol, side)]` | 已可多标的 | P28 | 保留。 |
| `StrategyEngine.params.symbol` | 单 symbol | per-symbol | P28 | engine 本身可继续单标的；runner 管理多个实例。 |
| `RuntimeState` | 单行，无 symbol | per-symbol | P28 | 加 `symbol` 字段或新表，按 symbol load/persist。 |
| `RuntimeStateSnapshot` | 无 symbol | per-symbol | P28 | 加 `symbol` 字段，历史曲线按 symbol 查询。 |
| `StrategyConfig` | 单行配置 | single-primary for MVP | P27 | P27 保持单交易目标；P28 再设计多策略配置。 |
| `WatchlistItem.is_active` | 单 active trading target | single-primary for MVP | P27 | P27 仅用于标识当前交易目标；不改多 active。 |
| `TrackedEntry.symbol` | symbol 主键 | 已可多标的 | P28 | 保留。 |
| `OrderRecord.symbol` | 订单已有 symbol | 已可多标的 | P28 | 保留。 |
| `TradeEvent.symbol` | 事件已有 symbol | 已可多标的 | P28 | 保留。 |
| `DailyPnlService` symbol 过滤参数 | 支持按 symbol 过滤 | 已可多标的 | P28 | 保留；全局风险仍读取组合级结果。 |
| `StatusResponse.last_price` | 标量 | single-primary for MVP | P27 | P27 不改现有 `/api/status`，新增独立 snapshots。 |
| `useStatusStream` | 单 `StatusData` WS | single-primary for MVP | P27 | P27 不改 WS，避免影响交易 cockpit。 |
| `Dashboard.vue` | 主 cockpit 单 symbol | single-primary for MVP | P27 | 保留主 cockpit，新增只读多标的区块。 |
| `Watchlist.vue` | 已轮询多标的 quotes | 已可多标的 | P27 | 作为 P27 模式参考。 |

---

## P27 只读监控 MVP 设计

### 后端

新增 `GET /api/watchlist/snapshots`。

返回类型建议：

```python
class WatchlistSnapshot(BaseModel):
    symbol: str
    market: str
    alias: str = ""
    is_trading_target: bool = False
    last_price: float
    bid: float
    ask: float
    timestamp: str
```

实现原则：

- 使用 `WatchlistService.list_items()` 获取 watchlist。
- 使用现有 `BrokerGateway.get_quotes(symbols)` 批量获取行情。
- 使用当前 `StrategyConfig.symbol` 标记 `is_trading_target`。
- 返回 ephemeral 数据，不写 DB。
- broker 失败时与 `/api/watchlist/quotes` 一致返回 503。

### 前端

新增类型与 API：

```ts
export interface WatchlistSnapshot {
  symbol: string
  market: 'US' | 'HK'
  alias: string
  is_trading_target: boolean
  last_price: number
  bid: number
  ask: number
  timestamp: string
}
```

```ts
export async function getWatchlistSnapshots(): Promise<WatchlistSnapshot[]> {
  const resp = await api.get('/api/watchlist/snapshots')
  return resp.data
}
```

新增 composable：`frontend/src/composables/useMultiSymbolSnapshots.ts`。

- 15 秒轮询，与 `Watchlist.vue` 行情轮询保持一致。
- 暴露 `snapshots`, `loading`, `error`, `refresh`, `start`, `stop`。
- 不接 WebSocket。

Dashboard 增加“多标的观察”区块：

- 放在现有 cockpit 下方。
- 表格列：标的、别名、市场、最新价、买一、卖一、更新时间、当前交易目标。
- 空列表显示“暂无观察标的”。
- 不显示下单按钮，不允许切换交易目标。

### 测试

后端：

- `GET /api/watchlist/snapshots` 空 watchlist 返回 `[]`。
- 有 watchlist + broker quotes 时返回 alias/market/quote 并正确标记 current strategy symbol。
- broker 失败返回 503。

前端 Cypress：

- Dashboard stub `/api/watchlist/snapshots` 后展示多标的表格。
- 空列表时展示 empty 文案。
- 请求失败时展示错误提示或保留错误状态。

---

## P28 多标的交易设计边界

P28 才允许交易行为变化。核心结构建议：

```python
@dataclass
class SymbolRuntime:
    symbol: str
    market: str
    engine: StrategyEngine
    trading_session_mode: str
    recent_quotes: list[dict[str, Any]]
```

`AppRunner` 持有：

```python
self._symbol_runtimes: dict[str, SymbolRuntime]
```

保留：

- `self.risk` 作为全局风控。
- `self._trade_svc` 单实例，但 pending order 必须改成 `dict[str, _PendingOrder]` 或明确全局串行下单。
- `TradeExecutionService._entry_positions` 字典。

禁止：

- 不允许多个 symbol 共享同一个 `StrategyEngine.state`。
- 不允许 `RuntimeState` 继续单行承载多标的状态。
- 不允许 Dashboard 把多标的状态混入现有 `StatusData` 标量字段。
- 不允许在没有重启对账测试前启用多标的自动下单。

---

## 不做范围

P26/P27 不做：

- 多标的自动下单。
- 多行 `StrategyConfig` 迁移。
- `RuntimeState` schema 迁移。
- WebSocket 多标的状态推送。
- LLM 对多个 symbol 并发下单。
- 多账户、多券商、多币种资金分配。

---

## 验收

P26 本轮验收：

- 本 spec 存在并明确 per-symbol/global/single-primary 边界。
- P27 implementation plan 存在，且只读 MVP 不改变交易行为。
- Roadmap 更新 P26 完成，并把 P27/P28/P29 顺序固化。

P27 验收：

- 新增 `/api/watchlist/snapshots`。
- Dashboard 展示多标的只读快照。
- 当前单标的 `/api/status`、WebSocket、runner 自动交易路径不变。
- 后端单测和前端 Cypress 覆盖空列表、成功、broker 失败。
