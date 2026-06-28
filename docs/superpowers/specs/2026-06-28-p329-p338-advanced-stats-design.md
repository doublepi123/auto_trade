# P329–P338：高级量化统计、网络智能与动态风险分析（10 轮自主迭代）

## 背景
承接 P309–P328 深度分析与元研究，新增 10 个 `backend/app/platform/*` 纯 Python 模块与 10 个 `/api/platform/*` 只读端点。参考 Mantegna MST、Newey-West HAC、Lo-MacKinlay 方差比、Reverse Stress Testing、Sharpe 风格分析、RiskMetrics EWMA、Barra 风险归因、Gatheral 波动率面、regime-switching cointegration、Michaud turnover frontier。

## 非目标
不改实盘/风控/broker/runner；不新增 DB 表/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分
| 轮 | 模块 | 端点 | 核心 |
|---|---|---|---|
| P329 | correlation_network.py | /correlation-network | MST + 层次聚类 + 节点中心性 |
| P330 | hac_statistics.py | /hac-statistics | Newey-West HAC 标准误 + t/p |
| P331 | adjusted_sharpe.py | /adjusted-sharpe | 自相关/偏度/峰度校正 Sharpe |
| P332 | reverse_stress.py | /reverse-stress | 逆向搜索 VaR 突破临界场景 |
| P333 | dynamic_style.py | /dynamic-style-analysis | 滚动 NNLS 风格分析 + 漂移 |
| P334 | online_covariance.py | /online-covariance | EWMA 递推协方差 + 条件数 |
| P335 | multi_strategy_risk.py | /multi-strategy-risk | 策略级风险贡献 + HHI |
| P336 | vol_of_vol.py | /vol-of-vol | VoV term structure + regime |
| P337 | regime_cointegration.py | /regime-cointegration | regime 内协整 + 断裂检测 |
| P338 | turnover_frontier.py | /turnover-frontier | 换手 vs 净 Sharpe 前沿 |

## 约束
纯函数 + frozen dataclass + to_dict()；模块复用 factor_utils；面板 ≤50 资产、序列 ≤5000；非法输入 ValueError，API 422；矩阵/回归小规模确定性算法。

## 测试
每轮 TDD：模块单测 + API 200/422。最终目标测试 + basedpyright + 全量 pytest + Oracle review。

## 自审
无 TBD；范围只读；10 模块边界独立；与 P149–P328 区分明确（网络/统计推断/动态风险）。