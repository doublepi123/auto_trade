# P173–P182：参考开源量化平台的订单/持仓/研究/编排核心能力（10 轮深度迭代）

> 承接 P149–P172（事件流、插件 SDK、回测、执行抽象、组合/风控/归因/分析、Sizer/指标/Universe/数据目录/优化器/成交模型）。本批 10 轮继续参考业界开源量化平台核心能力，补齐**订单管理、持仓引擎、基准相对分析、组合构造、预热、交易账本、调度器、蒙特卡洛稳健性、事件投影、多币种**——把平台推进到「机构级编排 + 研究级相对绩效」。每轮一个聚焦、可测、可独立合入的后端特性。

## 开源参考映射

| 能力域 | 参考开源项目 | 对应迭代 |
|--------|-------------|----------|
| 订单管理系统 OMS | Nautilus `OMS`、Lean `Ordering/OrderTicket` | P173 |
| 持仓引擎（多空/净额/翻转） | Nautilus `Position`/`PositionEngine`、Lean `SecurityPosition` | P174 |
| 基准与 alpha/beta | pyfolio `benchmark`、empyrical `alpha/beta/information_ratio` | P175 |
| 组合构造模型 | Lean `IPortfolioConstructionModel`（risk parity / equal weight） | P176 |
| 预热 / 历史播种 | Lean `SetWarmup`、Backtrader `preload` | P177 |
| 交易账本 | pyfolio `transactions`、QuantStats | P178 |
| 策略调度 | Lean `ScheduledEvent`、Backtrader `timer` | P179 |
| 蒙特卡洛稳健性 | vectorbt、QuantStats `rolling_sharpe` | P180 |
| 事件投影 / 读模型 | 事件溯源 CQRS、Lean `Consolidator` | P181 |
| 多币种 / CashBook | Nautilus `currencies`、Lean `CashBook` | P182 |

## 10 轮清单

| 代号 | 主题 | 一句话 | 参考 |
|------|------|--------|------|
| **P173** | 中央订单管理 OMS | `OrderManagementSystem` 订阅 order/fill 事件，跟踪全部订单状态转换、按状态查询（open/filled/cancelled）、关联成交 | Nautilus `OMS` |
| **P174** | 持仓引擎（多空/净额/翻转） | `PositionEngine` 维护 typed `Position`（long/short/flat），处理翻转与每仓 realized PnL | Nautilus `Position`/`PositionEngine` |
| **P175** | 基准与 alpha/beta | 策略 vs 基准（如买入持有）→ alpha/beta/相对收益/上涨下跌捕获率 | pyfolio `benchmark`、empyrical |
| **P176** | 组合构造模型 | `PortfolioConstructionModel` Protocol：`EqualWeightModel`/`RiskParityModel`（反波动加权）；信号→目标权重→OrderIntent | Lean `IPortfolioConstructionModel` |
| **P177** | 预热 / 历史播种 | `WarmupProvider` 在主数据流前喂入 N 根历史 bar，预热期不产生真实 intent | Lean `SetWarmup` |
| **P178** | 交易账本 | 每笔 fill 持久化 `transactions` 表；`GET /api/platform/transactions` 查询；支持导出 | pyfolio `transactions` |
| **P179** | 策略调度器 | `Scheduler`：按 bar 间隔或时间触发回调（如每 N bar 再平衡、每日收盘） | Lean `ScheduledEvent`、Backtrader timer |
| **P180** | 蒙特卡洛稳健性 | `MonteCarloAnalyzer` 重采样成交回报 → PnL 分布/百分位/破产概率；`POST /api/platform/montecarlo` | vectorbt、QuantStats |
| **P181** | 事件投影 / 读模型 | `Projection` 框架：从 `event_log` 构建滚动 NAV/日收益读模型 + 快照 | 事件溯源 CQRS |
| **P182** | 多币种 CashBook | `CashBook` 按币种记账 + FX 汇率；NAV 跨 US/HK 聚合 | Nautilus currencies、Lean `CashBook` |

## 设计原则

1. **参考而不照抄**：借鉴开源抽象形态，实现贴合本仓事件流与既有原语（`Portfolio`/`EventBus`/`EventStore`/`PlatformRunner`），零新依赖。
2. **加法不破坏**：OMS/PositionEngine/Scheduler/Projection/CashBook 均可选注入；默认路径不变。
3. **事件先行**：OMS/PositionEngine/Projection/CashBook 订阅 bus 事件或读 `event_log`；分析器纯函数消费事件。
4. **纯后端可测**：每轮 `pytest` 全绿、`basedpyright app/platform/` 0 真实错误。
5. **`_ensure_*` 同步**：凡新增表（transactions 等）同步迁移。
6. **YAGNI**：不做真实 TWAP/VWAP 算法、不做 ML 训练、不做前端 UI。

## 验证策略

- 每轮：`pytest tests/ -q` 不低于上轮、`basedpyright app/platform/` 0 errors。
- 末轮：全量回归 + Roadmap/CLAUDE.md 更新 + 合并。
