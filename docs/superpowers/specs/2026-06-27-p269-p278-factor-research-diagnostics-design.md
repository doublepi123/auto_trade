# P269–P278：因子研究闭环与策略诊断（10 轮自主迭代）

> 承接 P259–P268 时间序列研究与信号稳健性。本批 10 轮继续沿用平台层“纯 Python、零新依赖、可离线测试、只读 API、不接实盘 runner”的方式，参考 Qlib、alphalens、vectorbt、Zipline、Backtrader、pyfolio/empyrical、QuantStats 的研究工作流，补齐因子从定义、衰减、分层、换手、容量、稳健性到策略质量诊断的闭环。

## 路线选择

### 方案 A（采用）：因子研究闭环 + 策略质量诊断

- 内容：新增 10 个 `backend/app/platform/` 纯计算模块和 10 个 `/api/platform/*` 只读端点。
- 优点：延续当前平台研究层；低耦合；不改变实盘默认行为；可通过单元测试和 API 测试独立验证。
- 缺点：前端不可见；更多是研究工具而非直接交易功能。

### 方案 B：执行与实盘安全增强

- 内容：进一步接线 live execution、风控 gate、订单生命周期。
- 优点：更接近实盘价值。
- 缺点：风险高；需要集成 broker/runner；不适合在已有 P259–P268 未提交改动上继续叠高风险变更。

### 方案 C：前端研究工作台

- 内容：为平台分析端点新增 Lab/图表/Cypress。
- 优点：用户可见度高。
- 缺点：涉及 UX 决策、Cypress 与构建链，且用户选择了后端量化研究方向。

## 10 轮清单

| 代号 | 主题 | 一句话交付 | 主要路径 |
|------|------|------------|----------|
| **P269** | 因子换手率 | 横截面 rank 换手、Top/Bottom bucket 保留率、因子自相关 | `factor_turnover.py` |
| **P270** | 因子衰减曲线 | 多 horizon IC / RankIC / half-life / 最优 horizon | `factor_decay.py` |
| **P271** | 分组收益分析 | alphalens 风格 quantile forward return、spread、monotonicity | `factor_quantiles.py` |
| **P272** | 信息系数时序诊断 | IC 均值、波动、t-like score、正 IC 比率、回撤 | `ic_diagnostics.py` |
| **P273** | 因子覆盖率与数据健康 | coverage、missing、constant、outlier、stale factor 报告 | `factor_data_quality.py` |
| **P274** | 信号持续性与半衰期 | signal autocorrelation、turnover proxy、exponential half-life | `signal_persistence.py` |
| **P275** | 策略 SQN 质量评分 | Van Tharp SQN、trade expectancy、样本充足度等级 | `strategy_quality.py` |
| **P276** | 市场状态切片绩效 | 按 regime 分桶统计 returns/trades 的均值、波动、胜率、贡献 | `regime_performance.py` |
| **P277** | 多策略相关与分散化 | 策略收益相关矩阵、平均相关、分散化比率、冗余对 | `strategy_diversification.py` |
| **P278** | 回测置信与稳定性摘要 | bootstrap return CI、rolling Sharpe 稳定性、fragility score | `backtest_confidence.py` |

## 设计原则

1. **默认无行为变更**：所有新增功能位于 `backend/app/platform/`，不接入真实交易 runner、broker、风控状态机或数据库写路径。
2. **零新依赖**：只使用标准库；不引入 numpy / scipy / pandas / sklearn。
3. **薄 API**：端点只解析 payload、调用纯函数、把 `ValueError` 转为 422。
4. **小模块边界**：每轮一个专注模块，提供 frozen dataclass 结果对象、`to_dict()`、自由函数。
5. **复用已有能力**：复用 `factor_ic.py` 的 Pearson/Spearman/rank 思路、`api.py` 中 `_numeric_series` / `_panel_field` / `_finite_number` 解析模式；不重写 P264、P267、P268 已有能力。
6. **确定性测试**：bootstrap 或 rolling 逻辑必须支持 seed；测试覆盖正常、边界、非法输入。
7. **数值保守**：没有 scipy 时不声称精确 p-value；t-like score / confidence interval 明确为近似或 bootstrap 估计。

## API 端点

每轮追加一个只读计算端点：

- `POST /api/platform/factor-turnover`
- `POST /api/platform/factor-decay`
- `POST /api/platform/factor-quantiles`
- `POST /api/platform/ic-diagnostics`
- `POST /api/platform/factor-data-quality`
- `POST /api/platform/signal-persistence`
- `POST /api/platform/strategy-quality`
- `POST /api/platform/regime-performance`
- `POST /api/platform/strategy-diversification`
- `POST /api/platform/backtest-confidence`

所有端点使用 `require_api_key`，输入非法时返回 422；不读写数据库。

## 测试策略

- 每轮新增 `backend/tests/platform/test_<module>.py`，覆盖计算正确性、边界、非法输入。
- 每轮追加端点 200 / 422 测试到 `backend/tests/platform/test_api_risk_portfolio.py`。
- 末轮运行新增平台测试、API harness、`basedpyright app/platform/`。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 与 P264 因子 IC、P267 回测诊断重复 | 本批聚焦多期/横截面闭环、质量摘要和策略间关系，不重写单期 IC 或 trade-level diagnostics |
| API 文件继续变大 | 只添加薄包装；复杂逻辑放模块；复用已有解析 helper |
| 纯 Python 大面板较慢 | 端点输入维持 5000 上限；定位离线诊断，不进入高频路径 |
| 无 scipy 导致统计精度有限 | 返回近似字段，避免伪精确 p-value |
| 当前工作区已有未提交 P259–P268 | 只追加 P269–P278 文件与测试，避免改动上一批模块语义 |

## 验收命令

```bash
cd backend && python3 -m pytest \
  tests/platform/test_factor_turnover.py \
  tests/platform/test_factor_decay.py \
  tests/platform/test_factor_quantiles.py \
  tests/platform/test_ic_diagnostics.py \
  tests/platform/test_factor_data_quality.py \
  tests/platform/test_signal_persistence.py \
  tests/platform/test_strategy_quality.py \
  tests/platform/test_regime_performance.py \
  tests/platform/test_strategy_diversification.py \
  tests/platform/test_backtest_confidence.py \
  tests/platform/test_api_risk_portfolio.py -v

cd backend && python3 -m basedpyright app/platform/
```

## 非目标

- 不做前端页面 / Cypress。
- 不改真实交易 runner、broker、风控、凭据链路。
- 不引入新依赖。
- 不新增数据库表或迁移。
- 不实现完整 alphalens/Qlib 因子流水线或表达式 DSL；只做可复用的离线诊断原语。
- 不自动 git commit；仓库规则要求只有明确请求才提交。

## 自主授权处理

用户已明确“你自己决策，不需要再问我”。因此本规格采用方案 A，并把设计审批视为用户授权代理自主决策；后续仍按测试、类型检查和最小变更约束执行。
