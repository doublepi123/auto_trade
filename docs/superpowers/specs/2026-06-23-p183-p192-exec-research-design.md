# P183–P192：执行算法 + 研究层 + 实盘编排核心能力（10 轮深度迭代）

> 承接 P149–P182（事件流、插件 SDK、回测、执行抽象、组合/风控/归因/分析、Sizer/指标/Universe/数据/优化器/OMS/持仓/构造/预热/账本/调度/MC/投影/多币种）。本批 10 轮填补 Roadmap 显式 YAGNI 与其他开源量化核心：**算法执行、因子研究、多策略组合、Brinson 归因、回测运行持久化、智能参数搜索、交易时段过滤、保证金模型、tearsheet 导出、延迟仿真**。每轮一个聚焦、可测、可独立合入的后端特性。

## 开源参考映射

| 能力域 | 参考开源项目 | 对应迭代 |
|--------|-------------|----------|
| 算法执行（TWAP/VWAP/Iceberg） | Nautilus `ExecutionAlgorithm`、Lean `ExecutionModels` | P183 |
| 因子库 + 因子 IC | WorldQuant Alpha101、alphalens、Qlib | P184 |
| 多策略组合 / alpha 池 | Nautilus 多策略、Lean `AlphaModel` 合流 | P185 |
| Brinson 归因 | Brinson-Fachler 绩效分解 | P186 |
| 回测运行持久化与对比 | Lean `BacktestResult`、QuantConnect saved runs | P187 |
| 智能参数搜索（准随机 + 中位剪枝） | Optuna TPE / Hyperband、vectorbt 搜索 | P188 |
| 交易时段过滤 | Nautilus `TradingSession`、Lean `MarketHoursDatabase` | P189 |
| 保证金 / 杠杆模型 | Nautilus `MarginModel`、Lean `BuyingPowerModel` | P190 |
| tearsheet 导出 | pyfolio `create_full_tearsheet`、QuantStats | P191 |
| 订单延迟仿真 | Nautilus `LatencyModel` | P192 |

## 10 轮清单

| 代号 | 主题 | 一句话 | 参考 |
|------|------|--------|------|
| **P183** | 算法执行（TWAP/VWAP/Iceberg） | `ExecutionAlgorithm` 把父单拆为子切片（按时间/成交量/隐藏量），逐切片产出 OrderIntent | Nautilus `ExecutionAlgorithm` |
| **P184** | 因子库 + 因子 IC | 因子表达式（rank/delta/sma 等）+ 滚动信息系数（IC）分析 | Alpha101、alphalens、Qlib |
| **P185** | 多策略组合 / alpha 池 | `StrategyCombinator` 在共享事件流上跑 N 策略，加权合流信号 | Nautilus 多策略、Lean `AlphaModel` |
| **P186** | Brinson 归因 | 配置 vs 选股贡献分解（BHB/Brinson-Fachler），扩展 P157 | Brinson-Fachler |
| **P187** | 回测运行持久化与对比 | 平台回测结果落表（`platform_backtest_runs`）+ 列表/对比端点 | Lean saved runs |
| **P188** | 智能参数搜索 | 准随机（低差）采样 + 中位剪枝，比 grid 更高效 | Optuna TPE / Hyperband |
| **P189** | 交易时段过滤 | `SessionFilter`（pre/rth/post/closed）在 runner 路由前 gate | Nautilus `TradingSession` |
| **P190** | 保证金 / 杠杆模型 | `MarginModel` 计算所需保证金 + 杠杆上限 gate | Nautilus `MarginModel`、Lean `BuyingPowerModel` |
| **P191** | tearsheet 导出 | 由回测结果生成完整 tearsheet 字典 + CSV/JSON 导出端点 | pyfolio、QuantStats |
| **P192** | 订单延迟仿真 | PaperBroker 可配置 submit/ack/fill 延迟队列，按延迟触发 | Nautilus `LatencyModel` |

## 设计原则

1. **参考而不照抄**：借鉴开源抽象形态，实现贴合本仓事件流与既有原语（`PlatformRunner`/`PaperBroker`/`Portfolio`/`EventBus`），零新依赖。
2. **加法不破坏**：算法执行/会话过滤/保证金/延迟均为可选注入；默认路径不变。
3. **事件先行**：算法执行子切片、延迟触发经事件或 runner 调度。
4. **纯后端可测**：每轮 `pytest` 全绿、`basedpyright app/platform/` 0 真实错误。
5. **`_ensure_*` 同步**：凡新增表（platform_backtest_runs 等）同步迁移。
6. **YAGNI**：不做真实 ML 训练、不做暗池撮合 L2、不做前端 UI。

## 验证策略

- 每轮：`pytest tests/ -q` 不低于上轮、`basedpyright app/platform/` 0 errors。
- 末轮：全量回归 + Roadmap/CLAUDE.md 更新 + 合并。
