# P359–P368：风险体制、信息微观结构与流动性智能（10 轮自主迭代）

## 背景
承接 P349–P358，新增 10 个 `backend/app/platform/*` 纯 Python 模块与 10 个 `/api/platform/*` 只读端点。参考 RiskMetrics regime、Easley-O'Hara informed trading、Barra PCA 分解、Asness factor timing、Grasselli rebalancing、Grinold-Kahn capacity、Inclán-Tiao ICSS、Forbes-Rigobon correlation contagion、Mandelbrot fractal、Pastor-Stambaugh liquidity。

## 非目标
不改实盘/风控/broker/runner；不新增 DB 表/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分
| 轮 | 模块 | 端点 | 核心 |
|---|---|---|---|
| P359 | volatility_regime.py | /volatility-regime | 波动率体制检测+持续期 |
| P360 | information_trades.py | /information-trades | 成交自信息+知情交易概率 |
| P361 | systematic_risk_decomposition.py | /systematic-risk-decomposition | PCA 协方差系统性风险占比 |
| P362 | factor_timing.py | /factor-timing | 因子估值/拥挤/动量 timing |
| P363 | rebalancing_optimization.py | /rebalancing-optimization | 再平衡路径优化+前沿 |
| P364 | capacity_scaling.py | /capacity-scaling | AUM 缩放衰减曲线 |
| P365 | variance_break.py | /variance-break | ICSS 方差结构突变 |
| P366 | regime_switching_correlation.py | /regime-switching-correlation | 双状态相关切换 |
| P367 | trade_size_distribution.py | /trade-size-distribution | 成交分布幂律+Hurst |
| P368 | liquidity_adjusted_ir.py | /liquidity-adjusted-ir | 流动性调整 IR |

## 约束
纯函数 + frozen dataclass + to_dict()；复用 factor_utils；面板 ≤50 资产、序列 ≤5000；非法输入 ValueError，API 422；矩阵/回归小规模确定性算法。

## 测试
每轮 TDD；最终目标测试 + basedpyright + 全量 pytest + Oracle review。

## 自审
无 TBD；范围只读；10 模块边界独立；与 P149–P358 区分明确。