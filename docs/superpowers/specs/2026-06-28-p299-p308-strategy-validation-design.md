# P299–P308：策略验证与自适应智能（10 轮自主迭代）

## 背景

承接 P289–P298 跨资产洞察、交易前智能与信号诊断，本批继续相同边界：新增 10 个 `backend/app/platform/*` 纯 Python 计算模块与 10 个 `/api/platform/*` 只读端点。能力聚焦「给定策略/信号后，如何严谨验证其统计显著性、量化市场冲击与容量、理解不同市况下的自适应行为」。

## 目标

形成一条「条件化行为分析 → 显著性验证 → 容量与冲击边界 → 模型基准」的验证闭环，覆盖 regime 因子收益、信息流因果、事件研究、bootstrap 显著性、动态因子暴露、市场冲击建模、波动率预测对比、策略容量、动量溢出、尾部相依。

## 非目标

- 不改实盘交易逻辑、风控状态机、broker SDK 封装或 runner 主循环。
- 不新增数据库表、后台任务或持久化写路径。
- 不引入 numpy/scipy/pandas/sklearn/statsmodels 等依赖。
- 不新增前端页面或 Cypress 端到端测试。
- 不把任何诊断输出自动用于下单。

## 功能拆分

| 轮次 | 模块 | 端点 | 核心输出 |
|---|---|---|---|
| P299 | `regime_factor_returns.py` | `POST /api/platform/regime-factor-returns` | 按 regime 切片因子 IC/收益/胜率 |
| P300 | `transfer_entropy.py` | `POST /api/platform/transfer-entropy` | 双序列信息流方向与强度 |
| P301 | `event_study.py` | `POST /api/platform/event-study` | AR/CAR/检验统计量/事件窗口显著性 |
| P302 | `bootstrap_strategy_significance.py` | `POST /api/platform/bootstrap-significance` | 零 alpha 下 Sharpe bootstrap p 值与 CI |
| P303 | `dynamic_factor_exposure.py` | `POST /api/platform/dynamic-factor-exposure` | 滚动/EW 因子暴露时序与漂移检测 |
| P304 | `market_impact_model.py` | `POST /api/platform/market-impact` | 幂律/平方根临时+永久冲击函数 |
| P305 | `vol_forecast_comparison.py` | `POST /api/platform/vol-forecast-comparison` | RMSE/QLIKE/方向准确率模型对比 |
| P306 | `strategy_capacity.py` | `POST /api/platform/strategy-capacity` | 信号自相关/深度/换手→容量拐点 |
| P307 | `momentum_spillover.py` | `POST /api/platform/momentum-spillover` | 跨资产动量 Granger/脉冲/领先-滞后 |
| P308 | `tail_dependence.py` | `POST /api/platform/tail-dependence` | 经验/参数上下尾相依系数 |

## 架构与数据流

每个模块提供纯函数 `*_report(...)`，返回 frozen dataclass 与 `to_dict()`。`backend/app/platform/api.py` 读取 payload、调用 `_finite_number`/`_numeric_series`/局部校验、捕获 `(TypeError, ValueError)` 转 422。模块可复用 `factor_utils.py` 的 `validate_series/validate_pair/mean/std/pearson/ranks/spearman`；回归/特征值只用小规模确定性算法，不引入第三方库。面板/矩阵类输入限制资产数量与序列长度以避免 DoS（沿用 `validate_series` 5000 上限并在模块内限制资产数）。

## 输入校验与错误处理

- 数值必须是有限 `int|float`，拒绝 bool、NaN、inf、字符串。
- 序列非空，按模块要求满足最小长度。
- 字典输入显式校验，列表元素逐项校验。
- 模块层非法输入统一 `ValueError`；API 层统一 422。
- 随机类算法（bootstrap）使用固定 `random.Random(seed)` 保证确定性。

## 测试策略

每轮 TDD：先写模块单测与 API 422/200 回归，再实现。每个模块至少覆盖一个核心行为、一个边界行为和一个非法输入。最终验证包括 P299–P308 目标测试、新增模块 basedpyright、必要时全量 `pytest tests/ -q`、Oracle review 清零 Blocking/Critical/Important。

## 文档更新

最后一轮同步 `docs/Roadmap.md`、`README.md`、`CLAUDE.md`。

## 自审

- 无 TBD/TODO 占位。
- 范围限定为只读研究平台，不与实盘路径耦合。
- 10 个模块边界独立，可逐轮测试与合入。
- 与 P149–P298 区分明确：本批偏策略验证、自适应行为与统计显著性，而非基础研究能力。
