# P153–P162：平台能力闭环与扩展（10 轮自主迭代）

> 承接 P149–P152 + P156 平台地基（策略插件 SDK、统一事件流、Paper Broker、组合配置、组合风控）。本批 10 轮把平台从「地基可用」推进到「研究/执行/归因/部署可观测闭环」，每轮一个聚焦、可测、可独立合入的后端特性（前端可见处补最小 UI 不在本批，留后续）。

## 范围与非目标

- **范围**：平台层 `app/platform/`、组合层、策略版本化、平台只读/回放/诊断 API、风控 gate、Paper 持久化、高级订单意图。全部后端可由 `pytest` 验证。
- **非目标**：ML/LLM 训练闭环、因子研究仓、TWAP/VWAP 全量执行算法、灰度部署管控台、前端组合 UI——这些在原 P149–P158 设计中是更大子系统，本批只取其最易落地、最解耦的切片。

## 10 轮清单

| 代号 | 主题 | 一句话 |
|------|------|--------|
| **P153** | 平台回测 API | `POST /api/platform/backtest` 用 `PlatformRunner`(paper) 在 K 线上跑任意已注册策略，返回权益曲线/成交/期末持仓 |
| **P154** | Paper 订单持久化 | `PaperBroker` 把 submit/cancel/modify/fill 同步写 `paper_orders`，构造时可从 DB 重载未终结订单 |
| **P155** | 止损 / 追踪止损 / OCO 意图 | `OrderIntent` 增加 `stop_price`/`trailing_offset`/`linked_order_id`；`PaperBroker` 按止损价触发、OCO 联动撤单 |
| **P156+** | 集中度与相关性风控 | `PortfolioRiskController` 增加单标的集中度上限 + 滚动相关性敞口检测，发 `CONCENTRATION_BREACH`/`CORRELATION_BREACH` |
| **P157** | 组合归因 API | `GET /api/portfolio/attribution`：由 event_log 成本基础 + 实时价计算每标的 realized/unrealized PnL 与贡献 |
| **P158** | 策略参数版本化与回滚 | `strategy_param_versions` 表；`PUT /api/strategy` 留版本快照；`GET /api/strategy/versions`、`POST /{id}/rollback`（写审计） |
| **P159** | 平台诊断快照 | `GET /api/platform/snapshot`：从运行中的 `PlatformRunner` 取 positions/NAV/gross/net/未结 paper 订单 |
| **P160** | 事件日志查询与确定性回放 | `GET /api/platform/events`（分页/过滤）+ `POST /api/platform/replay` 用 `EventReplayer` 在新 runner 上重建成交/持仓 |
| **P161** | 组合运行器 | `PortfolioRunner`：按 `PortfolioConfig` 周期调用 `PortfolioAllocator` 生成再平衡 intents，经 `PlatformRunner` 执行 |
| **P162** | 风控 gate 与组合 kill-switch | `_execute_intent` 前查 `RiskEngine`；CRITICAL 违规即 pause + 审计 + `PORTFOLIO_KILL_SWITCH`；`POST /api/portfolio/kill-switch` |

## 设计原则

1. **复用既有抽象**：所有新功能尽量复用 `PlatformRunner`/`PaperBroker`/`EventBus`/`EventStore`/`PortfolioConfig`/`RiskEngine`，不另起并行栈。
2. **事件先行**：新风控维度、止损触发、回放结果都走 `Event` → `EventBus` → `EventStore`，保持可观测、可回放。
3. **特性开关兼容**：`AUTO_TRADE_PLATFORM_MODE=false` 时新端点要么只读可用（snapshot/events/attribution/replay），要么明确依赖 runner（backtest 自建 runner，不依赖全局）。
4. **TDD + 频繁提交**：每轮先写失败测试，再实现，再全量回归，单独 commit。
5. **`_ensure_*` 同步**：凡新增表/列，同步加 `_ensure_*` 并在 `init_db()` 调用。
6. **审计**：所有写操作（rollback、kill-switch、rebalance 触发）经 `AuditLogger`。

## 验证策略

- 每轮：`pytest tests/ -q` 全绿（不低于上轮基线）、`basedpyright app/platform/` 0 errors。
- 末轮：全量回归 + Roadmap/CLAUDE.md 更新 + 合并。
