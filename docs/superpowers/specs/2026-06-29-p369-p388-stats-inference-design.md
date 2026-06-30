# P369–P388：统计推断、微结构噪声与非线性依赖（20 轮自主迭代）

## 背景
承接 P359–P368，新增 20 个纯 Python 模块与 20 个只读端点。参考 Barndorff-Nielsen RV、Székely dCor、Fama-MacBeth、Engle-Ng news impact、Lo-MacKinlay VR、Mardia MVN、Hasbrouck price discovery、Engle DCC hedge、Embrechts copula stress、López de Prado overlap、AIC/BIC、Zhang noise、Gu-Kelly-Xiu nonlinear、Harvey-Siddique skew、BDS test、Ang regime beta、Ledoit-Wolf bootstrap、Choueifaty tail diversification。

## 非目标
不改实盘/风控/broker/runner；不新增 DB 表/前端/Cypress；不引入新依赖；不把诊断输出自动用于下单。

## 功能拆分（20 轮）
P369 volatility_signature / P370 distance_correlation / P371 fama_macbeth / P372 news_impact_curve / P373 variance_ratio_test / P374 multivariate_normality / P375 implied_risk_free_rate / P376 price_discovery / P377 hedge_ratio_comparison / P378 copula_stress / P379 backtest_overlap / P380 information_criteria / P381 intraday_volume_profile / P382 microstructure_noise / P383 quadratic_factor_model / P384 higher_moment_forecast / P385 bds_test / P386 regime_factor_betas / P387 strategy_correlation_bootstrap / P388 tail_diversification

## 约束
纯函数 + frozen dataclass + to_dict()；复用 factor_utils；面板 ≤50 资产、序列 ≤5000；非法输入 ValueError，API 422；矩阵/回归小规模确定性算法。

## 自审
无 TBD；范围只读；20 模块边界独立；与 P149–P368 区分明确。