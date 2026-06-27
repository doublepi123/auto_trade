# P289–P298：跨资产洞察、交易前智能与信号诊断（10 轮自主迭代）

## 背景

承接 P259–P288 的只读研究平台扩展，本批继续采用相同边界：新增 10 个 `backend/app/platform/*` 纯 Python 计算模块和 10 个 `/api/platform/*` 只读端点。所有能力用于研究、诊断与交易前分析，不接入实盘 `runner` / `broker` / 订单执行路径，不写数据库，不引入新依赖。

## 目标

形成一条“跨资产环境识别 → 交易前成本估计 → 模型/信号诊断 → 组合行为归因”的研究闭环，补齐横截面分散度、方差风险溢价、事前成本、模型融合、期权隐含矩、相关性体制、因子拥挤、曲线价差、换手归因和信号 IR。

## 非目标

- 不改实盘交易逻辑、风控状态机、broker SDK 封装或 runner 主循环。
- 不新增数据库表、后台任务或持久化写路径。
- 不引入 numpy/scipy/pandas/sklearn 等依赖。
- 不新增前端页面或 Cypress 端到端测试。
- 不把任何诊断输出自动用于下单。

## 功能拆分

| 轮次 | 模块 | 端点 | 核心输出 |
|---|---|---|---|
| P289 | `cross_sectional_dispersion.py` | `POST /api/platform/cross-sectional-dispersion` | 横截面 std/IQR/MAD/Gini/分位扩散、活跃机会评分 |
| P290 | `variance_risk_premium.py` | `POST /api/platform/variance-risk-premium` | realized variance、implied variance、VRP、z-score、状态标签 |
| P291 | `pretrade_cost.py` | `POST /api/platform/pretrade-cost` | spread/impact/volatility/participation 事前成本估计与成本有效前沿 |
| P292 | `ensemble_blending.py` | `POST /api/platform/ensemble-blending` | 多预测器权重、OOS R²、贡献度、冗余模型检测 |
| P293 | `option_implied_moments.py` | `POST /api/platform/option-implied-moments` | IV smile/skew/term 指标、风险中性矩近似 |
| P294 | `correlation_regime.py` | `POST /api/platform/correlation-regime` | 平均相关、最大特征值近似、集中度与相关性体制标签 |
| P295 | `factor_crowding.py` | `POST /api/platform/factor-crowding` | 因子信号集中度、估值价差、流量拥挤与综合拥挤评分 |
| P296 | `curve_spread.py` | `POST /api/platform/curve-spread` | 曲线 spread、carry、roll-down、z-score、均值回归信号 |
| P297 | `turnover_attribution.py` | `POST /api/platform/turnover-attribution` | 换手拆解：漂移、再平衡、进入/退出、现金流近似 |
| P298 | `signal_information_ratio.py` | `POST /api/platform/signal-information-ratio` | 信号 IR、SNR、稳定性、分桶质量、理论上限 |

## 架构与数据流

每个模块提供一个纯函数 `*_report(...)`，返回 frozen dataclass，并提供 `to_dict()` 生成 JSON-friendly 输出。`backend/app/platform/api.py` 只负责读取 payload、调用已有 `_finite_number` / `_numeric_series` / 新增局部校验辅助函数、捕获 `(TypeError, ValueError)` 并转换为 HTTP 422。

模块可复用 `factor_utils.py` 中的 `validate_series`、`validate_pair`、`mean`、`std`、`pearson`、`ranks`、`spearman`。若需要矩阵或回归，只实现小规模确定性算法，不引入第三方库。

## 输入校验与错误处理

- 所有数值必须是有限 `int | float`，拒绝 bool、NaN、inf、字符串。
- 所有序列必须非空，并按模块要求满足最小长度。
- 字典型输入必须显式校验为 dict，列表元素必须逐项校验类型。
- 模块层非法输入统一抛 `ValueError`；API 层统一返回 422。
- 为避免 DoS，面板类输入限制为小规模研究数据（沿用现有 `validate_series` 5000 上限；矩阵/面板在模块内限制资产数与行数）。

## 测试策略

每轮采用 TDD：先写模块单测与 API 422/200 回归，再实现。每个模块至少覆盖一个核心行为、一个边界行为和一个非法输入。最终验证包括：

- P289–P298 目标测试集合；
- 新增模块 `basedpyright`；
- 必要时全量 `python3 -m pytest tests/ -q`；
- Oracle review，Blocking/Critical/Important 必须清零。

## 文档更新

最后一轮同步更新：

- `docs/Roadmap.md`：新增 P289–P298 完成段。
- `README.md`：补充端点清单。
- `CLAUDE.md`：补充 platform 模块地图。

## 自审

- 无 TBD/TODO 占位。
- 范围限定为只读研究平台，不与实盘路径耦合。
- 10 个模块边界独立，可逐轮测试与合入。
- 与 P259–P288 区分明确：本批偏跨资产环境、交易前智能和信号诊断，而非时间序列/ML 流水线基础能力。
