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
| **测试** | ✅ 就绪。Backend pytest **692** 项、Frontend Cypress E2E **80** 项。 |
| **凭证安全** | ✅ 就绪。主密钥 + AES-GCM 加密存储，前端不回显明文。 |
| **数据库** | ✅ 就位。SQLite，含运行状态、状态快照、订单、`tracked_entries`、LLM 交互、交易事件和凭证配置。 |
| **LLM 行情数据** | ✅ 真实 K 线（日 K + 1 分钟 K），ATR/布林带有效。 |
| **多市场切日** | ✅ US/HK 交易所本地日历日驱动风控与日 PnL。 |
| **入场成本** | ✅ `tracked_entries` 持久化 + 启动对账。 |
| **操作审计** | ✅ `audit_logs` 表 + `AuditLogger` + 9 个写端点接入（控制 / 策略 / 凭证 / 撤单）。 |
| **多渠道通知** | ✅ `MultiChannelNotifier` + Server 酱 + Webhook + `severity_floor` 分级。 |
| **交易时段守卫** | ✅ `trading_session_mode` 双层 gate（runner + execute 服务），`SESSION` skip 与 `TRADING_SESSION_BLOCKED` 审计。 |
| **券商韧性** | ✅ `BrokerGateway._call_with_retry` 分档退避（订单 vs 行情），`BROKER_RETRY` 审计。 |
| **LONG 加仓** | ✅ LONG 状态下 `price <= buy_low` 触发 BUY，保持 LONG；60s 冷却对齐。 |
| **保证金下单量** | ✅ `margin_safety_factor` 可配置，BrokerGateway margin 路径已验证。 |
| **LLM 持仓成本** | ✅ `ContextModule` 输出持仓方向/数量/均价/浮盈%；无持仓显示"当前无持仓"。 |

---

## 近期已完成迭代 (2026-05-26)

> 对应 commit `323743b feat: add audit notifications and trading safety`。基线 `pytest 485 passed`，`basedpyright` 0/0。规格：[2026-05-26-audit-notification-trading-safety-design.md](superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md)，实施计划：[2026-05-26-audit-notification-trading-safety.md](superpowers/plans/2026-05-26-audit-notification-trading-safety.md)。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `AuditLog` 模型 + `_ensure_audit_log_table` + `AuditLogger` + 9 个写端点审计 | ✅ |
| **T2** | 交易时段守卫（`trading_session_mode` + 双层 gate + `SESSION` skip + `TRADING_SESSION_BLOCKED`） | ✅ |
| **T3** | `BrokerGateway._call_with_retry` 分档退避（订单全量、行情低重试） + `BROKER_RETRY` 审计 | ✅ |
| **T4** | `NotifierInterface` + `MultiChannelNotifier` + `ServerChan` / `Webhook` + severity 分级 | ✅ |
| **T5** | 前端集成：Credentials 通知渠道、Strategy 交易时段、Decision Timeline `source` + 多选 + 审计卡片、Dashboard SESSION 指示器 | ✅ |
| **T6** | 测试与 lint：~50 项 pytest 新增、3 个 cypress spec、`basedpyright` 0/0 | ✅ |

**新增端点 / 改造端点**：
- `GET /api/events` 现支持 `source=trade|audit|all` 与 `event_type` 重复参数（跨表 union 分页）。
- 9 个写端点（control / strategy PUT / credentials PUT / orders cancel）记录 `audit_logs`，含 `actor_hash`（SHA-256 X-API-Key 前 16 hex）、`source_ip`、脱敏 `request_summary`、`result`、`severity`。

**新增环境变量**：`AUTO_TRADE_BROKER_RETRY_MAX`、`AUTO_TRADE_BROKER_QUOTE_RETRY_MAX`、`AUTO_TRADE_BROKER_RETRY_BASE_MS`、`AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT`。

**显式 YAGNI 未做**（仍在 Roadmap 边缘）：节假日历、审计 CSV/JSON 导出、Webhook 模板编辑器、通知重发队列。

---

## 近期已完成迭代 (2026-05-25)

> 对应 commit `feat: exchange trade days, tracked entries, and real LLM market data`。用户向说明已同步至 `README.md` / `CLAUDE.md`。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P1** | DataAggregator 真实 K 线 + `BrokerGateway.get_candlesticks` | ✅ |
| **P3'** | `market_calendar` + 风控/日 PnL 按市场切日 + HK tick | ✅ |
| **P4'** | `tracked_entries` 持久化 + 启动 `TRACKED_ENTRY_DRIFT` 对账 | ✅ |
| **P5'** | lifespan 非阻塞启动；RTH 内推送静默重订（~90s） | ✅（未接 SDK disconnect 回调） |
| **P6'** | 批量 quote、`/api/orders` 默认读 DB + `refresh`、`_recent_quotes` 上限 500 | ✅ |
| **P7'** | IntervalApplication 与文档对齐（保留追价加仓） | ✅ |
| **P8'** | 删除 `_wait_for_order_completion`、`Settings.frontend_port` | ✅ |
| **P2** | API 鉴权收紧 | ❌ 明确不做（owner decision 2026-05-25） |

审计项 **#1、#3、#4、#6、#7、#8（部分）、#11、#12、#13、#14、#15** 已随上表修复；**#2**（区间应用）按 P7' 方案 B 保留实现；**#5** 保留为已知风险但不纳入 Roadmap；**#9、#10、#17** 仍开放。

---

## 近期已完成迭代 (2026-05-17)

- **可维护性重构 (Maintainability Refactor)**
  - 提取 `TradeExecutionService`：将订单执行、状态查询、通知、PnL 从 `AppRunner` 中解耦。
  - 提取 `RuntimeStateService`：将引擎与风控状态的加载/持久化解耦。
  - 重构 `AppRunner`：成为纯粹的生命周期协调器（订阅行情、路由事件、后台保活）。
  - 前端 API 层拆分为按域模块 (`client.ts`, `strategy.ts`, `credentials.ts`, `trade.ts`)。
  - 引入 Vue Composables (`useDashboardData`, `useStatusStream`, `useAccountRefresh`, `useFormState`) 清理页面级逻辑。
  - 补充 E2E 测试（Cypress 全页面导航、控制、策略、凭证、历史），Dashboard 可用性区分不可用与零值。

## 2026-05-24 全面审计与 2026-05-25 决策记录

> 本节为 2026-05-24 一次完整代码审计后的产物。2026-05-25 用户决策明确不实施 P2；在已落地的正确性修复之后，下文 P4 成为当前下一迭代。
>
> 审计基线（2026-05-24）：`pytest 374 passed, 1 skipped`。P1–P8' 交付后 **`pytest 417 passed`**；P4 交付后（2026-05-25）**`pytest 433 passed`**，`basedpyright` 0 errors。

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
| 2 | **P2** | API 鉴权收紧 | ❌ 不实施（保留审计记录；内网部署决策） | — |
| 3 | **P3'** | 交易日 + HK tick | 多市场正确性，避免风控/日 PnL 错切日 | 2~3 天 |
| 4 | **P4'** | 入场成本持久化 + 重启对账 | 修复重启后 PnL 计算回退到 broker `avg_price` | 2 天 |
| 5 | **P5'** | lifespan 非阻塞 + WS 重订 | 提升运行时韧性 | 1~2 天 |
| 6 | **P6'** | 性能：批量 quote + 去重 today_orders | 降低券商 quota 与前端延迟 | 0.5 天 |
| 7 | **P7'** | `IntervalApplicationService` 对齐文档 | 终止行为与文档不一致 | 0.5 天 |
| 8 | **P8'** | lint / 死代码清理 | 还清现存类型债 | 0.5 天 |
| ✅ | **P4** | 交易执行安全与成本控制增强 | 手续费门槛、撤单前改价保护、跳过原因可见（2026-05-25 交付） | — |
| 后续 | 沿用下文 | 审计日志、移动端、复盘工作台、观察列表 | P4 后顺序执行 | — |

### P1：DataAggregator 真实 K 线 ✅（2026-05-25 完成）

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

- [x] `pytest tests/test_data_aggregator.py tests/test_broker.py tests/test_llm_advisor.py -v` 全通。
- [ ] `basedpyright` 不引入新错误（P8' 部分待清）。
- [ ] 手工：开启 `AUTO_TRADE_ENV=dev`，触发 `/api/strategy/llm-interval/preview`，检查 prompt 文本里 `日 K 表` 行数 >1 且各行 OHLC 不再相同。

### P2：API 鉴权收紧 ❌（owner decision 2026-05-25：不实施）

> **审计结论保留：** `require_api_key` 覆盖范围有限；若服务暴露到不可信网络，变更类 endpoint 存在未授权操作风险。
>
> **决策：** 项目当前按可信内网部署运行，用户明确选择不将 API 鉴权收紧纳入 Roadmap，以避免引入额外运维负担。未来如部署边界改变，须重新立项评估。

#### 范围

- 不实施代码或测试改造。
- `README.md` 已声明服务仅供可信内网使用且 `AUTO_TRADE_API_KEY` 为可选配置。
- 若未来恢复该主题，需要重新确认所有写路由、WebSocket 和部署边界，而不是把本节视为待办。

### P3'：交易所感知交易日 + HK tick ✅（2026-05-25 完成）

> **目标：** 用标的所在市场的"交易日"切风控/日 PnL；让 HK 限价单价格落到合法 tick。

#### 范围

- 新建 `backend/app/core/market_calendar.py`
  - `trade_day_for(market: str, instant: datetime) -> date`：US 用 ET、HK 用 HKT 的**本地日历日**（午夜切日；不含节假日历）。`is_trading_hours` 用于 RTH 窗口判断（如行情重订）。
  - `is_trading_hours(market: str, instant: datetime) -> bool`（为后续交易时段守卫预留）。
- `backend/app/core/risk.py`：`RiskController` 接受 `trade_day_provider: Callable[[], date]`，默认仍 UTC（保持向后兼容），由 `AppRunner` 注入 market-aware 版本。
- `backend/app/services/daily_pnl_service.py`：`calculate(trade_day=...)` 调用方传入按市场计算的 `target_day`。
- `backend/app/services/trade_execution_service.py`：`_normalize_limit_price` 加入 HK 阶梯 tick 表（来自港交所规则），同 `BUY` 向下、`SELL` 向上的取整方向。
- 测试：
  - `tests/test_market_calendar.py`（新）：覆盖 US/HK 交易日边界、夏令时切换。
  - `tests/test_risk.py`、`tests/test_daily_pnl_service.py` 补"交易日切日 = 交易所收盘"用例。
  - `tests/test_trade_execution_service.py` 补 HK tick 量化用例。

#### 验证

- [x] `tests/test_market_calendar.py` 覆盖 US/HK 边界；`test_risk` / `test_daily_pnl_service` 补切日用例。
- [x] HK tick：`tests/test_trade_execution_service.py`。

### P4'：入场成本持久化 + 重启对账 ✅（2026-05-25 完成）

> **目标：** 进程重启后仍能用加权入场成本计算平仓 PnL，避免回到 broker `avg_price` 偏差。

#### 范围

- 新表 `tracked_entries(symbol PK, quantity, cost, updated_at)`，`database._ensure_tracked_entries_table` 补丁旧库。
- `TradeExecutionService._record_entry_price` / `_consume_entry_quantity` 改为同步落表（保留内存缓存当 fast path）。
- `AppRunner._initialize_runner` 启动时把 `tracked_entries` 注入回 `_trade_svc._entry_positions`。
- 启动对账：若 `tracked.quantity` 与 broker `position.quantity` 偏差 > 5% 且绝对差 ≥ 1 股，写 **`TRACKED_ENTRY_DRIFT`** 事件（决策时间线）。
- 测试：`tests/test_trade_execution_service.py` 加"重启 → tracked 复原 → 平仓 PnL 用 tracked avg"用例；`tests/test_runner.py` 加 reconcile 漂移用例。

#### 验证

- [x] 单元：`test_trade_execution_service` / `test_runner`（load + drift 事件）。
- [ ] 端到端：`BUY 100@10` → 重启 → broker 改 `avg=11`（mock）→ `SELL 100@12`，PnL 仍按 10 算 ≈ 200。

### P5'：lifespan 非阻塞 + WS 重订 ✅（2026-05-25 部分完成）

> **目标：** FastAPI 启动不被券商网络阻塞；行情 WebSocket 断线能自愈。

#### 范围

- `backend/app/main.py:lifespan`：`await asyncio.to_thread(get_runner().start)`，捕获返回 False 时仅记 warning 不阻塞。
- `backend/app/core/broker.py`：
  - 利用 longport SDK 的 disconnect 回调（如有）；若无，新增"看门狗"：`AppRunner._run_loop` 已每 5s 跑，扩展为检测 `_last_quote_at` 超过阈值且 quote_ctx 仍声称 alive 时强制 `unsubscribe + subscribe`。
- 测试：`tests/test_runner.py` 用 fake broker 注入"60s 无 quote"场景，断言触发重订。

#### 验证

- [x] `asyncio.to_thread(runner.start/stop)`；`test_runner` 重订用例。
- [ ] 启动期 `/api/health` 在 1s 内返回（即便 broker 凭证为空）— 待手工压测。
- [ ] 模拟订阅丢失后，30s 内 quote 恢复推送 — 依赖 RTH + 90s 静默阈值。

### P6'：性能优化 ✅（2026-05-25 完成）

#### 范围

- `backend/app/api/trade.py:get_account` 用 `broker.quote([s for s in positions])` 一次取回所有报价。
- `backend/app/api/trade.py:get_orders` 默认从 DB 读 today 订单，仅当 `?refresh=1` 时触发 broker 同步；前端按钮触发 refresh。
- `backend/app/runner.py:_remember_quote` 给 `_recent_quotes` 加 `len ≤ 500` 硬上限。

#### 验证

- [x] `test_account_api` 批量 quote；`test_api` orders 默认本地；`test_runner` recent_quotes cap。

### P7'：IntervalApplicationService 对齐文档 ✅（2026-05-25 完成 — 方案 B）

- 决策：**保留现行追价/加仓逻辑**，更新 Roadmap 的"迭代 0 → 0.2 渐进式平滑过渡策略"小节，明确描述实际行为是"LLM 在持仓状态下可以下调 buy_low 追价加仓 / 上抬 sell_high 拉高目标价"。
- 行为对照表见上文 `迭代 0 / 0.2`。
- `tests/test_interval_application.py` 已包含此行为断言；后续可在该文件加注释链接本节作为权威说明。

### P8'：lint / 死代码清理 ✅（2026-05-25 部分完成）

- [ ] 修 `basedpyright` 3 处错误（用 `isinstance` 或显式 cast）— `llm_advisor` / `runner` 已加 coerce，待全量 pyright 清零。
- [x] 删除 `TradeExecutionService._wait_for_order_completion`。
- [x] 删除 `Settings.frontend_port`（未使用）。

---

## 后续迭代计划（2026-05-25 更新）

> 注：P2 已由 owner 决策排除。P4 是当前已确认的下一迭代，设计见 `docs/superpowers/specs/2026-05-25-trade-execution-safety-design.md`。

当前系统已经完成 LLM 自动决策上下文、主动价格刷新、订单同步、今日订单分页与撤单、决策时间线、Dashboard 图表化监控。后续计划按“先降低交易风险，再增强复盘能力，再提升运维体验”的顺序推进。

### P3：回测与参数验证 MVP ✅（已完成）

> **目标：** 在实盘继续运行前，用历史价格序列验证当前策略区间、最低盈利金额、止损/撤单规则的收益和风险。

#### 已交付

- 后端 `BacktestEngine` + `POST /api/backtest/run`（CSV：`timestamp,open,high,low,close,volume`）
- 前端 **Backtest** 页：CSV 上传/粘贴、参数表单、收益曲线、交易明细、手续费敏感性
- pytest + Cypress `backtest.cy.ts` 覆盖主流程

#### 后续可选增强

- Sharpe / 盈亏比等扩展指标（Roadmap 原列项，当前 UI 未展示）

### P4：交易执行安全与成本控制增强 ✅（2026-05-25 交付，提交范围 29b4890 .. 4a9f36f）

> **目标：** 进一步降低”无价值撤单重挂、重复 LLM 发单、手续费吞噬收益、跳过原因不可见”的风险。
>
> **规格文档：** `docs/superpowers/specs/2026-05-25-trade-execution-safety-design.md`。

#### P4 交付摘要

- **配置持久化（Task 1）**：`StrategyConfig` 新增 `fee_rate_us`、`fee_rate_hk`、`min_repricing_pct`、`llm_action_cooldown_seconds`；`_ensure_strategy_config_trade_safety_columns` 补旧表；API schema 与前端 Strategy 表单同步。
- **费用后收益门槛（Task 2）**：`_profit_guard_for_exit` 叠加 round-trip 费用估算；净收益不足时 `ORDER_SKIPPED` + `skip_category=”FEE”`；`allow_loss_exit=True` 绕过门槛。
- **LLM 改价与冷却 Gate（Task 3）**：`execute_llm_order_decision` 在撤单前执行改价阈值（`REPRICING`）与同方向冷却（`COOLDOWN`）检查；`_last_llm_action_at[(symbol, side)]` 仅在成交/提交后更新；`CANCEL_PENDING` 不受 gate 影响。
- **回测跳过原因分类（Task 4）**：`BacktestEngine` 跳过事件携带 `skip_category`，不读实盘 `fee_rate_us/hk`，离线模型保持独立。
- **Strategy 表单（Task 5）**：前端 Strategy 页面新增四个执行保护字段，含帮助提示与校验，`PUT /api/strategy` 持久化。
- **Decision Timeline + Dashboard 展示（Task 6）**：`skipCategoryLabel` 统一渲染六类跳过标签（FEE / REPRICING / COOLDOWN / RISK / PENDING / POSITION）；Dashboard 最近动作与 Timeline 详情同步展示。

#### 验证结果（本轮交付后）

- [x] `pytest 433 passed`（+16 项，相比 P4 前 417 项）
- [x] `basedpyright` 0 errors, 0 warnings
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过
- [x] 边界检查：`backtest.py` 中无 `fee_rate_us` / `fee_rate_hk` 引用（回测费率配置独立）

### P5+：操作审计 + 多渠道报警 + 交易可靠性补强 ✅（2026-05-28 交付）

> **目标：** 把运维行为、风险事件、关键交易动作完整审计化；支持 Server 酱 + Webhook 多渠道通知；补齐交易时段守卫与 Broker 调用重试。
>
> **规格文档：** `docs/superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md`
>
> **基线（交付后）：** `pytest 487 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — AuditLog 基础设施**：`audit_logs` 表 + `_ensure_audit_log_table` + `AuditLogger` 工具类（摘要截断/脱敏/异常吞掉不抛）+ DI helper `extract_actor` + `get_audit_logger`。9 个写端点（control ×5 / strategy / credentials / order cancel / kill-switch）全部接入审计写入。
- **T2 — 交易时段守卫（双层）**：`StrategyConfig.trading_session_mode`（默认 `ANY`，零行为变更上线）+ `AppRunner._check_trading_session`（撤单前 gate）+ `TradeExecutionService.execute` 二次 gate；`CANCEL_PENDING` 显式放行；`skip_category="SESSION"`。
- **T3 — Broker retry/backoff**：`BrokerGateway._call_with_retry` 分档（订单 `broker_retry_max=3` / 行情 `broker_quote_retry_max=1`）+ 指数退避；优先结构化异常，否则降级字符串匹配（`限流`/`频率`/`timeout`）；每次重试写 `BROKER_RETRY` 审计。
- **T4 — Notifier 抽象 + Webhook + 分级**：`NotifierInterface` Protocol + `MultiChannelNotifier` fan-out（按 `severity_floor` 过滤）+ `WebhookNotifier`（JSON payload）+ `ServerChanNotifier` 迁入。`KILL_SWITCH` endpoint 补齐 `notify_risk_event("KILL_SWITCH", ..., severity="CRITICAL")`。`CredentialConfig.notification_channels` 持久化。
- **T5 — 前端集成**：Strategy 表单加 `trading_session_mode` 单选（ANY/RTH_ONLY）+ 不含节假日提示；Credentials 增加通知渠道列表（Server酱/Webhook+severity_floor）；Decision Timeline 支持 `source=trade|audit|all` 切换 + 多选 event_type 过滤 + 审计卡片（severity/actor_hash/source_ip）；Dashboard skipCategoryLabel 增加 `SESSION` 标签。
- **T6 — 测试与 lint 清零**：pytest 新增 ~52 项（总计 487）；Cypress 新增 3 个 spec（`credentials_notifications`、`decision_timeline_audit`、`strategy_session_guard`），共 14 个 E2E spec；`basedpyright` 0 errors。

#### 验证

- [x] `pytest 487 passed`
- [x] `basedpyright` 0 errors / 0 warnings
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过
- [x] Cypress 新增 3 个 spec 均通过

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

### P7：策略复盘与 LLM 优化工作台 🚧（当前迭代）

> **目标：** 利用已沉淀的 LLM 交互、状态快照、订单事件，按”交易日 × 当前 symbol”复盘，反哺 prompt 调优。
>
> **规格：** `docs/superpowers/specs/2026-05-26-replay-llm-workshop-design.md`

#### 范围

- 新增”Replay”页面：按交易日展示 K 线走势 + LLM 建议 × 实际成交 × 真实 PnL。
- 关联键：`LLMInteraction.order_id` ↔ `OrderRecord.broker_order_id`，**不增表、不加 FK**，查询时内存 JOIN。
- 5 个错误标签按优先级评估：`MISSED_STOP` / `PREMATURE_ENTRY` / `FREQUENT_REPRICE` / `LOW_PROFIT` / `NORMAL`；阈值为代码常量。
- 价格曲线优先来自 `BrokerGateway.get_candlesticks`，broker 不可用 / 历史超出保留时回退 `RuntimeStateSnapshot`。
- 导出 JSON（细粒度，完整 prompt + 关联订单）与 CSV（扁平表格），浏览器原生下载。
- `realized_pnl` 复用 `DailyPnlService.calculate` 重算，不修改写路径。
- `list_days` 固定按 `orders` / `llm_interactions` / `runtime_state_snapshots` 三类数据并集构造（market-aware trade day），仅返回有数据日，不补齐空白日历日。
- API 语义固定：参数非法返回 `422`；参数合法即使空数据也返回 `200`（空数组结构），不使用 `404` 表示“合法但无数据”。

#### 显式不做

- 多标的复盘（待 P8 Watchlist）；标签阈值可配置；历史 `StrategyConfig` 快照；反向 FK 字段。

#### 验证

- 后端 pytest ~25 新增（聚合主路径 / trip 划分 / 5 个标签 / 三端点 / 导出格式 / 合法空数据返回 200）。
- Cypress `replay.cy.ts` 覆盖列表、切日、导出、标签视觉。
- `basedpyright` 0/0；`npm run type-check` + `build`。

### P8：多标的观察列表（暂不自动交易）

> **目标：** 先支持多个标的的行情和 LLM 观察，暂不允许多标的自动下单，避免交易风险突然扩大。

#### 范围

- 支持 Watchlist：多个 symbol 的价格、波动、LLM 建议、风险摘要。
- 每次只允许一个“交易标的”处于自动交易状态。
- Dashboard 增加观察列表表格，不改现有单标的交易引擎。

#### 验证

- 后端测试覆盖观察列表 CRUD 和行情聚合。
- Cypress 覆盖添加/删除观察标的、切换交易标的前的确认流程。

### P9：LLM Prompt Engineering Optimization ✅（2026-05-29 交付）

> **目标：** 通过模块化 prompt 架构、技术指标扩展和 A/B 测试支持，提升 LLM 交易顾问的决策质量和系统性能。
>
> **规格文档：** `docs/superpowers/specs/2026-05-28-llm-prompt-engineering-optimization-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-28-llm-prompt-optimization.md`
>
> **基线（交付后）：** `pytest 549 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### Phase 1 交付摘要

- **T1-T5 — 模块化 Prompt 架构**：`PromptModule` 抽象基类 + `SystemModule`（角色/规则）+ `ContextModule`（K 线/指标/情绪）+ `StrategyModule`（持仓/风控）+ `OutputModule`（JSON 格式）+ `PromptBuilder` 编排器。`DataAggregator.build_prompt()` 重构为模块化组合。
- **T6 — 技术指标扩展**：RSI(14)、MACD(12,26,9)、成交量分析（均量/量比/趋势）集成到 `ContextModule`。
- **T7 — DataAggregator 集成**：`fetch_market_data()` 返回扩展指标数据，`PromptBuilder` 自动渲染。
- **T8 — 数据库模型**：`PromptVersion` + `ExperimentResult` 表 + `_ensure_*` 迁移补丁。
- **T9 — ABTestManager**：prompt 变体选择 + 交互结果记录 + 胜率计算。
- **T10 — Experiments API**：`/api/experiments` CRUD + 结果查询。
- **T11 — LLMAdvisorService 集成**：使用 `PromptBuilder`，支持实验变体选择。

#### Phase 2 交付摘要

- **T13 — 市场情绪模块**：`SentimentAnalyzer` 分析价格动量、波动率、成交量异常，输出情绪评分（bearish/neutral/bullish）。
- **T14 — 情绪集成**：`ContextModule` 渲染情绪分析结果到 prompt。
- **T15 — 多时间框架分析**：日 K + 周 K 趋势对齐检测，输出 alignment 信号。
- **T16 — 性能追踪器**：`PerformanceTracker` 记录 LLM 建议 vs 实际结果，计算准确率/收益/回撤；`/api/performance` 查询 API。

#### 验证结果（本轮交付后）

- [x] `pytest 549 passed`（+56 项，相比 P9 前 493 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（3.30s）

### P10：LLM 特征工程扩展 — 技术指标深度优化 ✅（2026-05-29 交付）

> **目标：** 扩展经典技术指标覆盖，为 LLM 提供更全面的市场分析维度。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-feature-engineering-expansion-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-feature-engineering-expansion.md`
>
> **基线（交付后）：** `pytest 586 passed, 1 skipped`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — OBV（能量潮）**：量价背离检测，输出 OBV 序列 + 趋势 + 价格-OBV 背离信号。
- **T2 — ADX（平均趋向指数）**：趋势强度判断，输出 ADX 值 + 趋势强度分类 + DI+/DI-。
- **T3 — Stochastic（随机指标）**：超买超卖检测，输出 %K、%D、信号。
- **T4 — CCI（商品通道指数）**：价格偏离度识别，输出 CCI 值 + 信号。
- **T5 — Williams %R**：超买超卖检测（更敏感），输出 %R 值 + 信号。
- **T6 — VWAP（成交量加权平均价）**：机构成本参考，输出 VWAP 值 + 价格相对位置。
- **T7 — aggregate_signals()**：综合 7 个指标信号，输出 overall_signal + confidence + summary。
- **T8 — DataAggregator 集成**：`fetch_market_data()` 返回所有新指标数据。
- **T9 — ContextModule 渲染**：LLM prompt 新增"技术指标扩展"区块。
- **T10 — 最终验证**：pytest 586 passed, 1 skipped，basedpyright 0 errors，前端构建通过。

#### 验证结果（本轮交付后）

- [x] `pytest 586 passed, 1 skipped`（+38 项，相比 P9 前 549 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（3.64s）

### P11：LLM 自适应特征选择 ✅（2026-05-29 交付）

> **目标：** 实现 LLM 自主特征选择，根据市场状态自动选择最相关的技术指标，减少无关指标干扰，提升决策质量。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-adaptive-feature-selection-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-adaptive-feature-selection.md`
>
> **基线（交付后）：** `pytest 607 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — MarketStateDetector**：基于 ADX/BB/ATR/Volume 检测市场状态（trending/ranging/volatile/neutral），输出状态 + 置信度 + 推荐指标。
- **T2 — SelectionModule**：渲染市场状态和可用指标列表，引导 LLM 选择 3-5 个最相关指标。
- **T3 — FeatureSelector**：解析 LLM 返回的 JSON 指标选择，过滤上下文只保留选中指标。
- **T4 — DataAggregator 集成**：`fetch_market_data()` 返回市场状态数据。
- **T5 — ContextModule 过滤**：根据 `selected_indicators` 只渲染选中指标。
- **T6 — LLMAdvisorService 集成**：prompt 中加入 SelectionModule，LLM 分析时考虑指标选择。
- **T7 — 最终验证**：pytest 607 passed，basedpyright 0 errors，前端构建通过。

#### 验证结果（本轮交付后）

- [x] `pytest 607 passed`（+20 项，相比 P10 前 587 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（4.50s）
### P12：LLM 优化工作台前端化 ✅（2026-05-29 交付）

> **目标：** 将 P9 后端已有的实验管理、性能追踪、技术指标等 API 暴露为可用的前端 Lab 页面，供用户直接查看和管理 Prompt 优化工作台。
>
> **说明：** 本迭代与远端独立推进的 P10（特征工程扩展）/P11（自适应特征选择）并行开发，集成时重新编号为 **P12** 以避免与已交付的 P10 冲突。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-lab-frontend-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-lab-frontend.md`
>
> **基线（集成后）：** rebase 到 origin/main（含 P10/P11）之上，全量 `pytest 621 passed`（P11 的 607 项 + 本迭代新增），`npm run type-check` + `npm run build` 通过，Cypress `lab.cy.ts` 4/4 通过。

#### 交付摘要

- **后端 3 个只读端点**：
  - `GET /api/experiments`（列出实验名称，复用 P9 `ExperimentResult` 表）
  - `GET /api/indicators?symbol=`（实时技术指标快照：ATR、RSI、MACD、布林带、成交量、情绪、多时间框架；broker 缺失时 `available=false`）
  - `GET /api/performance/{stats,compare,recommendations}?experiment=`（A/B 性能统计，补充响应模型 schema）
- **前端 Lab 页（`/#/lab`）**：`Lab.vue` 三页签：
  - **实验与版本**：Prompt 版本表格（`PromptVersion` 列表、激活操作）+ 版本创建表单 + 实验摘要选择查看
  - **性能看板**：选择实验后展示 A/B 汇总统计（总交易/胜率/总 PnL/均 PnL）+ 变体对比表 + 优化建议列表
  - **指标面板**：输入标的后查询实时技术指标，`available=false` 时显示"行情不可用"水印，否则渲染 6 张指标卡片
- **Cypress E2E**：新增 `frontend/cypress/e2e/lab.cy.ts`，4 个测试用例覆盖三页签渲染、性能加载、指标不可用/可用

#### 验证结果

- [x] `pytest 621 passed`（rebase 到 P11 的 607 项之上，本迭代新增端点测试全绿；并修复一个因时区/午夜边界导致的 `test_orders_default_returns_local_today_orders_with_pagination` 偶现失败）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（7.30s）
- [x] `Cypress lab.cy.ts` 4/4 通过（本地 Vite dev server `CYPRESS_BASE_URL=http://localhost:3001`）

### 建议执行顺序

| 顺序 | 迭代 | 状态 | 原因 |
|------|------|------|------|
| 已完成 | P3 回测与参数验证 MVP | ✅ 已交付 | 已为实盘调参提供历史验证基础。 |
| 已完成 | **P4 交易执行安全与成本控制增强** | ✅ 2026-05-25 | 直接回应手续费、重复 LLM 动作和无价值撤单重挂风险。 |
| 已完成 | **P5+ 操作审计 + 多渠道报警 + 交易可靠性补强** | ✅ 2026-05-28 | 合并 Roadmap 原 P5 与审计遗留 #9/#10；487 项 pytest + 14 Cypress spec 全绿。 |
| 已完成 | **P6 移动端与应急操作体验** | ✅ 2026-05-28 | 底部 Tab 导航 + Dashboard 图表折叠 + 表单单列布局；15 个文件改动。 |
| 已完成 | **P7 策略复盘与 LLM 优化工作台** | ✅ 2026-05-28 | 新增 ReviewService + /api/review/export + Review.vue；pytest 493 passed。 |
| 已完成 | **P8 多标的观察列表** | ✅ 2026-05-28 | WatchlistItem 模型 + CRUD API + 行情聚合 + Watchlist.vue；pytest 11 passed。 |
| 已完成 | **P9 LLM Prompt Engineering Optimization** | ✅ 2026-05-29 | 模块化 Prompt 架构 + 技术指标（RSI/MACD/Volume）+ A/B 测试 + 市场情绪 + 多时间框架 + 性能追踪；pytest 549 passed。 |
| 已完成 | **P10 LLM 特征工程扩展** | ✅ 2026-05-29 | 新增 OBV/ADX/Stochastic/CCI/Williams %R/VWAP 六个技术指标 + aggregate_signals() 综合信号；pytest 587 passed。 |
| 已完成 | **P11 LLM 自适应特征选择** | ✅ 2026-05-29 | MarketStateDetector + SelectionModule + FeatureSelector，LLM 基于市场状态自主选择指标；pytest 607 passed。 |
| 已完成 | **P12 LLM 优化工作台前端化** | ✅ 2026-05-29 | 暴露 P9 后端能力：3 个只读端点 + Lab 三页签前端；Cypress 4/4。 |
| 已完成 | **P13 加仓 + 成本锚定 LLM** | ✅ 2026-05-31 | Engine LONG→BUY 加仓 + ContextModule 持仓成本区块 + 冷却/优先级集成；pytest +8，basedpyright 0/0。 |
| 已完成 | **P14 保证金下单量** | ✅ 2026-05-31 | margin_safety_factor 配置化 + BrokerGateway margin 路径验证；pytest 628 passed，frontend type-check + build 通过。 |
| 已完成 | **P15 Dashboard & 配置性能优化** | ✅ 2026-05-31 | `/api/account` 短 TTL 缓存 + Dashboard 分区加载 + 配置页初始 loading guard；pytest 633 passed / basedpyright 0/0 / frontend build / 新增 Cypress 4 项通过。 |
| 已完成 | **P16 策略实验平台 Phase 1：批量回测 + 排行榜** | ✅ 2026-05-31 | 新增 `/api/strategy-experiments`、参数网格服务、批量回测持久化、Experiments 页面；pytest 678 passed / basedpyright 0/0 / frontend type-check + build / Cypress 77 passed。 |
| 已完成 | **P17 策略实验平台 Phase 2：LLM 评分 + 导出 + Strategy 草稿带回** | ✅ 2026-05-31 | `LLMRecommendationEvaluator`（6 类标签：EFFECTIVE/INEFFECTIVE/TOO_EARLY/TOO_LATE/RISKY/INSUFFICIENT_DATA）+ `GET /api/strategy-experiments/llm-evaluations`；实验 CSV/JSON 导出 + `GET /api/strategy-experiments/{id}/export`；Strategy 草稿带回（`/#/strategy?draftExperimentRunId=xxx`）；前端 Experiments 页面扩展；pytest +12，Cypress +3，frontend type-check + build 通过。 |
| 已完成 | **P18 技术债清理：basedpyright 错误清零** | ✅ 2026-05-31 | 修复 app/ 42 处类型错误（dict/Callable/Generator 泛型补齐、Optional 访问保护、常量重定义消除）；修复 tests/ 约 120 处类型错误（MissingTypeArgument、OptionalMemberAccess、Generator 返回类型等）；pytest 691 passed / basedpyright 0 errors / frontend build / Cypress 80 passed。 |
| 已完成 | **P19 A/B Testing 集成：LLM Prompt 变体实验** | ✅ 2026-05-31 | `LLMInteraction` 增 `prompt_variant` 字段 + `_ensure_llm_interaction_variant_column` 迁移；`Settings.llm_experiment_name` 配置；`LLMAdvisorService._select_variant` 确定性按 symbol hash 分配变体；`_build_prompt` 支持自定义 template；`analyze`/`preview` 全流程透传变体标识并写入 interaction 日志；pytest 696 passed / basedpyright 0 errors。 |

### 下一步建议

**P19 A/B Testing 集成已完成交付。** 后续建议推进 P20（多标的自动交易扩展）或 P21（实时推送通知），视业务优先级而定。
---

## 原始规划记录（已交付部分保留作为历史）

以下章节记录最初提出的 5 个主题：**LLM智能区间调整**、**回测系统**、**实时图表与监控增强**、**日志审计与报警**、**响应式与移动端适配**。其中 LLM、回测与实时图表已经交付；当前执行优先级以本文上方 2026-05-25 更新为准，即先实施 **P4 交易执行安全与成本控制增强**。

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

## 原始迭代交付状态摘要

| 迭代 | 主题 | 当前状态 | 备注 |
|------|------|------|------|
| 0 | LLM 智能区间调整 | ✅ 已交付并经 2026-05-25 数据真实性修正 | 保留追价加仓规则 |
| 1 | 回测系统 | ✅ 已交付 | P4 仅补跳过分类 |
| 2 | 实时图表与监控增强 | ✅ 已交付 | Dashboard 已可用 |
| 3 | 日志审计与报警 | 待后续实施 | 排在 P4 之后 |
| 4 | 响应式与移动端适配 | 待后续实施 | 排在 P4 之后 |

---

## 实施建议

1. **一次只做一条主线。** 每个迭代内可以并行开发互不影响的子任务（如前后端可并行），但不要在同一个代码基上交叉进行两个迭代的开发，避免功能碎片化。
2. **每个迭代产出物：**
   - 一个保存在 `docs/superpowers/plans/` 的实施计划文件（可参考已有格式）。
   - 完成后更新此 Roadmap，标记对应迭代完成状态。
3. **P4 交易执行安全与成本控制是当前最高优先级**，因为回测和 Dashboard 已交付，下一风险集中在真实下单的费用、LLM 替换挂单与可解释性。
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
