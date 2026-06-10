# 全量代码审查报告（2026-06-10）

> 审查范围：整个代码库（backend + frontend，约 2.5 万行），基于 main @ `3b8487e`。
> 方法：7 个独立角度（3 正确性 + 3 清理 + 1 设计层次）并行扫描产出 36 个候选，去重后逐条读源码核实。以下所有条目均已对照实际代码确认，附文件:行号。
> 状态标记：⬜ 未修复 / ✅ 已修复（修复时更新此列）。

---

## 总体结论

整体架构清晰（分层、快照恢复、异步对账、审计都有章法），但**多 symbol 支持是后来叠加在单引擎设计上的，留下了一批真实的资金安全 bug**；另有几个跨线程竞态和一个会重复下单的重试包装。

**修复优先级建议：**

1. **第一批（实盘资金级风险）**：#1（重试重复下单）、#2/#3/#5（pending 生命周期三个洞——同根问题，需要带锁的单一 owner + per-symbol restore 路由）、#4（超时不撤单）。
2. **第二批（风控失效）**：#6/#7（风控计数被清零的两条路径）、#8、#10。
3. **第三批（功能 bug）**：#11（一行修复：路由顺序）、#9、#12、#13、#14。
4. **第四批（前端契约 + 数据正确性）**：#15–#20。
5. **持续重构**：设计/维护性主题，可随相关迭代逐步收敛。

---

## 🔴 严重：实盘资金风险

### ⬜ 1. 下单接口包了重试 → 可能重复提交真实订单

- **位置**：`backend/app/core/broker.py:578`
- **问题**：`submit_limit_order` 走 `_call_with_retry`，可重试标记包含 `"timeout"`/`"connection"`（`broker.py:32-45`）。券商**已受理**但 HTTP 响应超时的提交会被原样重发，产生两张活的限价单——只有第二张被记录和跟踪，第一张成为无人知晓的"幽灵单"。按 90% 保证金购买力定的量，重复成交可直接打穿保证金。
- **修复方向**：订单提交不可盲目重试。需要幂等键（客户端订单 ID），或重试前先按提交参数查询今日订单确认上一次是否已受理。

### ⬜ 2. 副 symbol 订单失败会把快照恢复进主引擎

- **位置**：`backend/app/runner.py:609`、`runner.py:1289`、`runner.py:764`
- **问题**：LLM 在 watchlist 副 symbol 下单时，快照取自 `target_engine`（`runner.py:789`），存进 `_PendingOrder`。但周期 reconcile（`_run_loop`、`_on_quote`）和 `cancel_order_by_id` 对**所有** pending 一律传 `self.engine.restore`（主引擎）。副 symbol 订单被拒/超时 → 副 symbol 的 FLAT 快照写进主引擎（主引擎实际持仓却自认 FLAT，下次触价重复满仓买入），而副引擎永远不被恢复。
- **根因**：`_PendingOrder` 不携带所属引擎。
- **修复方向**：`_PendingOrder` 自带 restore 目标（或 reconcile 按 `pending.symbol` 经 `_runtime_for_symbol` 解析回调）。

### ⬜ 3. 订单同步会"吞掉"刚成交的 pending，成交永不结算

- **位置**：`backend/app/runner.py:1093`（`sync_today_orders_from_broker`）+ `trade_execution_service.py:134-163`（`load_pending_orders`）
- **问题**：同步先 `_upsert_broker_order`（可能把 pending 行改成 FILLED），紧接着 `_load_pending_orders` 只加载 `SUBMITTED/PARTIAL_FILLED` 行并**清空重建**内存 pending 表。在 1s 对账轮询看到成交之前，15s 的订单同步先跑到，这笔成交就被静默丢弃：`_finalize_pending_fill` 永不执行——买入不写 `tracked_entries`（违反文档声明的不变量）、卖出不 `record_trade`、取消不恢复引擎快照。
- **修复方向**：`load_pending_orders` 重建时，对"内存中存在但 DB 状态已转终态"的条目先走 finalize 流程，而非直接丢弃；或同步路径检测状态翻转时显式触发对账。

### ⬜ 4. pending 超时后从不撤单，限价单仍挂在券商

- **位置**：`backend/app/services/trade_execution_service.py:894-949`（`_handle_pending_order_timeout`）
- **问题**：超时处理里，状态查询失败（恰恰是引发超时的那个券商故障）或订单仍 live 时，直接 pause + 清 pending + 恢复快照，**没有任何 `cancel_order` 调用**。那张限价单还活着，之后成交——引擎已回 FLAT、无 tracked entry、无人对账。
- **修复方向**：超时路径必须尝试撤单；撤单失败（含"已成交"错误）时保留 pending 并继续对账，而不是清掉。

### ⬜ 5. 双线程 reconcile 竞态：同一笔成交结算两次

- **位置**：`backend/app/services/trade_execution_service.py:202-225`（`reconcile`）、`:870-871`；调用方 `runner.py:606`（quote 线程）与 `runner.py:1286`（run loop 线程）
- **问题**：成员检查（214-218 行）通过后即释放锁去查券商，没有 in-flight 守卫。两个线程同时看到 FILLED → `_finalize_pending_fill` 跑两遍：`record_trade` 双计（一笔亏损连亏计数 +2，提前触发熔断）、tracked entry 双消耗（下次平仓回退到 broker avg_price）；且 871 行会把已被对方清掉的 pending 重新插回。
- **修复方向**：per-order reconcile in-flight 标记（在锁内置位/复位），或 reconcile 全程单线程化（只让 run loop 做）。

### ⬜ 6. ledger 回放会清空真实亏损，风控护栏失效

- **位置**：`backend/app/runner.py:1105-1131`（`_sync_risk_from_order_ledger`）+ `services/daily_pnl_service.py:122`
- **问题**：每 15s 用本地订单 ledger 重放并 **REPLACE** `risk.daily_pnl`/连亏计数。开仓成交不在 ledger 时（停机期间成交、或在券商 App 手动开仓——同步只拉**当日**订单），平仓 fill 的 `matched_quantity=0` 被跳过 → `record_trade` 刚记下的真实亏损 15 秒内被归零。日亏损上限和连亏上限就此失效。
- **修复方向**：回放发现"无法配对的平仓"时不能简单丢弃——可回退用 tracked_entries / broker avg_price 估算 PnL，或至少不 REPLACE 比当前更"乐观"的值并告警。

### ⬜ 7. GET /api/status 用 UTC 日重算 PnL 并写库

- **位置**：`backend/app/api/strategy.py:114-125`
- **问题**：端点调 `DailyPnlService.calculate()` 不传 `trade_day`/`to_trade_day`（默认 UTC 日历日），而 runner 用交易所本地日（`trade_day_for`）。美股 ET 晚间跨过 00:00 UTC 后，前端每次轮询都把 `daily_pnl=0、明天的日期` 持久化进 `runtime_state`，覆盖 runner 写入的真实亏损。该窗口内重启 → 风控从零恢复；前端跨 UTC 午夜后显示 PnL=0 也是它。
- **修复方向**：端点与 runner 用同一交易日换算（传 `trade_day_for(config.market)`）；并考虑该端点只读、不回写 runtime_state。

### ⬜ 8. 仓位同步竞态把引擎错误打回 FLAT

- **位置**：`backend/app/runner.py:1366-1393`（`_sync_engine_state_with_positions`）
- **问题**：`get_positions()` 在锁外执行；锁内复检只看**当下**的 `_trigger_in_flight`/pending。若在 get_positions 网络往返期间一笔触发单**立即 FILLED**（无 pending 留存），旧仓位快照会把刚转 LONG 的引擎强制 sync 回 FLAT → 下次触价重复买入。
- **修复方向**：记录"最近一次成交时间戳"，仓位快照早于该时间则放弃本轮 sync；或 get_positions 后复检引擎状态版本号。

---

## 🟠 高：功能性 bug

### ⬜ 9. `confidence_score=None` 直接 TypeError，并中止整轮 cron

- **位置**：`backend/app/services/interval_application_service.py:196`（`:30`/`:93` 的 `.get(…, 0.0)`）；调用方 `main.py:291`、`api/llm_advisor.py:248`
- **问题**：调用方构造 dict 时显式放入 `result.get("confidence_score")`——key 存在值为 `None`，`.get(…, 0.0)` 默认值不生效 → `None < float` TypeError。LLM 少返回一个字段 → analyze 端点 500；cron 里异常触发 `break`（`main.py:358`），**剩余所有 symbol 本轮全部跳过**。
- **修复方向**：守护入口做 `confidence = suggestion.get("confidence_score") or 0.0` 类型规整；cron 的 per-symbol 异常应 `continue` 而非 `break`。

### ⬜ 10. 取消时的部分成交被完全忽略

- **位置**：`backend/app/services/trade_execution_service.py:241-266`（`cancel_pending_order_for_symbol`）
- **问题**：不看 `executed_quantity`（对账路径 879-886 行是处理的）：LLM cancel-replace 前已部分成交的份额没有入场记录、没有 PnL、快照按"什么都没发生"恢复。tracked 成本基础从此与真实持仓脱节。
- **修复方向**：取消结果带 `executed_quantity > 0` 时走与 `_reconcile_pending_order` 失败分支相同的部分成交 finalize 逻辑。

### ⬜ 11. 路由顺序错误，LLM 评估面板永远 422

- **位置**：`backend/app/api/strategy_experiments.py:125`（`GET /llm-evaluations`）vs `:48`（`GET /{experiment_id}`，int 参数）
- **问题**：Starlette 按注册序匹配且不回退——`/llm-evaluations` 永远被 `/{experiment_id}` 截获返回 422。Experiments.vue 的 LLM 推荐评分面板从上线起就不可能加载出数据。
- **修复方向**：一行修复——把 `/llm-evaluations` 路由挪到 `/{experiment_id}` 之前注册。

### ⬜ 12. 推送行情 bid/ask 恒为 0，日志被刷爆 + 诊断永久失真

- **位置**：`backend/app/core/broker.py:527`、`:548`；`runner.py:942-957`、`:998`
- **问题**：longport 的 `PushQuote` 事件没有 `bid`/`ask` 属性，`getattr(_event, "bid", 0)` 恒得 0。`_evaluate_quote_quality` 要求 `bid>0 and ask>0`，于是 RTH 内**每个推送 tick 都打一条 warning**，诊断永远报 `price_positive=False`，真正的坏行情被噪音淹没。
- **修复方向**：要么订阅 Depth 取真实 bid/ask，要么质量检查对推送行情只校验 `last_price`，bid/ask 缺失时跳过 spread 判断。

### ⬜ 13. LLM cron 在事件循环上做阻塞网络/DB I/O

- **位置**：`backend/app/main.py:241-247`；`api/llm_advisor.py:42-110`（`_position_context`/`_account_context`）
- **问题**：`get_positions`、`get_cash`、2× `estimate_margin_max_quantity` 及同步 SQLAlchemy 提交直接跑在 asyncio 循环上（只有 `advisor.analyze` 和下单被 to_thread）。券商抖动叠加重试退避（base 1s）时，所有 `/api/*` 和 WS 心跳冻结数秒，客户端断连。
- **修复方向**：把 per-symbol 上下文收集 + DB 写整体包进 `asyncio.to_thread`。

### ⬜ 14. 波动触发在分析前就被消耗

- **位置**：`backend/app/main.py:249-253`
- **问题**：`_last_llm_trigger_price(_by_symbol)` 在 `advisor.analyze` **之前**更新。DeepSeek 超时/失败后，参考价已挪到当前价，波动 gate 不再重触发——对急涨急跌的应激分析丢失，只能等下一个时间 gate。
- **修复方向**：仅在 `result.get("success")` 后更新触发参考价。

---

## 🟡 中：前端契约与数据正确性

### ⬜ 15. 通知中心读取不存在的字段，正文永远是 `{}`

- **位置**：`frontend/src/composables/useNotificationStream.ts:135`、`:198`
- **问题**：读 `item.detail`/`item.action`，但后端 `TimelineEventResponse`（`schemas.py`）只有 `payload`/`message`/`event_type`。每条通知渲染成 `ORDER_FILLED: {}`。
- **修复方向**：改读 `item.message`/`item.payload`，标题用 `event_type`。

### ⬜ 16. 通知去重用裸 id，跨表碰撞吞掉 CRITICAL 通知

- **位置**：`frontend/src/composables/useNotificationStream.ts:182`
- **问题**：`source=all` 合并 trade_events 与 audit_logs 两个独立自增 id 空间；trade id=7 先出现，audit id=7 的 KILL_SWITCH 通知被去重吞掉。后端返回了 `source` 字段但没用上。
- **修复方向**：去重 key 用 `${item.source}:${item.id}`。

### ⬜ 17. trade 事件 severity 硬编码 None → 风险事件渲染成 2 秒 INFO toast

- **位置**：`backend/app/services/event_list_service.py:43`
- **问题**：RISK_PAUSED/ORDER_REJECTED 在推送渠道按 WARNING/CRITICAL 发（`notify_risk_event` 映射），在前端通知中心却因 `severity=None` → `parseSeverity` 默认 INFO，一闪而过。
- **修复方向**：`_trade_row_to_out` 复用 `notify_risk_event` 的同一份 severity 映射。

### ⬜ 18. `source=all` 时 skip_category 过滤不作用于 audit 查询 + 深分页错行

- **位置**：`backend/app/services/event_list_service.py:137-155`
- **问题**：① skip_category 只过滤 trade 查询（137 行），audit 行原样混入且 `total = trade_total + 未过滤的 audit_total` 虚高；② 深分页超过 `_MAX_MERGED_FETCH=2000` cap 后 `merged[start:…]` 切的是不完整合并集，返回错误行，`total` 却声称那些页存在。
- **修复方向**：① skip_category 生效时 audit 查询返回空集（audit 没有 skip_category 概念）；② 超过 cap 的页返回 4xx 或把 total clamp 到可达范围。

### ⬜ 19. "一次性"迁移 UPDATE 每次启动都执行

- **位置**：`backend/app/database.py:157-159`（`_ensure_runtime_state_daily_pnl_date_column`）
- **问题**：归零 `daily_pnl`/`consecutive_losses` 的 UPDATE 在"列缺失"分支**之外**，任何 `daily_pnl_date IS NULL` 的行每次启动都被反复清零，且用 `DATE('now')`（UTC）而非交易所本地日。
- **修复方向**：UPDATE 移入 `daily_pnl_date` 新增列的分支内。

### ⬜ 20. preview 节流缺冷启动守卫

- **位置**：`backend/app/services/llm_advisor_service.py:391`（对照 `:611` 的 analysis 路径）
- **问题**：analysis 路径修过的 `<= 0` 冷启动判断没抄到 preview 路径——`monotonic() < 60` 的环境里首次 preview 被误限流。
- **修复方向**：补同一守卫：`_LAST_PREVIEW_TIMESTAMP > 0 and …`。

---

## 🔧 设计/维护性主题（非 bug，已出现实际漂移）

| # | 主题 | 证据 | 收敛方向 |
|---|------|------|----------|
| D1 | 多 symbol 是单引擎设计的补丁 | `_PendingOrder` 不知所属引擎（#2 根因）；`_pending_order` setter 保留"删第一个"单单语义（`trade_execution_service.py:190-198`） | pending 自带 restore 目标 |
| D2 | 市场属性两套 keying 已分叉 | tick 量化按 `.HK` 后缀、费率/币种/时段按 market code，散布 5 文件；无后缀 symbol 不做 tick 量化但默认按 US 收费 | 单一 MarketSpec 抽象，按 symbol 解析一次 |
| D3 | RTH 双层 gate 已漂移 | 层 A 只在 LLM cancel 相关路径调用（`runner.py:681/720`），普通 LLM 下单只走层 B——一条路径有审计、另一条没有，响应 shape 不同 | 单一 SessionPolicy，每个订单意图只判定一次 |
| D4 | skip_category 契约靠字符串字面量维护 | 7 处散布；backtest 已用 `category` 字段名漂移（`BacktestSkippedSignal.category`）；DecisionTimeline 下拉是硬编码第二份 | 后端 enum + 前端从 labels.ts 单点生成 |
| D5 | 订单状态簿记 fork | `_order_event_type_for_status` 两份逐字拷贝（`api/trade.py:136`、`runner.py:1240`）；LIVE/TERMINAL 状态集三处字面量；API 与 reconcile 路径对同一取消产生 shape 不同事件 | 单一 OrderStatusBookkeeper |
| D6 | `/api/control/*` 六端点 ~310 行复制粘贴 | start 端点已漂移（trace 时机、异常保护与其余五个不同） | `_run_control_action(action, severity, fn)` helper |
| D7 | 重复实现 | naive→UTC 规范化 9 处；symbol 规范化 3 helper + 10 inline；ISO 解析 3 份；前端日期格式化 6 份且 locale 已不一致 | `core/datetime_util.py`、`core/symbols.py`、前端 `utils/format.ts` |
| D8 | 热路径浪费 | `_on_quote` 每 tick 无条件 `_broadcast_status()`（零连接也广播）+ 持锁 O(500) 列表重建 ×2；WS broadcast 每连接重复 `json.dumps` 串行发（每个 1s 超时）；月报 O(天数×全表订单)；Dashboard 5s 轮询 4 端点不看可见性 | 广播去抖 + 空连接短路；deque(maxlen)；序列化一次 + gather；报表单次回放分桶 |
| D9 | `database.py` 20 个 `_ensure_*` 接近失控 | 默认值三处重复（`models.py:36`/`database.py:113`/`engine.py` StrategyParams）；helper 命名与实际内容已脱节（#19 同源） | 通用 models-metadata diff 补列循环 |

---

## 已检查且确认无问题的点

- `engine.sync_state()` 保留 `last_trigger_at`（冷却不丢）✓
- `_order_status_timeout_seconds == 0` 正确禁用 reconcile 超时（`trade_execution_service.py:837`）✓
- `CANCEL_PENDING` 正确绕过两层时段 gate ✓
- `max_daily_loss` 写路径有 `gt=0` 校验，falsy-zero 不成立 ✓
- WS 状态广播字段与 `useStatusStream.ts` 解析完全一致 ✓
- `/api/account` 已有 TTL 缓存 + 批量 `get_quotes` ✓
- skip_category 各值与前端 `labels.ts` 当前一致（但见 D4 漂移风险）✓
