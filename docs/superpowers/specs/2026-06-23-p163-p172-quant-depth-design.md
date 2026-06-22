# P163–P172：参考开源量化平台核心能力的 10 轮深度迭代

> 承接 P149–P162 平台地基与闭环。本批 10 轮参考业界开源量化平台（Nautilus Trader、QuantConnect Lean、Backtrader、pyfolio/empyrical、vectorbt）的核心能力，把 auto_trade 平台从「可跑回测/可归因」推进到「研究级分析 + 工程化执行抽象」。每轮一个聚焦、可测、可独立合入的后端特性。

## 开源参考映射

| 能力域 | 参考开源项目 | 对应迭代 |
|--------|-------------|----------|
| 绩效分析 / tearsheet | pyfolio、empyrical、QuantStats | P163、P171 |
| 组合/账户中央状态 | Nautilus `Portfolio`/`Cache`、Lean `SecurityPortfolioProvider` | P164 |
| 仓位定尺 | Backtrader `Sizer`、Lean `IPortfolioConstructionModel` | P165 |
| 指标即服务 | Backtrader `Indicator`、TA-Lib、Nautilus `Indicator` | P166 |
| 标的全集选择 | Lean `Universe`/`IUniverseSelectionModel`、Nautilus `Universe` | P167 |
| 执行客户端抽象 | Nautilus `ExecutionClient`、Lean `IBrokerageModel` | P168 |
| 数据目录与重采样 | Nautilus `DataEngine`、Lean `BaseData`/`QCAlgorithm.History` | P169 |
| 参数寻优 / walk-forward | Lean `IOptimizer`、vectorbt 网格搜索 | P170 |
| 成交模型（滑点/费用可插拔） | Nautilus `FillModel`/`CostModel`、Backtrader `CommissionInfo` | P172 |

## 10 轮清单

| 代号 | 主题 | 一句话 | 参考 |
|------|------|--------|------|
| **P163** | 绩效分析（empyrical/pyfolio 核心） | 由权益曲线计算 Sharpe/Sortino/最大回撤/Calmar/胜率/盈亏比/收益序列；并入回测响应 + 独立端点 | pyfolio、empyrical、QuantStats |
| **P164** | 中央组合/账户状态 | `Portfolio` 单一真相源：cash/positions/NAV/realized PnL，订阅 fill；回测与风控从其读取 | Nautilus `Portfolio`/`Cache` |
| **P165** | 仓位定尺（Sizer） | `Sizer` Protocol：`FixedFractional`/`ATR`/`FullEquity`；策略产出信号 → Sizer 算 quantity | Backtrader `Sizer`、Lean `IPortfolioConstructionModel` |
| **P166** | 指标即服务 | `Indicator` Protocol + 注册表；滚动 SMA/EMA/RSI/ATR，经 `StrategyContext.indicator()` 取用并按 bar 缓存 | Backtrader `Indicator`、TA-Lib |
| **P167** | 标的全集（Universe） | `Universe` Protocol：`StaticUniverse`/`TopNByVolume`；runner 路由前查 universe | Lean `Universe`、Nautilus `Universe` |
| **P168** | 执行客户端抽象 | `ExecutionClient` Protocol（Paper/Live 实现），解耦 runner 与具体 PaperBroker | Nautilus `ExecutionClient` |
| **P169** | 数据目录与重采样 | `DataCatalog` 从 `event_log`/bar 存储拉历史 bar，支持 1m→5m→1h 重采样 | Nautilus `DataEngine`、Lean `History` |
| **P170** | 参数寻优与 walk-forward | `POST /api/platform/optimize`：网格/随机搜索策略参数，按 Sharpe/PnL 排名 + 样本内外拆分 | Lean `IOptimizer`、vectorbt |
| **P171** | 交易/回撤分析器 | `TradeAnalyzer`/`DrawDownAnalyzer`/`ReturnsAnalyzer` 消费已完成回测事件，输出每笔/水下曲线 | Backtrader `Analyzer`、pyfolio |
| **P172** | 可插拔成交模型 | `FillModel` Protocol：`VolumeShareSlippageModel`/`FixedPerShareCommissionModel`；PaperBroker 用模型替代固定系数 | Nautilus `FillModel`/`CostModel` |

## 设计原则

1. **参考而不照抄**：借鉴开源抽象形态（Protocol/注册表/事件订阅），实现贴合本仓既有事件流与 `PlatformRunner`，不引入新依赖。
2. **加法不破坏**：新原语（Portfolio/ExecutionClient/FillModel）以可选注入接入；默认路径行为不变。
3. **事件先行**：Sizer/Universe/Indicator 经 `StrategyContext` 或 bus 暴露；分析器消费 `event_log`，可重放可复算。
4. **纯后端可测**：每轮 `pytest` 全绿、`basedpyright app/platform/` 0 真实错误。
5. **`_ensure_*` 同步**：凡新增表/列同步迁移。
6. **YAGNI**：不引入真 ML 训练、不下单真实灰度管控、不做前端 UI（留后续）。

## 验证策略

- 每轮：`pytest tests/ -q` 不低于上轮、`basedpyright app/platform/` 0 errors。
- 末轮：全量回归 + Roadmap/CLAUDE.md 更新 + 合并。
