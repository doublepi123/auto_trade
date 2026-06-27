# P309–P328：深度分析、条件模型与元研究（20 轮自主迭代）

## 背景

承接 P299–P308 策略验证与自适应智能，本批继续相同边界：20 个 `backend/app/platform/*` 纯 Python 模块与 20 个 `/api/platform/*` 只读端点。参考 NautilusTrader、Qlib、vectorbt、mlfinlab、pyfolio/QuantStats、alphalens、Optuna、River、CausalImpact、MarketProfile、tsfresh、Gatheral 波动率面、Hamilton regime 等开源理念，聚焦深度分析、条件模型与元研究，不复制现有能力。

## 目标

形成 5 个子主题链：组合构造+成本表面 → 尾部风险+波动率期限结构 → 信号质量+自适应 → 因果推断+结构性分析 → 策略生命周期+元研究。

## 非目标

不改实盘交易/风控/broker/runner；不新增 DB 表/后台任务/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分

| 轮次 | 模块 | 端点 | 核心输出 |
|---|---|---|---|
| P309 | `pareto_optimization.py` | `/pareto-optimize` | 多目标 Pareto 前沿 |
| P310 | `volume_profile.py` | `/volume-profile` | POC/价值区间/TPO |
| P311 | `cost_surface.py` | `/cost-surface` | 成本三维网格 |
| P312 | `liquidity_adjusted_returns.py` | `/liquidity-adjusted-returns` | Amihud/Roll 调整净 alpha |
| P313 | `drawdown_surface.py` | `/drawdown-surface` | 回撤深度-持续时间联合分布 |
| P314 | `tail_hedge_cost.py` | `/tail-hedge-cost` | EVT 反推尾部对冲成本 |
| P315 | `correlation_risk_premium.py` | `/correlation-risk-premium` | 隐含-已实现相关性溢价 |
| P316 | `vol_term_structure.py` | `/vol-term-structure` | IV 期限结构 contango/backwardation |
| P317 | `concept_drift.py` | `/concept-drift` | ADWIN/EDDM 分布漂移 |
| P318 | `multitimeframe_coherence.py` | `/multitimeframe-coherence` | 多时间框架信号一致性 |
| P319 | `feature_extraction.py` | `/feature-extraction` | 自动统计特征提取 |
| P320 | `factor_momentum.py` | `/factor-momentum` | 因子时序动量 |
| P321 | `causal_impact.py` | `/causal-impact` | 贝叶斯结构时序因果 |
| P322 | `spread_stability.py` | `/spread-stability` | 滚动对冲比/半衰期时变/协整断裂 |
| P323 | `regime_transitions.py` | `/regime-transitions` | 转移概率/期望持续/稳态 |
| P324 | `regime_backtest_diagnostics.py` | `/regime-backtest-diagnostics` | regime 条件回测诊断 |
| P325 | `capacity_frontier.py` | `/capacity-frontier` | 容量退化曲线/最优容量点 |
| P326 | `regime_attribution.py` | `/regime-attribution` | regime-specific alpha/beta 归因 |
| P327 | `distribution_shape.py` | `/distribution-shape` | 滚动偏度/峰度/尾部聚类 |
| P328 | `walk_forward_surface.py` | `/walk-forward-surface` | IS/OOS 退化曲面 |

## 架构与数据流

沿用前批：纯函数 `*_report(...)` + frozen dataclass `to_dict()`，`api.py` 薄校验 + 422。模块复用 `factor_utils`；回归/矩阵用小规模确定性算法；面板限资产数 ≤50、序列 ≤5000；bootstrap 用 `random.Random(seed)`。

## 测试策略

每轮 TDD：模块单测 + API 200/422。最终目标测试 + basedpyright + 全量 pytest + Oracle review 清零 Blocking/Critical/Important。

## 文档更新

末轮同步 Roadmap/README/CLAUDE。

## 自审

无 TBD；范围只读；20 模块边界独立；与 P149–P308 区分明确（深度分析/条件模型/元研究）。