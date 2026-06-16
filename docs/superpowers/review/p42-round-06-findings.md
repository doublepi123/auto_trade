# P42 Round 6 — 第二轮 review（多 symbol + 性能）

> 来源：2 个 Explore subagent 并行扫描。Round 1 已覆盖 backend-correctness / resilience / frontend-ux / test-coverage / security-perf 五个角度，本轮聚焦 Round 1 没深挖的两个维度。
>
> 工作区：`/home/lcy/code/auto_trade-p42`

## 维度 A：多 symbol 状态机正确性（15 条 finding，已去重）

### P0（多 symbol 架构核心风险）

| # | File | Line | 摘要 |
|---|------|------|------|
| 1 | `backend/app/runner.py` | 930 | `execute_llm_trade_action` 用 `self.engine.params.min_profit_amount`（主标的）评估 secondary symbol SELL，忽略 per-symbol params |
| 2 | `backend/app/runner.py` | 1271 | `_sync_risk_from_order_ledger` 用未 filter 的 PnL 更新全局风控，symbol B 亏损导致 symbol A 被 pause |
| 3 | `backend/app/services/daily_pnl_service.py` | 87 | `calculate()` symbol filter 在 legacy `symbol=''` rows 上漏算（PnL 欠报） |
| 4 | `backend/app/services/trade_execution_service.py` | 238 | `pending_order_for(symbol)` case-sensitivity 不一致；broker symbol 大写、用户输入小写时漏匹配 |

### P1

| # | File | Line | 摘要 |
|---|------|------|------|
| 5 | `backend/app/services/trade_execution_service.py` | 253 | `_pending_order` setter 删除"first"pending 是 dict 迭代序，不适合多 pending 场景 |
| 6 | `backend/app/runner.py` | 641 | `_execute_triggered_order` 闭包绑 `trigger_engine`，但 `decision.trigger_symbol` 与 `quote.symbol` 不一致时回退到错误 engine |
| 7 | `backend/app/services/runtime_state_service.py` | 114 | `persist_symbol` 在 `risk is None` 时 new 一个空 RiskController，覆盖真实 risk 快照 |
| 8 | `backend/app/services/runtime_state_service.py` | 76 | `persist_risk()` 单行按 symbol 写入，但 `_sync_risk_from_order_ledger` 用 primary symbol 写入；多 symbol PnL 聚合会覆盖 |
| 9 | `backend/app/runner.py` | 138 | `_mark_fill_processed` 在 `symbol=''` 时回退到 primary engine，secondary fill 错记 |
| 10 | `backend/app/runner.py` | 1893 | 重启时 `_load_pending_orders` 不预注册 secondary runtime，pending 命中无 runtime 的 symbol 时静默 early-return |
| 11 | `backend/app/services/trade_execution_service.py` | 145 | `load_tracked_entries` 先清后填，中间窗口并发可空 `_entry_positions` |

### P2

| # | File | Line | 摘要 |
|---|------|------|------|
| 12 | `backend/app/runner.py` | 1521 | snapshot copy 与迭代期间 runtime 被移除时仍写入陈旧 state |
| 13 | `backend/app/runner.py` | 1656 | `_auto_resume_pause_if_due` 只看 self.risk.paused（global） |
| 14 | `backend/app/runner.py` | 2032 | `reload_strategy` 更新 self.engine 但不传播到 secondary runtimes |
| 15 | `backend/app/services/trade_execution_service.py` | 248 | `_pending_order` setter 单条删除语义不清 |

## 维度 B：后端 perf hot path（15 条 finding，已去重）

### P0

| # | File | Line | 摘要 |
|---|------|------|------|
| 16 | `backend/app/services/report_service.py` | 199 | `_build_report` day-by-day N+1 `pnl_service.calculate` |
| 17 | `backend/app/services/daily_pnl_service.py` | 86 | `filled_at/created_at` OR 表达式用不上复合索引 |
| 18 | `backend/app/api/trade.py` | 514 | `_fetch_account_response` 串行 broker 调用（get_account → get_positions → get_quotes） |
| 19 | `backend/app/services/event_list_service.py` | 180 | 跨表 union 每次 page 都跑两次 `.count()` 全表扫描 |

### P1

| # | File | Line | 摘要 |
|---|------|------|------|
| 20 | `backend/app/services/data_aggregator.py` | 38 | `fetch_market_data` 3 个串行 broker 调用 |
| 21 | `backend/app/runner.py` | 1049 | `_remember_quote` 高频 + `recent_price_context` O(n) 线性扫 |
| 22 | `backend/app/services/review_service.py` | 34 | 4 个独立无 LIMIT 查询 |
| 23 | `backend/app/services/review_service.py` | 339 | `_compute_error_tags` N×M 嵌套循环 + 每行 re-parse JSON |

### P2

| # | File | Line | 摘要 |
|---|------|------|------|
| 24 | `backend/app/api/trade.py` | 230 | `get_orders` count + offset 都全排序扫描 |
| 25 | `backend/app/services/event_list_service.py` | 99 | `func.json_extract` 强制 SQLite 解析每行 JSON |
| 26 | `backend/app/services/data_aggregator.py` | 55 | 4 个独立 list comp over same candle data |
| 27 | `backend/app/runner.py` | 130 | global `_recent_quotes` 在多 symbol 下过早淘汰 |
| 28 | `backend/app/services/daily_pnl_service.py` | 102 | Python 侧 latest_orders 去重 |
| 29 | `backend/app/core/broker.py` | 471 | `_get_quotes_inner` 持锁跨整个网络往返 |

## 与 Round 1 去重

- `#16 report N+1` 在 Round 1 P1 #39 已记。
- `#1 runner min_profit_amount` 在 Round 1 P1 #15（trade_execution_service:820）已记。**Round 6 给了准确的 file:line 修正**——`runner.py:820` vs `runner.py:930`。两者可能是同 bug 的不同观察点。
- `#2 sync_risk_from_order_ledger` 是新 P0。
- `#3 daily_pnl symbol filter` 是新 P0。
- `#4 pending_order_for case-sensitivity` 是新 P0。
- 其他大多数是 Round 1 没覆盖的精确位置。

## 后续轮次建议

- **Round 7–8**: 修 P0 #1–4（多 symbol 架构）。这些需要在 `_symbol_runtimes` 现有架构上加 symbol-aware 切换，改动较大。
- **Round 9**: 修 P0 #16（report N+1）—— 单文件、可独立 ship。
- **Round 10**: 修 P0 #17（daily_pnl 索引）—— 加 DB 索引，风险低。
- **Round 11**: 修 P0 #18（account 串行 → 并行）—— 中等改动。
- **Round 12**: 修 P0 #19（events count 缓存）—— 加 TTL cache。

## 显式不做（与已有 YAGNI 一致）

- 不做 SQLAlchemy → SQL 重写（已 SQLAlchemy ORM 风格）。
- 不动 broker SDK 接口（仅调用层优化）。
- 不引入新依赖（httpx / 第三方 cache 库）。
