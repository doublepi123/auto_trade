# P279–P288：ML 研究流水线与快速信号验证（10 轮自主迭代）

> 承接 P259–P278 的时间序列、因子诊断与策略质量工具链。本批继续沿用平台层“纯 Python、零新依赖、可离线测试、只读 API、不接实盘 runner”的方式，参考 Qlib、mlfinlab、alphalens、vectorbt、Zipline Pipeline、QuantStats/pyfolio 的研究工作流，补齐从预测评估、监督标签、样本权重、研究 bar、因子中性化到快速信号回测的闭环。

## 路线选择

### 方案 A（采用）：ML/因子研究流水线

- 内容：新增 10 个 `backend/app/platform/` 纯计算模块和 10 个 `/api/platform/*` 只读端点。
- 优点：与 P269–P278 互补，不重复 IC/换手/回测置信；能支持后续 ML/因子实验；风险低、可单测。
- 缺点：仍是后端研究 API，前端不可见。

### 方案 B：组合与风险深度增强

- 内容：regime transition、dynamic correlation、reverse stress、drawdown risk budgeting。
- 优点：贴近组合风控。
- 缺点：与现有 risk/portfolio/stress 模块边界重叠较多。

### 方案 C：执行与微观结构研究

- 内容：market impact、capacity、execution schedule diagnostics。
- 优点：面向交易执行质量。
- 缺点：更接近实盘执行链路；保持只读时闭环不如方案 A 完整。

## 10 轮清单

| 代号 | 主题 | 一句话交付 | 主要路径 |
|------|------|------------|----------|
| **P279** | 预测诊断 | MSE/MAE/bias/directional accuracy/IC/bucket spread | `forecast_diagnostics.py` |
| **P280** | 三重障碍标签 | De Prado profit/stop/time 三障碍监督标签 | `triple_barrier.py` |
| **P281** | 样本唯一性 | 事件并发度、平均唯一性、time-decay 权重 | `sample_uniqueness.py` |
| **P282** | Research Bar Builder | tick / volume / dollar bar 构造 | `bar_builder.py` |
| **P283** | 因子中性化 | market/group demean、group zscore、OLS residualize | `factor_neutralization.py` |
| **P284** | 因子 Tearsheet | 聚合 IC、quantile、turnover、quality 的一站式报告 | `factor_tearsheet.py` |
| **P285** | 声明式特征管道 | 白名单 feature ops，不使用 eval | `feature_pipeline.py` |
| **P286** | 信号快速回测 | vectorbt 风格 entry/exit 数组回测 | `signal_backtest.py` |
| **P287** | Rolling Tearsheet | 多窗口 rolling Sharpe/Sortino/MDD/beta/alpha/IR | `rolling_tearsheet.py` |
| **P288** | 组合约束诊断 | weights 的 exposure、turnover、group/capacity violations | `portfolio_constraints.py` |

## 设计原则

1. **默认无行为变更**：所有新增功能位于 `backend/app/platform/`，不接入真实交易 runner、broker、风控状态机或数据库写路径。
2. **零新依赖**：只使用标准库；不引入 numpy / scipy / pandas / sklearn。
3. **薄 API**：端点只解析 payload、调用纯函数、把 `ValueError` 转为 422。
4. **小模块边界**：每轮一个专注模块，提供 frozen dataclass 结果对象、`to_dict()`、自由函数。
5. **复用已有能力**：复用 `factor_utils.py`、`factor_ic.py`/`factor_quantiles.py`/`factor_turnover.py`/`factor_data_quality.py`、`risk_ratios.py` 的既有思路；不重写已有能力。
6. **安全 DSL**：`feature_pipeline.py` 只支持白名单 op 和显式依赖，不使用 `eval`、`exec` 或动态 import。
7. **只读快速研究**：`signal_backtest.py` 是离线数组回测，不替代 `PlatformRunner`、不下单、不持久化。

## API 端点

- `POST /api/platform/forecast-diagnostics`
- `POST /api/platform/triple-barrier-labels`
- `POST /api/platform/sample-uniqueness`
- `POST /api/platform/bar-builder`
- `POST /api/platform/factor-neutralization`
- `POST /api/platform/factor-tearsheet`
- `POST /api/platform/feature-pipeline`
- `POST /api/platform/signal-backtest`
- `POST /api/platform/rolling-tearsheet`
- `POST /api/platform/portfolio-constraints`

所有端点使用 `require_api_key`，输入非法时返回 422；不读写数据库。

## 测试策略

- 每轮新增 `backend/tests/platform/test_<module>.py`，覆盖正常、边界、非法输入。
- 每轮追加端点 200 / 422 测试到 `backend/tests/platform/test_api_risk_portfolio.py`。
- 末轮运行新增平台测试、API harness、`basedpyright app/platform/` 和后端全量 pytest。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Feature pipeline 变成无限 DSL | 只允许固定白名单 op；未知 op / 缺依赖直接 ValueError |
| Signal backtest 与现有 runner 混淆 | 明确只接数组输入、纯计算、无订单对象/DB/runner |
| Factor tearsheet 重复细粒度端点 | 作为聚合报告，复用已有模块，不重写核心算法 |
| OLS residualize 奇异矩阵 | 用小型 ridge 正则化高斯消元，返回残差，不声称精确 Barra |
| Bar builder 输入过大 | API 层限制列表长度，定位研究用途 |

## 非目标

- 不做前端页面 / Cypress。
- 不改真实交易 runner、broker、风控、凭据链路。
- 不引入新依赖。
- 不新增数据库表或迁移。
- 不实现完整 Qlib/Zipline/Vectorbt/mlfinlab；只做本仓可维护的研究原语。

## 自主授权处理

用户已明确“批准自主执行”。因此本规格采用方案 A，并允许代理完成规格、计划、实现、验证、复审与最终提交推送；仍需遵守测试覆盖、最小变更、无新依赖和安全边界。
