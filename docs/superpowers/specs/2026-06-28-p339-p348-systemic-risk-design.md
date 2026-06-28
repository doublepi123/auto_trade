# P339–P348：系统性风险、自适应配置与期权面智能（10 轮自主迭代）

## 背景
承接 P329–P338 高级统计与动态风险，新增 10 个 `backend/app/platform/*` 纯 Python 模块与 10 个 `/api/platform/*` 只读端点。参考 Riskfolio-Lib CVaR、Adrian-Brunnermeier CoVaR、Billio 网络系统性风险、Granger 因果网络、Ang-Bekaert regime allocation、Gatheral 波动率面、Daryanani 再平衡、Barra 动态风险归因、networkx 中心性、CBOE 期权策略分析。

## 非目标
不改实盘/风控/broker/runner；不新增 DB 表/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分
| 轮 | 模块 | 端点 | 核心 |
|---|---|---|---|
| P339 | cvar_optimization.py | /cvar-optimize | Rockafellar-Uryasev CVaR 约束组合优化 |
| P340 | systemic_risk.py | /systemic-risk | ΔCoVaR + MES 条件风险传染 |
| P341 | granger_network.py | /granger-network | 多资产 Granger 因果有向网络 |
| P342 | regime_allocation.py | /regime-allocation | regime 驱动自适应配置权重 |
| P343 | greeks_surface.py | /greeks-surface | Greeks 跨 strike×expiry 敏感度面 |
| P344 | rebalancing_intelligence.py | /rebalancing-intelligence | 再平衡频率成本-收益决策 |
| P345 | dynamic_risk_contribution.py | /dynamic-risk-contribution | 时变风险贡献分解 |
| P346 | option_strategy_payoff.py | /option-strategy-payoff | 多腿期权组合损益 + 盈亏平衡 |
| P347 | network_centrality.py | /network-centrality | 度/介数/特征向量/PageRank 中心性 |
| P348 | vol_surface_arbitrage.py | /vol-surface-arbitrage | IV 面日历/蝶式/PCP 套利检测 |

## 约束
纯函数 + frozen dataclass + to_dict()；复用 factor_utils；面板 ≤50 资产、序列 ≤5000；非法输入 ValueError，API 422；矩阵/回归小规模确定性算法；面板资产数 ≤50。

## 测试
每轮 TDD：模块单测 + API 200/422。最终目标测试 + basedpyright + 全量 pytest + Oracle review。

## 自审
无 TBD；范围只读；10 模块边界独立；与 P149–P338 区分明确（系统性风险/自适应配置/期权面智能）。