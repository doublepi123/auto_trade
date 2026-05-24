# Auto Trade 迭代计划 (Iteration Roadmap)

> 本文档根据项目当前状态、近期完成工作、以及原始设计规格中的非目标/限制项制定。每次迭代聚焦单一主题，确保可交付、可验证、可回滚。

---

## 项目当前状态快照

| 维度 | 状态 |
|------|------|
| **核心交易能力** | ✅ 就绪。区间交易策略引擎、长桥SDK集成、风控系统、订单执行全部就位。 |
| **Web UI** | ✅ 就绪。Dashboard、Strategy、Credentials、Trade History、Decision Timeline 页面，Dashboard 已包含实时价格/盈亏图表。 |
| **API 覆盖** | ✅ 完备。策略配置、凭证管理、订单查询、状态获取、状态历史、事件时间线、运行时控制（启停/暂停/Kill Switch）。 |
| **WebSocket 推送** | ✅ 就绪。实时状态同步。 |
| **本地部署** | ✅ 就绪。Docker Compose 一键启动。 |
| **测试** | ✅ 就绪。Backend pytest 单元测试、Frontend Cypress E2E 测试。 |
| **凭证安全** | ✅ 就绪。主密钥 + AES-GCM 加密存储，前端不回显明文。 |
| **数据库** | ✅ 就位。SQLite，含运行状态、状态快照、订单、LLM 交互、交易事件和凭证配置。 |

---

## 近期已完成迭代 (2026-05-17)

- **可维护性重构 (Maintainability Refactor)**
  - 提取 `TradeExecutionService`：将订单执行、状态查询、通知、PnL 从 `AppRunner` 中解耦。
  - 提取 `RuntimeStateService`：将引擎与风控状态的加载/持久化解耦。
  - 重构 `AppRunner`：成为纯粹的生命周期协调器（订阅行情、路由事件、后台保活）。
  - 前端 API 层拆分为按域模块 (`client.ts`, `strategy.ts`, `credentials.ts`, `trade.ts`)。
  - 引入 Vue Composables (`useDashboardData`, `useStatusStream`, `useAccountRefresh`, `useFormState`) 清理页面级逻辑。
  - 补充 E2E 测试（Cypress 全页面导航、控制、策略、凭证、历史），Dashboard 可用性区分不可用与零值。

## 2026-05-24 全面审计 & 下一步迭代（取代下文 P4~P8 排序）

> 本节为 2026-05-24 一次完整代码审计后的产物，**优先级高于下文 2026-05-22 列出的 P4~P8**。原 P3 回测已完成，原 P4~P8 主题保留但顺延到 P9 之后。
>
> 审计基线：`pytest 374 passed, 1 skipped`、前端 `vue-tsc` 通过、`basedpyright` 报 3 处 `object`→数值类型推断错误（`llm_advisor.py:190`、`runner.py:786`、`runner.py:795`）。

### 审计发现（按严重度）

#### A. 交易正确性 / 数据真实性

1. **`DataAggregator` 完全伪造 LLM 用的历史 K 线**（`backend/app/services/data_aggregator.py:47-81`）
   - `_fetch_daily_candles` / `_fetch_minute_candles` 只调用 `broker.get_quote()` 一次，用 `last_price * 0.98/1.02` 合成 1 根假 OHLC 返回。
   - ATR(14) 需要 ≥5 根 → 永远返回 `0`；布林带需要 ≥10 根 → 永远返回 `(0, 0, 0)`。
   - `LLMAdvisorService.analyze` 把这一根伪造数据塞进 prompt 的"最近 7 天日 K"表，**整个 LLM 顾问回路一直在假数据上跑。**
   - longport SDK 实际暴露 `QuoteContext.candlesticks` / `history_candlesticks_by_date` / `history_candlesticks_by_offset`，但 `BrokerGateway` 没包装。

2. **`IntervalApplicationService` LONG/SHORT 行为与设计文档相反**（`backend/app/services/interval_application_service.py:146-183`）
   - CLAUDE.md / 原设计：`LONG → 只允许上抬 sell_high`，`buy_low 忽略`。
   - 实现：`if new_buy_low <= old_buy_low: config.buy_low = new_buy_low` —— 实际允许 LONG 状态**下调** buy_low（即追加买入），SHORT 镜像同理。
   - 必须二选一：要么改代码恢复"只放宽不收紧"，要么改文档承认追价行为。

3. **`daily_pnl` / `consecutive_losses` 按 UTC 日切日**（`backend/app/core/risk.py:165`、`backend/app/services/daily_pnl_service.py:65`）
   - 美股 RTH = UTC 14:30~21:00；港股 RTH = UTC 01:30~08:00。UTC 00:00 切日点落在两边都不合理的时刻。
   - 结果：日内连损/亏损上限会在不该重置的时点重置。

4. **HK 标的没有 tick 量化**（`backend/app/services/trade_execution_service.py:217`）
   - `_normalize_limit_price` 只处理 `.US` 的 0.01 tick；HK 阶梯 tick（0.001/0.005/0.01/0.02/0.05）未实现，下单价格可能被券商拒。

#### B. 安全

5. **`require_api_key` 实际只挂在 `/strategy/llm-interval/preview` 一个端点**（`backend/app/api/auth.py` + `backend/app/api/llm_advisor.py:124`）
   - 全部变更类 endpoint 裸奔：`/api/control/{start,stop,pause,resume,kill-switch,disable-kill-switch}`、`PUT /api/strategy`、`PUT /api/credentials`、`POST /api/orders/{id}/cancel`、`WS /ws`。
   - 即使是挂了的端点，`auth.py:21` 的 `if settings.env in ("dev","test") and not provided: return` 让 dev/test 下"不带 header 即放行"。
   - 内网假设下尚可接受，但同时也意味着 `AUTO_TRADE_API_KEY` 实际形同虚设。

#### C. 可靠性 / 恢复

6. **`runner.start()` 在 `lifespan` 中同步阻塞事件循环**（`backend/app/main.py:146`）：`_initialize_runner` 包含多次券商网络调用，会让 FastAPI 启动期阻塞秒级。
7. **`TradeExecutionService._entry_positions` 仅在内存**：commits `e094691`/`1499828` 引入加权入场成本修复 broker `avg_price` 偏差，但进程重启后丢失，退回到被修复前的状态。
8. **Longport WebSocket 没有重连**（`backend/app/core/broker.py:253`）：只能靠 `_refresh_quote_if_stale` 每 15s 主动拉 quote 续命，底层订阅断了不会重订。
9. **缺少交易时段守卫**：盘前/盘后/休市 quote 也会触发下单。
10. **券商调用无 retry/backoff**：限流靠 `_is_auto_resumable_pause_reason` 字符串匹配（含中文 `限流` `频率`），易漏判。

#### D. 性能

11. **`/api/account` 对每个持仓循环 `broker.get_quote()`**（`backend/app/api/trade.py:353`）—— N 次往返，每次刷新都触发。
12. **`/api/orders?scope=today` 每次都去券商拉**，与 runner 后台 15s 同步重复，浪费 quota。
13. **`TradeExecutionService._wait_for_order_completion` 是死代码**（已被 pending reconcile 取代，且内含阻塞 `time.sleep`）。
14. **`AppRunner._recent_quotes` 无上限**，只靠时间窗剪枝。

#### E. 测试覆盖盲区

15. `DataAggregator` 零测试 —— 假数据 bug 因此长期未被发现。
16. `IntervalApplicationService._apply_long` 无对照规范的断言，行为漂移没被守护。
17. 无"重启 + pending 订单存在"的端到端集成测试，仅有单元级 mock。

### 迭代排序（2026-05-24 起执行）

| 顺序 | 代号 | 主题 | 价值 | 预估工时 |
|------|------|------|------|----------|
| 1 | **P1** | DataAggregator 真实 K 线 | 直接修复 LLM 决策基础 | 1~2 天 |
| 2 | **P2** | API 鉴权收紧 | 关上"内网漂移到半公网"时的最大漏洞 | 1 天 |
| 3 | **P3'** | 交易日 + HK tick | 多市场正确性，避免风控/日 PnL 错切日 | 2~3 天 |
| 4 | **P4'** | 入场成本持久化 + 重启对账 | 修复重启后 PnL 计算回退到 broker `avg_price` | 2 天 |
| 5 | **P5'** | lifespan 非阻塞 + WS 重订 | 提升运行时韧性 | 1~2 天 |
| 6 | **P6'** | 性能：批量 quote + 去重 today_orders | 降低券商 quota 与前端延迟 | 0.5 天 |
| 7 | **P7'** | `IntervalApplicationService` 对齐文档 | 终止行为与文档不一致 | 0.5 天 |
| 8 | **P8'** | lint / 死代码清理 | 还清现存类型债 | 0.5 天 |
| 9+ | 沿用下文 | 审计日志、移动端、复盘工作台、观察列表 | 顺延执行 | — |

### P1：DataAggregator 真实 K 线

> **目标：** 让 LLM 顾问拿到真实历史，恢复 ATR/布林带的有效性。

#### 范围

- `backend/app/core/broker.py`
  - 新增 `BrokerGateway.get_candlesticks(symbol: str, period: str, count: int) -> list[BrokerCandle]`，封装 `quote_ctx.history_candlesticks_by_offset(symbol, period, AdjustType, count, direction)`。
  - 数据结构 `BrokerCandle(timestamp, open, high, low, close, volume)`，统一返回 `datetime` 而非字符串。
- `backend/app/services/data_aggregator.py`
  - `_fetch_daily_candles` 改为请求最近 30 根日 K（保留 `slice[-7:]` 给 prompt 但 ATR/布林带用全部）。
  - `_fetch_minute_candles` 改为请求最近 60~120 根 1 分钟 K。
  - 删除 `quote.last_price * 0.98/1.02` 合成代码；如果 broker 不可用就返回空列表并让 prompt 提示"历史数据不可用"。
  - `fetch_market_data` 接受外部传入的 `BrokerGateway`（避免每次新建 + close），由 `LLMAdvisorService` 注入 `runner.broker` 或独立实例。
- `backend/tests/test_data_aggregator.py`（新）
  - 用 mock broker 验证 7 根日 K → ATR(14) 非零、布林带三值与 mean/std 一致。
  - 验证 broker 抛错时 `fetch_market_data` 返回空列表而非合成数据。
- `backend/tests/test_broker.py` 增 candlesticks 包装测试（mock longport）。

#### 验证

- [ ] `pytest tests/test_data_aggregator.py tests/test_broker.py tests/test_llm_advisor.py -v` 全通。
- [ ] `basedpyright` 不引入新错误。
- [ ] 手工：开启 `AUTO_TRADE_ENV=dev`，触发 `/api/strategy/llm-interval/preview`，检查 prompt 文本里 `日 K 表` 行数 >1 且各行 OHLC 不再相同。

### P2：API 鉴权收紧

> **目标：** 让 `AUTO_TRADE_API_KEY` 真正生效，关闭 dev/test 自动放行。

#### 范围

- `backend/app/api/auth.py`
  - 拆为两个 dependency：`require_api_key`（强制）与 `require_api_key_optional`（兼容内网空 key）。
  - 删除 `if settings.env in ("dev","test") and not provided: return` 分支；改为"未配置 key 时 GET 放行、变更类拒绝"，由路由选用。
- `backend/app/main.py`
  - `include_router` 时显式给变更类 router 加 `dependencies=[Depends(require_api_key)]`：`trade_router`（限 control + cancel）、`strategy_router`（限 PUT/POST）、`credentials_router`、`llm_advisor_router`（analyze/enable/disable）。
  - 拆分 `trade_router` 为读 / 写两份，或对 control 路由用 `APIRouter(..., dependencies=[...])` 嵌套。
- `backend/app/api/ws.py`
  - 支持 `?token=<api_key>` query 校验；启动横幅打印 `auth=on/off`。
- 测试：`tests/test_auth.py`（新），覆盖未配置 / 配置 + 正确 header / 配置 + 错误 header / WS 三种路径。

#### 验证

- [ ] 设置 `AUTO_TRADE_API_KEY=test` 后，无 header 调用 `POST /api/control/pause` 返回 401。
- [ ] 不设置 `AUTO_TRADE_API_KEY` 时，无 header 调用 `POST /api/control/pause` 仍按当前内网假设放行（README 同步澄清）。
- [ ] `WS /ws?token=wrong` 被拒。

### P3'：交易所感知交易日 + HK tick

> **目标：** 用标的所在市场的"交易日"切风控/日 PnL；让 HK 限价单价格落到合法 tick。

#### 范围

- 新建 `backend/app/core/market_calendar.py`
  - `trade_day_for(market: str, instant: datetime) -> date`：US 用 ET，HK 用 HKT，按 RTH 结束时间切日（US 16:00 ET / HK 16:00 HKT）。
  - `is_trading_hours(market: str, instant: datetime) -> bool`（为后续交易时段守卫预留）。
- `backend/app/core/risk.py`：`RiskController` 接受 `trade_day_provider: Callable[[], date]`，默认仍 UTC（保持向后兼容），由 `AppRunner` 注入 market-aware 版本。
- `backend/app/services/daily_pnl_service.py`：`calculate(trade_day=...)` 调用方传入按市场计算的 `target_day`。
- `backend/app/services/trade_execution_service.py`：`_normalize_limit_price` 加入 HK 阶梯 tick 表（来自港交所规则），同 `BUY` 向下、`SELL` 向上的取整方向。
- 测试：
  - `tests/test_market_calendar.py`（新）：覆盖 US/HK 交易日边界、夏令时切换。
  - `tests/test_risk.py`、`tests/test_daily_pnl_service.py` 补"交易日切日 = 交易所收盘"用例。
  - `tests/test_trade_execution_service.py` 补 HK tick 量化用例。

#### 验证

- [ ] 模拟 `2026-05-24 13:00 UTC`（美股 9:00 ET，盘前）和 `2026-05-24 19:30 UTC`（美股 15:30 ET，临近收盘），同一个 US 标的视作同一交易日。
- [ ] HK 标的报价 `0.243` 时 BUY 限价被量化到 `0.240` / SELL 到 `0.245`（步长 0.005）。

### P4'：入场成本持久化 + 重启对账

> **目标：** 进程重启后仍能用加权入场成本计算平仓 PnL，避免回到 broker `avg_price` 偏差。

#### 范围

- 新表 `tracked_entries(symbol PK, quantity, cost, updated_at)`，`database._ensure_tracked_entries_table` 补丁旧库。
- `TradeExecutionService._record_entry_price` / `_consume_entry_quantity` 改为同步落表（保留内存缓存当 fast path）。
- `AppRunner._initialize_runner` 启动时把 `tracked_entries` 注入回 `_trade_svc._entry_positions`。
- 启动对账：若 `tracked.quantity` 与 broker `position.quantity` 偏差 > 5% 或差值绝对量 > 阈值，写 `RECONCILE_DRIFT` 事件 + 通知。
- 测试：`tests/test_trade_execution_service.py` 加"重启 → tracked 复原 → 平仓 PnL 用 tracked avg"用例；`tests/test_runner.py` 加 reconcile 漂移用例。

#### 验证

- [ ] 端到端：`BUY 100@10` → 重启 → broker 改 `avg=11`（mock）→ `SELL 100@12`，PnL 仍按 10 算 ≈ 200。

### P5'：lifespan 非阻塞 + WS 重订

> **目标：** FastAPI 启动不被券商网络阻塞；行情 WebSocket 断线能自愈。

#### 范围

- `backend/app/main.py:lifespan`：`await asyncio.to_thread(get_runner().start)`，捕获返回 False 时仅记 warning 不阻塞。
- `backend/app/core/broker.py`：
  - 利用 longport SDK 的 disconnect 回调（如有）；若无，新增"看门狗"：`AppRunner._run_loop` 已每 5s 跑，扩展为检测 `_last_quote_at` 超过阈值且 quote_ctx 仍声称 alive 时强制 `unsubscribe + subscribe`。
- 测试：`tests/test_runner.py` 用 fake broker 注入"60s 无 quote"场景，断言触发重订。

#### 验证

- [ ] 启动期 `/api/health` 在 1s 内返回（即便 broker 凭证为空）。
- [ ] 模拟订阅丢失后，30s 内 quote 恢复推送。

### P6'：性能优化

#### 范围

- `backend/app/api/trade.py:get_account` 用 `broker.quote([s for s in positions])` 一次取回所有报价。
- `backend/app/api/trade.py:get_orders` 默认从 DB 读 today 订单，仅当 `?refresh=1` 时触发 broker 同步；前端按钮触发 refresh。
- `backend/app/runner.py:_remember_quote` 给 `_recent_quotes` 加 `len ≤ 500` 硬上限。

#### 验证

- [ ] 持仓 5 个标的时 `/api/account` 单次延迟下降到接近一次 quote 的耗时。
- [ ] 高频 quote 注入下 `_recent_quotes` 长度稳定。

### P7'：IntervalApplicationService 对齐文档 ✅（2026-05-25 完成 — 方案 B）

- 决策：**保留现行追价/加仓逻辑**，更新 Roadmap 的"迭代 0 → 0.2 渐进式平滑过渡策略"小节，明确描述实际行为是"LLM 在持仓状态下可以下调 buy_low 追价加仓 / 上抬 sell_high 拉高目标价"。
- 行为对照表见上文 `迭代 0 / 0.2`。
- `tests/test_interval_application.py` 已包含此行为断言；后续可在该文件加注释链接本节作为权威说明。

### P8'：lint / 死代码清理

- 修 `basedpyright` 3 处错误（用 `isinstance` 或显式 cast）。
- 删除 `TradeExecutionService._wait_for_order_completion`。
- 删除 `Settings.frontend_port`（未使用）。

---

## 后续迭代计划（2026-05-22 更新）

> 注：以下 P4~P8 在 2026-05-24 审计后顺延到 P1~P8' 完成之后再执行，主题不变。

当前系统已经完成 LLM 自动决策上下文、主动价格刷新、订单同步、今日订单分页与撤单、决策时间线、Dashboard 图表化监控。后续计划按“先降低交易风险，再增强复盘能力，再提升运维体验”的顺序推进。

### P3：回测与参数验证 MVP ✅（已完成）

> **目标：** 在实盘继续运行前，用历史价格序列验证当前策略区间、最低盈利金额、止损/撤单规则的收益和风险。

#### 已交付

- 后端 `BacktestEngine` + `POST /api/backtest/run`（CSV：`timestamp,open,high,low,close,volume`）
- 前端 **Backtest** 页：CSV 上传/粘贴、参数表单、收益曲线、交易明细、手续费敏感性
- pytest + Cypress `backtest.cy.ts` 覆盖主流程

#### 后续可选增强

- Sharpe / 盈亏比等扩展指标（Roadmap 原列项，当前 UI 未展示）

### P4：交易执行安全与成本控制增强

> **目标：** 进一步降低“频繁撤单重挂、连续小额交易、手续费吞噬收益、持仓/融券数量越界”的风险。

#### 范围

- 统一交易预检服务：所有自动下单路径（区间触发、LLM 即时动作、止损、撤单重挂）都走同一套校验。
- 增加冷却/合并策略：短时间内同方向重复信号合并，只保留最优挂单。
- 增加手续费估算与盈亏门槛：买入/卖出完整 round-trip 预估手续费后，再判断区间是否值得执行。
- 对撤单重挂设置最小改价阈值：价格差距不足以覆盖手续费/滑点时不重挂。
- 在 Dashboard 和决策时间线展示“被跳过的交易原因”。

#### 验证

- 后端测试覆盖：同方向重复单被合并、卖多不超过可用持仓、平空不超过可用融券、低收益重挂被拒绝。
- Cypress 覆盖 Dashboard 最近动作和时间线显示跳过原因。

### P5：操作审计与多渠道报警

> **目标：** 把运维行为、风险事件、关键交易动作完整审计化，并支持 Server 酱以外的通知通道。

#### 范围

- 新增 `AuditLog`：记录策略修改、凭证修改、启动/停止/暂停/恢复/Kill Switch、手动撤单。
- 审计字段包含：动作、操作者哈希、来源 IP、请求摘要、结果、时间。
- 通知抽象为 Notifier 接口，支持 Server 酱和通用 Webhook。
- 风控事件分级：`INFO`、`WARNING`、`CRITICAL`；Kill Switch、止损失败、订单拒绝进入高优先级。
- 前端 Credentials 增加通知渠道配置；Decision Timeline 增加审计事件筛选。

#### 验证

- 后端测试覆盖审计写入、Webhook payload、通知失败降级。
- Cypress 覆盖通知渠道配置和审计筛选。

### P6：移动端与应急操作体验

> **目标：** 手机上能可靠查看状态和执行紧急操作，避免桌面不可用时无法止损/暂停。

#### 范围

- App 顶部导航移动端改为紧凑菜单或底部 Tab。
- Dashboard 移动端优先展示：价格、持仓、盈亏、暂停/Kill Switch、最近订单。
- 图表在小屏幕可折叠，避免横向滚动。
- 增加移动端 Cypress 视口测试：iPhone、Pixel。
- 可选：基础 PWA manifest，让页面可添加到主屏幕。

#### 验证

- Cypress 移动端视口无横向滚动，关键按钮可点击，文字不溢出。
- 浏览器手工验证 Dashboard、Strategy、History、Decision Timeline。

### P7：策略复盘与 LLM 优化工作台

> **目标：** 利用已经保存的 LLM 交互、状态快照、订单事件，分析哪些提示词/决策真正赚钱。

#### 范围

- 新增“复盘”页面：按日期/标的展示价格走势、LLM 建议、执行结果、真实 PnL。
- 将 `llm_interactions` 与 `trade_events`、`orders` 通过 `interaction_id`/`order_id` 关联展示。
- 支持导出复盘数据为 JSON/CSV。
- 增加“错误类型”标签：过早买入、过早卖出、错过止损、频繁重挂、收益不足。

#### 验证

- 后端测试覆盖复盘聚合 API。
- Cypress 覆盖筛选、详情展开、导出。

### P8：多标的观察列表（暂不自动交易）

> **目标：** 先支持多个标的的行情和 LLM 观察，暂不允许多标的自动下单，避免交易风险突然扩大。

#### 范围

- 支持 Watchlist：多个 symbol 的价格、波动、LLM 建议、风险摘要。
- 每次只允许一个“交易标的”处于自动交易状态。
- Dashboard 增加观察列表表格，不改现有单标的交易引擎。

#### 验证

- 后端测试覆盖观察列表 CRUD 和行情聚合。
- Cypress 覆盖添加/删除观察标的、切换交易标的前的确认流程。

### 建议执行顺序

| 顺序 | 迭代 | 原因 |
|------|------|------|
| 1 | P3 回测与参数验证 MVP | 最能降低实盘盲调风险，也为后续策略优化提供基准。 |
| 2 | P4 交易执行安全与成本控制增强 | 直接回应手续费、频繁下单、持仓越界等真实交易风险。 |
| 3 | P5 操作审计与多渠道报警 | 让系统进入更可运维、可追责、可恢复的状态。 |
| 4 | P6 移动端与应急操作体验 | 提升紧急止损/暂停的可达性。 |
| 5 | P7 策略复盘与 LLM 优化工作台 | 基于已沉淀数据优化 prompt 和决策规则。 |
| 6 | P8 多标的观察列表 | 扩展能力，但先限制为观察，避免放大自动交易风险。 |

### 下一步建议

下一批优先做 **P3 回测与参数验证 MVP**，并拆成两次提交：

1. 后端回测引擎 + API + 单元测试。
2. 前端回测页面 + 图表 + Cypress + Docker 验证。

---

## 迭代目标

在保持现有功能稳定的基础上，逐步消除 README 中列举的"限制"项，并向生产级运维体验演进。当前计划覆盖 5 个迭代，主题分别为：**LLM智能区间调整**、**回测系统**、**实时图表与监控增强**、**日志审计与报警**、**响应式与移动端适配**。LLM 智能区间调整作为当前最高优先级，因为它直接提升策略的核心交易决策质量。

---

## 迭代 0：LLM 智能区间调整 (LLM Intelligent Interval)

> **目标：** 通过 DeepSeek LLM 分析市场行情，自动为策略推荐买入/卖出价格区间，减少人工频繁调整的依赖。策略运行期间持续优化区间，实现动态自适应交易。
> **价值：** 核心交易决策智能化，显著降低人工调参成本，提升策略对不同市场环境的适应能力。
> **设计文档：** [docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md](docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md)

### 任务

#### 0.1 数据聚合与 LLM 顾问服务

- 新建文件：
  - `backend/app/services/data_aggregator.py`：聚合长桥历史行情、实时价格、持仓状态、最近成交记录，构建 LLM Prompt。
  - `backend/app/services/llm_advisor_service.py`：调用 DeepSeek API，解析 JSON 响应，防抖控制（30 分钟窗口）。
- 实现细节：
  - 输入数据：加权组合（7 天日 K 权重 60% + 24 小时分钟 K 权重 40%），附带 ATR、布林带指标。
  - Prompt 要求 LLM 输出 `suggested_buy_low`, `suggested_sell_high`, `confidence_score`, `analysis`。
  - 失败处理：API 超时 30s，重试一次，失败时通过 Server酱通知 "LLM 区间分析失败"。

#### 0.2 渐进式平滑过渡策略

- 新建文件：
  - `backend/app/services/interval_application_service.py`：核心规则引擎。
- 实现细节（**2026-05-25 更新：实际行为是"追价加仓"，非原"只放宽不收紧"**）：
  - **FLAT（空仓）**：LLM 建议立即生效。
  - **LONG（持多）**：sell_high 优先取 `max(old, new)`；若 `new_sell_high < old_sell_high`，强制不低于 `current_price * (1 + 波动阈值)` 防贴现价。new_buy_low 仅在 `≤ old_buy_low` 时下调（**允许 LLM 追价加仓**），不会上抬 buy_low。
  - **SHORT（持空）**：镜像 LONG —— buy_low 仅在 `≤ old_buy_low` 时下调；否则取 `min(new, current_price * (1 - 波动阈值))`，**允许 LLM 追价加空**。
  - **风控兜底**：置信度 < `llm_min_confidence`（默认 0.7）拒绝；区间宽度 > `llm_max_stripe_width_pct`（默认 8%）拒绝；区间宽度 < `min_exit_profit_pct * current_price` 或 `min_profit_amount/reference_quantity` 拒绝。

#### 0.3 定时触发与手动触发

- 新建文件：
  - `backend/app/crontabs/interval_analysis_cron.py`：APScheduler 定时任务（每 4 小时）。
- 新增 API：`POST /api/strategy/llm-interval/analyze`（手动触发），`GET /api/strategy/llm-interval/status`（状态查询）。

#### 0.4 前端 UI 集成

- 修改文件：
  - `frontend/src/views/Strategy.vue`：增加 LLM 智能区间卡片（开关、置信度显示、建议区间、上次应用原因）。
  - `frontend/src/views/Dashboard.vue`：状态栏增加 LLM 下次分析时间指示器。
- 新增 API Client：`frontend/src/api/llm_advisor.ts`。

#### 0.5 数据库迁移

- Alembic 迁移：为 `strategy_config` 表新增 `auto_interval_enabled`, `llm_suggested_buy_low`, `llm_suggested_sell_high`, `llm_confidence_score`, `llm_analysis`, `llm_last_analysis_at`, `llm_next_analysis_at`, `llm_applied_buy_low`, `llm_applied_sell_high`, `llm_applied_at`, `llm_reject_reason`。

#### 验证

- [ ] 后端 pytest：覆盖渐进式过渡规则、风控兜底、LLM 解析、防抖限制。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `strategy_llm_*.cy.ts` 和 `dashboard_llm_indicator.cy.ts`。
- [ ] Docker Compose 启动后，手动触发分析返回正确结构；30 分钟内重复触发被限制。

---

## 迭代 1：回测系统 (Backtesting)

> **目标：** 消除 README 中"暂不支持回测系统"的限制。实现策略的历史数据回测验证，帮助用户在实盘运行前验证参数合理性。
> **价值：** 显著降低盲目上线风险，是量化交易策略上线前的核心验证环节。

### 任务

#### 1.1 设计回测数据接口与数据层

- 新建文件：
  - `backend/app/core/backtest.py`：回测引擎（纯数据驱动的策略推演）。
  - `backend/app/api/backtest.py`：FastAPI 路由：`POST /api/backtest/run`。
  - `backend/app/schemas.py`：新增 `BacktestResult`, `BacktestParams`, `BacktestTradeLog`。
  - `backend/tests/test_backtest.py`：单元测试。
- 实现细节：
  - `BacktestEngine` 不依赖实时的长桥行情，而是接收历史价格序列和策略参数，逐 K 线推演状态机（flat -> long -> flat 或 flat -> short -> flat），输出每笔虚拟成交。
  - 价格序列由用户提供（CSV 上传）或长桥历史行情接口获取（二期可选；一期先用用户上传）。
  - 回测需要模拟风控逻辑（日亏损、连续亏损），但不触发真实通知和真实下单。

#### 1.2 前端回测页面

- 新建/修改文件：
  - `frontend/src/views/Backtest.vue`：策略参数选择与历史数据上传界面。
  - `frontend/src/api/backtest.ts`：`runBacktest` API 调用。
  - `frontend/src/router/index.ts`：新增 `/backtest` 路由，导航栏加入"回测"入口。
- 实现细节：
  - 支持配置回测参数（与实盘策略参数共用 `StrategyConfig`，但可选覆盖）。
  - 上传 CSV（格式：`timestamp,open,high,low,close,volume`）。
  - 提交后端生成回测结果，前端以表格和图表展示。

#### 1.3 回测结果可视化

- 前端新增 `frontend/src/components/BacktestChart.vue`：
  - 采用折线图展示标的收盘价与买卖信号点。
  - 采用柱状图展示每日盈亏（PnL）。
  - 表格展示每笔虚拟成交记录（时间、动作、价格、数量、盈亏）。
  - 汇总指标：总收益率、最大回撤、夏普比率、胜率、盈亏比。

#### 验证

- [ ] 后端 `test_backtest.py` 通过（至少覆盖 flat-long-flat 与 flat-short-flat 两条主路径）。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `backtest.cy.ts` 覆盖页面交互。
- [ ] Docker Compose 全栈启动后，`curl -X POST http://localhost:8000/api/backtest/run` 返回正确结构。

---

## 迭代 2：实时图表与监控增强 (Dashboard Visualization)

> **目标：** Dashboard 从纯文本仪表盘升级为带实时折线图、盈亏曲线的监控中心。
> **价值：** 将离散的价格/盈亏数字转化为时间序列图形，帮助用户快速判断策略当前状态和历史走势。
> **状态：** ✅ 已完成。实现采用项目内轻量 SVG 图表组件，避免引入新的前端网络依赖。

### 任务

#### 2.1 后端增加历史状态查询接口

- 新建/修改文件：
  - `backend/app/api/status.py`：新增 `GET /api/status/history?from=...&to=...`，按区间查询历史 `RuntimeState` 快照。
  - `backend/app/services/runtime_state_service.py`：补充 `query_history` 方法。
- 实现细节：
  - 基于 `orders` 表和 `runtime_state` 表的数据，按时间范围聚合返回价格/盈亏序列。

#### 2.2 Dashboard 实时价格与盈亏曲线

- 新建/修改文件：
  - `frontend/src/components/PriceChart.vue`：实时价格折线图，随 WebSocket 消息更新。
  - `frontend/src/components/PnLChart.vue`：盈亏柱状图/面积图，随 WebSocket 消息更新。
  - `frontend/src/views/Dashboard.vue`：引入图表组件，重新排布布局。
- 实现细节：
  - 图表库选择：优先使用 `vue-echarts`（轻量、与 Vue 3 生态兼容），替代重量级图表库。
  - 数据点缓存限制：前端保留最近 200 个数据点，保持内存和渲染性能。
  - 图表需要区分"历史加载"（页面打开时 REST 查询）和"实时追加"（WebSocket 推送）。

#### 2.3 交易信号标记

- 图表上标记策略触发的买入/卖出点位，以箭头和颜色区分。
- 数据来源：页面打开时加载 `GET /api/orders` 的最近成交记录，并在 WebSocket 收到新的风控通过+下单事件时实时追加标记。

#### 验证

- [x] 前端 `npm run build` 通过。
- [x] Dashboard 打开后图表能加载历史状态数据，并通过状态流追加最新价格/盈亏点。
- [x] Cypress 新增 `dashboard_charts.cy.ts` 页面校验图表与交易信号标记渲染。

---

## 迭代 3：日志审计与报警系统 (Audit Logging & Alerting)

> **目标：** 由当前的 Server酱单一通知渠道，升级为支持自定义 Webhook、Email 的多渠道报警系统；并增加操作审计日志。
> **价值：** 生产环境运维需要更灵活的报警通道（如企业微信、钉钉、Slack）；操作审计满足合规与排障需求。

### 任务

#### 3.1 操作审计日志

- 新建/修改文件：
  - `backend/app/models.py`：新表模型：`AuditLog`（`id`, `action`, `actor`, `detail`, `ip`, `created_at`）。
  - `backend/app/core/audit.py`：`AuditLogger`，封装审计日志记录逻辑。
  - `backend/app/api/*.py`：核心操作（启动/停止/暂停/Kill Switch/策略修改/凭证修改）调用 `AuditLogger.record(...)`。
  - `backend/alembic/versions/`：生成数据库迁移脚本。
- 实现细节：
  - `action` 枚举：`START`, `STOP`, `PAUSE`, `RESUME`, `KILL_SWITCH`, `STRATEGY_UPDATE`, `CREDENTIALS_UPDATE`。  
  - `actor`：从请求头中读取 `X-API-Key` 的哈希（不存明文）。
  - `ip`：读取请求 `X-Forwarded-For` 或 `client.host`。

#### 3.2 通知渠道扩展

- 新建/修改文件：
  - `backend/app/core/notify.py`：重构为策略模式：`NotifierInterface`。
  - `backend/app/core/notifiers/`：新目录，包含：
    - `serverchan.py`：现有 Server酱实现。
    - `webhook.py`：通用 Webhook POST 通知（允许用户配置任意 URL 和模板）。
- 实现细节：
  - 凭证配置中新增 `notification_channel` 字段（`serverchan` | `webhook`），后端根据配置实例化对应 Notifier。
  - 前端 Credentials 页面增加通知渠道选择表单。

#### 3.3 风控事件报警分级

- 修改：
  - `backend/app/core/risk.py`：不同级别事件调用不同的通知方法（如风控暂停 vs Kill Switch 可分别指定通知级别）。
- 实现细节：
  - `notify_risk_event` 增加 `severity` 参数（`WARNING`, `CRITICAL`）。
  - `CRITICAL` 级别同时触发所有配置的通知渠道（Server酱 + Webhook），确保不遗漏。

#### 验证

- [ ] 后端新增 `test_audit.py` 和 `test_notifiers.py`，测试覆盖日志写入与通知发送。
- [ ] Docker Compose 启动后，修改策略能触发日志写入；触发风控能收到 Webhook 消息。
- [ ] Cypress 凭证页面可切换通知渠道。

---

## 迭代 4：响应式与移动端适配 (Responsive & Mobile)

> **目标：** 让前端在移动浏览器上有可用的操作体验。
> **价值：** 用户需要随时随地查看策略状态或执行 Kill Switch，移动端是高频场景。

### 任务

#### 4.1 前端响应式布局

- 修改文件：
  - `frontend/src/App.vue`：侧边栏导航改为可折叠或底部 Tab 栏（移动端）。
  - `frontend/src/views/Dashboard.vue`：关键指标以卡片流排布，图表高度自适应，控制按钮增大触控区域。
  - `frontend/src/views/Strategy.vue`：表单增加移动端单列布局。
  - `frontend/src/views/Credentials.vue`：同策略页面。
- 实现细节：
  - 使用 CSS Media Queries 与 Element Plus 的 `el-col` 响应式断点。
  - 隐藏非必要的复杂图表（如回测图表在移动端可折叠）。

#### 4.2 PWA 基础支持

- 修改文件：
  - `frontend/index.html`：增加 `manifest.json` 链接。
  - `frontend/public/manifest.json`：Web App Manifest。
  - `frontend/vite.config.ts`：使用 `vite-plugin-pwa` 配置 Service Worker（离线缓存静态资源和基础页面）。
- 实现细节：
  - 离线时页面仍然可以打开（前端缓存），但 API 调用提示"网络不可用"。
  - 目标不是完全离线可用，而是保证页面壳体和已缓存数据的离线访问。

#### 4.3 移动端安全优化

- `nginx.conf`：增加移动设备相关的响应头优化（`X-Content-Type-Options`, `Referrer-Policy`）。
- 登录/凭证页面：移动端自动隐藏密码输入框的保存提示，防止凭证泄露到设备键盘记忆。

#### 验证

- [ ] 使用 Chrome DevTools 模拟 iPhone 14 Pro、Pixel 7 设备，页面无横向滚动、按钮可点击、文字不溢出。
- [ ] 在移动 Safari 上测试 PWA "添加到主屏幕" 后图标和启动画面正确。
- [ ] Cypress 增加移动端视口尺寸的烟雾测试（`cy.viewport('iphone-x')`）。

---

## 迭代优先级总结

| 迭代 | 主题 | 价值 | 难度 | 建议顺序 |
|------|------|------|------|----------|
| 1 | 回测系统 | 极高（消除核心限制，降低实盘风险） | 中等 | **第一个** |
| 2 | 实时图表与监控增强 | 高（提升日常监控体验） | 较低 | 第二个 |
| 3 | 日志审计与报警 | 高（满足生产合规需求） | 中等 | 第三个 |
| 4 | 响应式与移动端适配 | 中高（扩展使用场景） | 较低 | 第四个 |

---

## 实施建议

1. **一次只做一条主线。** 每个迭代内可以并行开发互不影响的子任务（如前后端可并行），但不要在同一个代码基上交叉进行两个迭代的开发，避免功能碎片化。
2. **每个迭代产出物：**
   - 一个保存在 `docs/superpowers/plans/` 的实施计划文件（可参考已有格式）。
   - 完成后更新此 Roadmap，标记对应迭代完成状态。
3. **回测系统是当前最高优先级**，因为它直接消除 README 中列出的最大限制，且独立性好，不会与现有运行时逻辑产生深度耦合。
4. **测试纪律：** 每个新增功能必须有对应测试（后端 unit test + 前端 Cypress E2E），覆盖率不低于现有的 80% 水平。

---

## 附录：原始设计中的非目标与当前状态对照

| 非目标（原始） | 当前状态 | 计划 |
|---|---|---|
| 多标的组合策略 | ❌ 仍不支持 | 超出当前 Roadmap，需更大架构升级 |
| 高频交易 | ❌ 仍不支持 | 设计限制，暂不改变 |
| 复杂择时指标 | ❌ 仍不支持 | 暂不改变 |
| 回测系统 | ❌ -> ✅ 计划中 | **迭代 1** 实现 |
| 量化研究平台 | ❌ 仍不支持 | 超出范围 |
| 代客理财/公开策略分发 | ❌ 仍不支持 | 法律合规限制，永久不在计划中 |
