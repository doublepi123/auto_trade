# P42 Round 1 — 全项目 review（去重 + 排序）

> 来源：5 个 Explore subagent 并行扫描（backend-correctness / backend-resilience / frontend-ux / test-coverage-and-flakiness / security-and-performance），共约 100 条 finding，已按 (file, line) 去重，按严重度 + 修复成本排序。
>
> 工作区：`/home/lcy/code/auto_trade-p42`（worktree `feature/p42-autonomous-20-round`）

## P0（必须修 / 跨 sprint 累积风险）

| # | File | Line | 摘要 | 来源 |
|---|------|------|------|------|
| 1 | `backend/app/core/broker.py` | 565 / 587 | `submit_limit_order` 不在 `_call_with_retry` 包装内，与其他 broker 调用风格不一致 | correctness + resilience |
| 2 | `backend/app/services/trade_execution_service.py` | 640 | `_execute_sell` 在 broker 返回同一 symbol 多个 LONG position 时静默跳过 | correctness |
| 3 | `backend/app/services/trade_execution_service.py` | 1252 | `_recover_from_missing_order_record` 撤单后未复核订单终态，可能把已成交误判 | resilience |
| 4 | `backend/app/services/trade_execution_service.py` | 1201 | `_handle_terminal_fill_result` 构造 `_PendingOrder` 时 `submitted_at=0.0`，timeout 监控可对刚成交的订单误触发 | correctness |
| 5 | `backend/app/runner.py` | 1114 | `_resubscribe_quotes_if_silent` 用主标的 market 守门，非 RTH 时其他市场 symbol 不重订 | correctness |
| 6 | `backend/app/services/trade_execution_service.py` | 1425 | `_persist_entry_safe` 静默吞所有异常，tracked_entries 与 broker 可能持续不一致 | resilience |
| 7 | `backend/app/api/llm_advisor.py` | 203 | `analyze` 触发 `execute_llm_order_decision` 无额外 rate limit | security |
| 8 | `backend/app/api/trade.py` | 436 | `cancel order` 不校验 order_id 的 symbol/owner 归属 | security |
| 9 | `backend/app/services/llm_advisor_service.py` | 662 | DeepSeek HTTP 错误日志可能回显 Authorization header | security |
| 10 | `backend/app/api/auth.py` | 37 | API key 失败日志打印 `request.client` IP，可被用于日志侧信道 | security |
| 11 | `backend/app/services/trade_event_service.py` | 1 | `trade_event_service.py` **无直接测试**（事件记录 + 序列化 + 解码 fallback） | test-coverage |

## P1（应修 / 单一模块 / 可独立 ship）

| # | File | Line | 摘要 | 来源 |
|---|------|------|------|------|
| 12 | `backend/app/core/risk.py` | 91 | `daily_pnl <= -max_daily_loss` 与 `_maybe_rollover_day` 重置顺序在某些时序下放行已超限订单 | correctness |
| 13 | `backend/app/services/daily_pnl_service.py` | 123 | `consecutive_losses` 在 matched_quantity<=0 时仍滚动 | correctness |
| 14 | `backend/app/runner.py` | 696 | `_trigger_in_flight` 是全局 bool，symbol A 的 pending 阻塞 symbol B 的 quote 触发 | correctness |
| 15 | `backend/app/runner.py` | 820 | `execute_llm_order_decision` 在非主标的用 `self.engine.params.min_profit_amount`，忽略 per-symbol params | correctness |
| 16 | `backend/app/runner.py` | 745 | `_runtime_for_symbol` 把 whitespace 路由到 primary | correctness |
| 17 | `backend/app/core/broker.py` | 716 | `get_positions` 对 SHORT 仓的 `available_quantity` 取 abs，符号错误 | correctness |
| 18 | `backend/app/services/trade_execution_service.py` | 1432 | `_resolve_avg_price_for_exit` 未校验 broker avg_price 的 side 符号 | correctness |
| 19 | `backend/app/services/llm_advisor_service.py` | 360 / 456 | `analyze` 与 `_record_analysis` 缺事务边界与失败回传 | resilience |
| 20 | `backend/app/services/llm_advisor_service.py` | 842 | `_record_interaction` 静默吞所有异常 | resilience |
| 21 | `backend/app/services/llm_advisor_service.py` | 667 | DeepSeek 响应 dict.get 链无防御，KeyError 暴露 | resilience |
| 22 | `backend/app/services/watchlist_score_service.py` | 200 | 调用 `advisor._call_deepseek` 私有方法 | resilience |
| 23 | `backend/app/services/watchlist_score_service.py` | 1 | 无专用 test 文件，依赖 test_watchlist_score 间接覆盖 | test-coverage |
| 24 | `backend/tests/test_watchlist.py` | 14 | `clean_db` fixture 不清 RuntimeState/LLMInteraction/Snapshot，会跨测污染 | test-coverage |
| 25 | `backend/tests/test_risk.py` | 151 | DST 边界用例在 tz 匹配时 pytest.skip，CI 不可移植 | test-coverage |
| 26 | `backend/tests/test_market_calendar.py` | 113 | `is_trading_hours` 在 DST 切换瞬间（2:00 AM local）无测试 | test-coverage |
| 27 | `backend/tests/test_runner.py` | 113 | secondary pending 测试只覆盖单 symbol 重复，并发多 symbol 未测 | test-coverage |
| 28 | `backend/tests/test_broker_retry.py` | 36 | `_call_with_retry` 退避被 `time.sleep` mock 旁路，未校验 10ms/20ms/40ms 序列 | test-coverage |
| 29 | `frontend/src/views/Strategy.vue` | 331 | `loadLLMStatus` catch 静默 | frontend-ux |
| 30 | `frontend/src/views/Reports.vue` / `Review.vue` | 164 / 212 | 默认 symbol `'AAPL.US'` 硬编码测试值 | frontend-ux |
| 31 | `frontend/src/views/Backtest.vue` | 233 | `sampleCsv` 在 mount 时预填 | frontend-ux |
| 32 | `frontend/src/views/Dashboard.vue` | 755 / 719 | `loadMetrics` / `loadStatusHistory` catch 静默 | frontend-ux |
| 33 | `frontend/src/views/Dashboard.vue` | 277 | 多标的表无 error UI | frontend-ux |
| 34 | `frontend/src/views/Dashboard.vue` | 1062 | session-dot 无 aria-label | frontend-ux |
| 35 | `backend/app/database.py` | 89 | `_ensure_*` 用 f-string 拼 ALTER TABLE，注释里已提醒未来风险 | security |
| 36 | `backend/app/services/llm_advisor_service.py` | 422 | throttle 检查仅针对 60s 窗口，force=True 路径无限制 | security |
| 37 | `backend/app/services/llm_advisor_service.py` | 659 | `httpx.post` 120s timeout，无连接池限制 | security/perf |
| 38 | `backend/app/services/review_service.py` | 34 | `get_review` 4 个独立查询相同过滤 | perf |
| 39 | `backend/app/services/report_service.py` | 199 | `_build_report` day-by-day N+1 `pnl_service.calculate` | perf |
| 40 | `backend/app/api/trade.py` | 514 | `_fetch_account_response` 串行调用 broker.get_account/positions/quotes | perf |

## P2 / P3（共约 60 条，已落到 `state/p42-round-01-raw.md` 备查）

略。本轮优先 P0 + 部分 P1。P2/P3 留作 Round 18–19。

## Round 2 候选（已落地）

- ✅ 修复 watchlist P0（settings singleton pollution）：`backend/tests/test_watchlist_score.py` 加 autouse monkeypatch fixture，强制 `settings.deepseek_api_key = ""`。
- ✅ 顺手修 `test_config.py::test_default_values` 在 worktree cwd 下的路径脆弱性（cwd-agnostic suffix 断言）。

## Round 3+ 计划

| 候选 | 主题 | 类型 |
|------|------|------|
| 1 | broker retry 收敛（#1 P0） | backend correctness |
| 2 | runner 多 symbol 触发隔离（#14/#15 P1） | backend correctness |
| 3 | 前端静默 catch 清扫（#29/#32 P1） | frontend ux |
| 4 | market_calendar DST 边界测试（#26 P1） | test coverage |
| 5 | risk 时序 / daily_pnl 时序（#12/#13 P1） | backend correctness |
| 6 | trade_event_service 单元测试补齐（#11 P0） | test coverage |
| 7 | LLM advisor 错误处理统一（#19/#20/#21 P1） | backend resilience |
| 8 | cancel order 鉴权（#8 P0，security） | backend security |
| 9 | auth 日志侧信道（#10 P0） | backend security |
| 10 | report N+1（#39 P1） | backend perf |
