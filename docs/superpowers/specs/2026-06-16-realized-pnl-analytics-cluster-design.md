# 已实现盈亏分析簇 + 告警触发历史（5 轮自主迭代）

> 用户指令："自主进行 5 轮功能迭代"。基线 `pytest 1091 passed, 2 failed`（pre-existing `test_watchlist_score`）。
> 选型：`/select-5-iterations` workflow（4 个只读探索 agent + judge panel 打分 trader_value × eng_fit × novelty）。规格即本文件。

## 选型与偏离记录

judge panel 推荐 top-5 为：① 往返成交账本 ② 交易统计+连胜连亏 ③ 权益曲线 ④ 按标的归因 ⑤ 净/毛 PnL 切换。本批做两处**降低风险**的偏离：

1. **① 改为只读、不落库**。judge 原方案是"在 fill 时由 DailyPnlService 写入 `closed_trades` 透写缓存"。但那会触碰 fill/写入路径（贴近实盘）并引入缓存一致性负担。本仓库既定约定是**读时重算**（`DailyPnlService.calculate`、`/api/metrics/summary` 每次请求都 replay fills，几百笔 fill 是微秒级）。落库属于过早优化 + 写路径风险。故改为**纯只读 FIFO 配对服务**，同等价值、零实盘风险，且成为 ②③④ 的共享基础。
2. **⑤ 净/毛切换 → 告警触发历史**。净/毛需改 `ReportService` + `metrics.py` + trades 三处（爆破半径大），而净 PnL 能力可天然内建进新的配对服务（带 `est_fees`/`net_pnl`）。告警触发历史（judge rank 6，total 23 > 净/毛 22，novelty 8）自包含、零实盘风险、append-only 写在告警 cron 内（非下单路径），独立可 ship。

最终 5 轮（基础先行）：**① 往返成交 → ② 交易统计+连胜连亏 → ③ 权益曲线 → ④ 按标的归因 → ⑤ 告警触发历史**。

## 共享基础：`DailyPnlService.pair_round_trips()`

`DailyPnlService` 已拥有 FIFO 分桶账本（`_LedgerPosition`/`_apply_fill`）与 fill 抽取（`_fill_from_order`）。新增**只读方法** `pair_round_trips(symbol?, from_dt?, to_dt?, fee_rate_us, fee_rate_hk)`：

- 复用 `_fill_from_order` 抽取 fill（含 broker_order_id 去重、FILLED/PARTIAL_FILLED、executed 价格回退）。
- 独立 entry-lot FIFO 循环（**不触碰** `calculate()` / `_apply_fill`，零风控回归）：BUY→入 long lot 队；SELL→FIFO 消费 long lots；SELL_SHORT→入 short lot 队；BUY_TO_COVER→FIFO 消费 short lots。
- 每笔**平仓 fill** 产出一个 `ClosedRoundTrip`（聚合其匹配的 entry lots）：`side(long/short)`、`entry_order_id`、`exit_order_id`、`entry_at`（首匹配 lot）、`exit_at`、`entry_price`（加权均价）、`exit_price`、`quantity`、`gross_pnl`、`est_fees`、`net_pnl`、`holding_seconds`。
- `gross_pnl` 数学与 `_close_long`/`_close_short` 一致（long: `(exit-entry)*qty`；short: `(entry-exit)*qty`）。
- `est_fees = estimate_round_trip_fee(...)`（复用 `app/core/fees.py`），费率按标的后缀 `.HK` 取 `fee_rate_hk` 否则 `fee_rate_us`（读自活跃 `StrategyConfig`，历史交易用当前配置费率，文档标注为估算）。
- 日期过滤语义：过滤**平仓时间** `exit_at ∈ [from_dt, to_dt]`；entry 可早于窗口（正确归因窗口内平仓的往返）。

**不落库、不写审计、不下单。** ②③④ 全部在此方法返回的 `list[ClosedRoundTrip]` 上做纯聚合。

---

## ① 往返成交（Closed Round-Trip Trades）— `GET /api/trades`

- `GET /api/trades?symbol=&from_date=&to_date=&limit=` → `ClosedTradePage{items,total}`。
- 新 `app/api/trades.py`（prefix `/api/trades`，无冲突）。
- schema `ClosedTrade` / `ClosedTradePage`。
- 前端：TradeHistory.vue 增「已实现成交（往返）」区/标签 + `api/trades.ts` + 类型 + Cypress。

**不重复**：`/api/metrics/summary` 是粗粒度 SELL→最近 BUY 代理（仅 FILLED、无费、无 streak、无逐笔行）；`/api/reports` 是单标的、按 side 归因、日报；`DailyPnlService.calculate` 是单日。本端是首个股级 FIFO 往返（entry↔exit 配对 + 持仓时长 + 净/毛）。

## ② 交易统计 + 连胜连亏（Trade Stats + Streaks）— `GET /api/trades/stats`

- 在 ① 的 `ClosedRoundTrip` 列表上纯聚合：`total_trades/win_count/loss_count/win_rate/avg_win/avg_loss/expectancy(avg_pnl)/profit_factor/payoff_ratio/largest_win/largest_loss/current_streak{type,count}/max_win_streak/max_loss_streak/avg_hold_seconds`。
- `GET /api/trades/stats?days=&symbol=`。
- 前端：TradeHistory 统计条 + Dashboard 指标区扩展。

**不重复** `/api/metrics/summary`：后者无 streak / expectancy / 持仓时长 / 逐笔下钻。

## ③ 权益曲线（Cumulative Realized PnL Curve）— `GET /api/equity/curve`

- 把 ① 的 `ClosedRoundTrip` 按平仓日分桶：每日 `realized_pnl`、累计 `cumulative_pnl`、运行最大回撤 `drawdown`。
- `GET /api/equity/curve?from_date=&to_date=` → `EquityCurvePoint[]`。
- 前端：Dashboard 新 `EquityCurvePanel`（常驻，SVG 折线 + 回撤）。

**不重复**：Dashboard PnLChart 仅日内 session（~200 点）；`/api/reports/range` 的 `cumulative_pnl` 是**单标的**且仅在 Reports 内；本端是**账户级（全标的）**常驻曲线。

## ④ 按标的归因（Per-Symbol PnL Attribution）— `GET /api/pnl/by-symbol`

- 把 ① 按标的分组：`realized_pnl/trade_count/win_rate/contribution_share/largest_win/largest_loss`。
- `GET /api/pnl/by-symbol?from_date=&to_date=` → `SymbolAttributionRow[]`。
- 前端：Dashboard/Reports 新 `SymbolAttributionPanel`。

**不重复**：`ReportResponse.attribution` 按 **side**（单标的）；`/api/positions/pnl` 仅浮盈。本端是组合级、symbol 维度。

## ⑤ 告警触发历史（Alert Firing History）— `alert_firings` 表

- 新表 `alert_firings(id, rule_id, symbol, rule_type, threshold, trigger_value, severity, message, fired_at)` + `_ensure_alert_firings_table`。
- 在既有 `AlertRuleService.evaluate()` 内，`rule.last_fired_at = now` 处**同步 append 一行**（同一事务，best-effort 不阻断通知）。
- `GET /api/alert-rules/{id}/history?from_date=&to_date=&limit=` → `AlertFiringPage`；`GET /api/alert-firings?rule_id=`。
- 前端：AlertRules.vue 每规则触发历史 + 计数徽标。

**不重复**：`AlertRule` 仅存 `last_fired_at`（覆盖式）；`/api/notifications` 无 `rule_id` 外键，无法回溯到规则。本端是新可观测性表 + 只读时间线，零下单路径改动。

---

## 验收口径（每轮）

- `pytest tests/ -q` 全绿（基线 1091 passed；新增失败 = 回归）。
- `basedpyright` 0 errors / 0 warnings / 0 notes（`--pythonpath .venv/bin/python`）。
- `npm run type-check` 0 errors；`npm run build` 通过（chunk 预算不退步）。
- 涉及 DB 的轮次：旧库 `_ensure_*` 兼容迁移存在。
- Cypress spec 编写（本机无法 headless 运行，仅类型/构建校验，如实标注）。
