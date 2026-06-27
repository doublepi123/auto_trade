# P259–P268：时间序列研究 + 信号诊断 + 因子稳健性（10 轮自主迭代）

> 承接 P243–P258（期权 / 随机过程 / 统计套利 / 鲁棒统计 / 路由 / 固收 / PCA / 分数差分）。本批 10 轮继续沿用平台层“纯 Python、零新依赖、可离线测试、API 可选暴露”的迭代方式，补齐时间序列研究与信号稳健性工具链：谱分析、周期检测、变点检测、熵与复杂度、滚动特征、因子 IC、特征正交化、信号组合、回测诊断、数据质量诊断。

## 路线选择

### 方案 A：继续扩展交易核心

- 内容：改 `AppRunner`、真实下单链路、策略状态机、风控状态。
- 优点：直接影响实盘能力。
- 缺点：高风险；需要大量集成验证；10 轮连续改核心容易引入回归。

### 方案 B：前端体验和工作台增强

- 内容：Dashboard / Review / Reports / Watchlist 的交互增强。
- 优点：可见度高。
- 缺点：需要视觉决策和 Cypress 维护；用户已要求不要反复确认，不适合作为当前自主批次主线。

### 方案 C（采用）：后端平台层时间序列研究工具链

- 内容：每轮一个纯计算模块 + API 端点 + 单元测试。
- 优点：与近期 P243–P258 风格一致；低耦合；可独立验证；不改变实盘默认行为。
- 缺点：前端不可见；需要在文档中解释用途。

## 10 轮清单

| 代号 | 主题 | 一句话交付 | 主要路径 |
|------|------|------------|----------|
| **P259** | 谱分析 | DFT periodogram、主频、谱熵、带宽能量占比 | `spectral_analysis.py` |
| **P260** | 周期检测 | 自相关、Ljung-Box 近似、候选周期评分、季节性强度 | `cycle_detection.py` |
| **P261** | 变点检测 | CUSUM、均值/方差漂移检测、二分分段、告警评分 | `change_point.py` |
| **P262** | 熵与复杂度 | Shannon entropy、sample entropy、permutation entropy、Hurst 指数 | `entropy_complexity.py` |
| **P263** | 滚动特征 | rolling mean/std/zscore/skew/kurt、EWMA、rolling beta | `rolling_features.py` |
| **P264** | 因子 IC 分析 | Spearman/Pearson IC、rank IC、分桶收益、ICIR | `factor_ic.py` |
| **P265** | 特征正交化 | Gram-Schmidt、残差化、相关剪枝、VIF 近似 | `feature_orthogonalization.py` |
| **P266** | 信号组合 | 标准化、权重归一、rank combine、风险预算式信号融合 | `signal_combination.py` |
| **P267** | 回测诊断 | trade expectancy、profit factor、payoff ratio、streak、bootstrap CI | `backtest_diagnostics.py` |
| **P268** | 数据质量诊断 | 缺口、重复、异常跳点、stale price、OHLC 一致性报告 | `data_quality.py` |

## 设计原则

1. **默认无行为变更**：所有新增功能位于 `backend/app/platform/`，不接入实盘 runner。
2. **零新依赖**：只使用标准库；不引入 numpy / scipy / pandas / sklearn。
3. **小模块边界**：每轮模块提供 dataclass 结果对象、`to_dict()`、自由函数；API 层只做请求解析与 422 校验。
4. **确定性测试**：涉及随机 bootstrap 的模块必须支持 `seed`；测试覆盖正常、边界、非法输入。
5. **数值保守**：近似统计量在 docstring / 返回字段中标明 approximation；避免声称精确 p-value。
6. **复用现有风格**：沿用 `platform/api.py` 的端点注册、`tests/platform/test_api_risk_portfolio.py` 的 API harness、既有 `_math_utils.py` / `risk_metrics.py` 工具。

## API 端点

每轮追加一个只读计算端点：

- `POST /api/platform/spectral-analysis`
- `POST /api/platform/cycle-detection`
- `POST /api/platform/change-point`
- `POST /api/platform/entropy-complexity`
- `POST /api/platform/rolling-features`
- `POST /api/platform/factor-ic`
- `POST /api/platform/feature-orthogonalization`
- `POST /api/platform/signal-combination`
- `POST /api/platform/backtest-diagnostics`
- `POST /api/platform/data-quality`

所有端点使用 `require_api_key`，输入非法时返回 422；不读写数据库。

## 测试策略

- 每轮新增 `backend/tests/platform/test_<module>.py`，覆盖计算正确性、边界、非法输入。
- 每轮追加端点 200 / 422 测试到 `backend/tests/platform/test_api_risk_portfolio.py`。
- 末轮运行平台相关新增测试、API harness、`basedpyright app/platform/`。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 纯 Python DFT / rolling 在大输入下较慢 | 端点校验输入长度上限；实现偏离线诊断而非高频路径 |
| 统计 p-value 无 scipy 不精确 | 返回近似统计量和明确的 `approximation` 字段，不输出伪精确结论 |
| API 文件持续变大 | 只添加薄包装；复杂逻辑放模块；必要时复用局部解析 helper |
| 与已有模块能力重叠 | 只补时间序列诊断和信号稳健性，不重写 PCA / 分数差分 / 波动率模型 |

## 验收命令

```bash
cd backend && python3 -m pytest \
  tests/platform/test_spectral_analysis.py \
  tests/platform/test_cycle_detection.py \
  tests/platform/test_change_point.py \
  tests/platform/test_entropy_complexity.py \
  tests/platform/test_rolling_features.py \
  tests/platform/test_factor_ic.py \
  tests/platform/test_feature_orthogonalization.py \
  tests/platform/test_signal_combination.py \
  tests/platform/test_backtest_diagnostics.py \
  tests/platform/test_data_quality.py \
  tests/platform/test_api_risk_portfolio.py -v

cd backend && python3 -m basedpyright app/platform/
```

## 非目标

- 不做前端页面 / Cypress。
- 不改真实交易 runner、风控、broker、凭据链路。
- 不引入新依赖。
- 不新增数据库表或迁移。
- 不承诺机构级统计显著性检验精度；本批是离线研究与诊断工具。

## 自主授权处理

用户要求“自己全权决策，不要再问我”。因此本规格采用方案 C，视为设计方向已由代理自主批准；但不会自动 git commit，因为仓库级指令要求只有明确请求时才提交。
