# Auto Trade 迭代计划 (Iteration Roadmap)

> 本文档根据项目当前状态、近期完成工作、以及原始设计规格中的非目标/限制项制定。每次迭代聚焦单一主题，确保可交付、可验证、可回滚。

---

## 项目当前状态快照

| 维度 | 状态 |
|------|------|
| **核心交易能力** | ✅ 就绪。区间交易策略引擎、长桥SDK集成、风控系统、订单执行全部就位。 |
| **Web UI** | ✅ 就绪。Dashboard、Strategy、Credentials、Trade History、Decision Timeline 页面，Dashboard 已包含实时价格/盈亏图表。 |
| **API 覆盖** | ✅ 完备。策略配置、凭证管理、订单查询、状态获取、状态历史、事件时间线、运行时控制（启停/暂停/Kill Switch）。 |
| **WebSocket 推送** | ✅ 就绪。实时状态同步。 |
| **本地部署** | ✅ 就绪。Docker Compose 一键启动。 |
| **测试** | ✅ 就绪。Backend pytest **1228** 项、pytest-cov 覆盖率 **89%**、Frontend Cypress E2E **216** 项。 |
| **凭证安全** | ✅ 就绪。主密钥 + AES-GCM 加密存储，前端不回显明文。 |
| **数据库** | ✅ 就位。SQLite，含运行状态、状态快照、订单、`tracked_entries`、LLM 交互、交易事件、审计日志和凭证配置。 |
| **LLM 行情数据** | ✅ 真实 K 线（日 K + 1 分钟 K），ATR/布林带有效。 |
| **多市场切日** | ✅ US/HK 交易所本地日历日驱动风控与日 PnL，含静态节假日历。 |
| **入场成本** | ✅ `tracked_entries` 持久化 + 启动对账。 |
| **操作审计** | ✅ `audit_logs` 表 + `AuditLogger` + 9 个写端点接入（控制 / 策略 / 凭证 / 撤单），可 CSV/JSON 导出。 |
| **多渠道通知** | ✅ `MultiChannelNotifier` + Server 酱 + Webhook（含 token 白名单模板） + 失败重试队列 + `severity_floor` 分级。 |
| **交易时段守卫** | ✅ `trading_session_mode` 双层 gate（runner + execute 服务），`SESSION` skip 与 `TRADING_SESSION_BLOCKED` 审计。 |
| **券商韧性** | ✅ `BrokerGateway._call_with_retry` 分档退避（订单 vs 行情），`BROKER_RETRY` 审计。 |
| **LONG 加仓** | ✅ LONG 状态下 `price <= buy_low` 触发 BUY，保持 LONG；60s 冷却对齐。 |
| **保证金下单量** | ✅ `margin_safety_factor` 可配置，BrokerGateway margin 路径已验证。 |
| **LLM 持仓成本** | ✅ `ContextModule` 输出持仓方向/数量/均价/浮盈%；无持仓显示"当前无持仓"。 |
| **回测指标** | ✅ Sharpe / Sortino / Calmar / Profit Factor / 盈亏比 / 最大回撤。 |
| **绩效仪表盘** | ✅ 6 项关键指标（trade_count / win_rate / profit_factor / sharpe / max_drawdown / avg_pnl）。 |
| **指标快照** | ✅ `GET /api/indicators` 实时技术指标（ATR/RSI/MACD/布林带/成交量/多时间框架）。 |
| **日历端点** | ✅ `GET /api/calendar/{today,closures,lookup}` 节假日历查询。 |
| **观察列表评分** | ✅ LLM 评分 + 排序（`WatchlistScore` + `scored-snapshots`）。 |
| **决策时间线搜索** | ✅ 全文搜索（消息/标的/事件类型）+ 书签（localStorage 持久化）。 |
| **后端热路径** | ✅ `recent_quotes` 改 `deque(maxlen=...)` + 单边窗口淘汰；`broker.get_quotes([symbol])` 批量复用。 |
| **Docker 镜像** | ✅ 多阶段构建（`builder → runtime`），剥离 toolchain；tini 转发信号。 |

---

## 近期已完成迭代 (2026-06-28) — 深度分析、条件模型与元研究（20 轮 P309–P328）

> 自主 feature 迭代：承接 P299–P308 策略验证，参考 NautilusTrader/Qlib/vectorbt/mlfinlab/pyfolio/Optuna/River/CausalImpact/tsfresh 等，新增 20 个纯 Python / 零新依赖的深度分析、条件模型与元研究模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-28-p309-p328-deep-analytics-design.md](superpowers/specs/2026-06-28-p309-p328-deep-analytics-design.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P309** | Pareto Optimization：多目标 Pareto 前沿筛选 | ✅ |
| **P310** | Volume Profile：成交量剖面 POC/价值区间 | ✅ |
| **P311** | Cost Surface：交易成本三维曲面 | ✅ |
| **P312** | Liquidity Adjusted Returns：Amihud/Roll 流动性调整收益 | ✅ |
| **P313** | Drawdown Surface：回撤深度-持续时间联合分布 | ✅ |
| **P314** | Tail Hedge Cost：EVT 尾部对冲成本 | ✅ |
| **P315** | Correlation Risk Premium：隐含-已实现相关性溢价 | ✅ |
| **P316** | Vol Term Structure：IV 期限结构 contango/backwardation | ✅ |
| **P317** | Concept Drift：概念漂移检测 | ✅ |
| **P318** | Multitimeframe Coherence：多时间框架信号一致性 | ✅ |
| **P319** | Feature Extraction：自动统计特征提取 | ✅ |
| **P320** | Factor Momentum：因子时序动量排名 | ✅ |
| **P321** | Causal Impact：贝叶斯结构时序因果推断 | ✅ |
| **P322** | Spread Stability：滚动对冲比/半衰期时变/协整断裂 | ✅ |
| **P323** | Regime Transitions：转移概率/期望持续/稳态分布 | ✅ |
| **P324** | Regime Backtest Diagnostics：regime 条件回测诊断 | ✅ |
| **P325** | Capacity Frontier：容量退化曲线/最优容量点 | ✅ |
| **P326** | Regime Attribution：regime-specific alpha/beta 归因 | ✅ |
| **P327** | Distribution Shape：滚动偏度/峰度/尾部聚类 | ✅ |
| **P328** | Walk Forward Surface：IS/OOS 退化曲面 | ✅ |

**新增端点：** `POST /api/platform/pareto-optimize`、`/volume-profile`、`/cost-surface`、`/liquidity-adjusted-returns`、`/drawdown-surface`、`/tail-hedge-cost`、`/correlation-risk-premium`、`/vol-term-structure`、`/concept-drift`、`/multitimeframe-coherence`、`/feature-extraction`、`/factor-momentum`、`/causal-impact`、`/spread-stability`、`/regime-transitions`、`/regime-backtest-diagnostics`、`/capacity-frontier`、`/regime-attribution`、`/distribution-shape`、`/walk-forward-surface`。

**验证：** 新增纯单元测试 + `tests/platform/test_api_risk_portfolio.py`；当前新增批次验证 368 passed。

**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn/statsmodels/tsfresh，不实现完整 Nautilus/Qlib/Zipline/vectorbt/pyfolio/CausalImpact。

---

## 近期已完成迭代 (2026-06-28) — 策略验证与自适应智能（10 轮 P299–P308）
+
+> 自主 feature 迭代：承接 P289–P298 跨资产洞察，新增 10 个纯 Python / 零新依赖的策略验证、自适应行为、市场冲击容量与统计显著性模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-28-p299-p308-strategy-validation-design.md](superpowers/specs/2026-06-28-p299-p308-strategy-validation-design.md)；计划：[2026-06-28-p299-p308-strategy-validation.md](superpowers/plans/2026-06-28-p299-p308-strategy-validation.md)。
+
+| 代号 | 主题 | 状态 |
+|------|------|------|
+| **P299** | Regime Factor Returns：按 regime 切片因子 IC/收益/胜率 | ✅ |
+| **P300** | Transfer Entropy：双序列信息流方向与强度 | ✅ |
+| **P301** | Event Study：AR/CAR/检验统计量/事件窗口显著性 | ✅ |
+| **P302** | Bootstrap Strategy Significance：零 alpha Sharpe bootstrap p 值与 CI | ✅ |
+| **P303** | Dynamic Factor Exposure：滚动/EW 因子暴露时序与漂移检测 | ✅ |
+| **P304** | Market Impact Model：幂律/平方根临时+永久冲击函数 | ✅ |
+| **P305** | Vol Forecast Comparison：RMSE/QLIKE/方向准确率模型对比 | ✅ |
+| **P306** | Strategy Capacity：信号自相关/深度/换手→容量拐点 | ✅ |
+| **P307** | Momentum Spillover：跨资产动量 Granger/脉冲/领先-滞后 | ✅ |
+| **P308** | Tail Dependence：经验/参数上下尾相依系数 | ✅ |
+
+**新增端点：** `POST /api/platform/regime-factor-returns`、`/transfer-entropy`、`/event-study`、`/bootstrap-significance`、`/dynamic-factor-exposure`、`/market-impact`、`/vol-forecast-comparison`、`/strategy-capacity`、`/momentum-spillover`、`/tail-dependence`。
+
+**验证：** 新增纯单元测试 + `tests/platform/test_api_risk_portfolio.py` 可 `--no-cov` 定向运行；当前新增批次验证 255 passed。
+
+**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn/statsmodels，不实现完整 Nautilus/Qlib/Zipline/vectorbt/pyfolio。
+
+---
+
 ## 近期已完成迭代 (2026-06-27) — 跨资产洞察、交易前智能与信号诊断（10 轮 P289–P298）

> 自主 feature 迭代：承接 P279–P288 ML 研究流水线，新增 10 个纯 Python / 零新依赖的跨资产环境识别、交易前成本估计、模型融合、期权隐含矩、相关性体制、因子拥挤、曲线价差、换手归因与信号 IR 模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-27-p289-p298-cross-asset-research-design.md](superpowers/specs/2026-06-27-p289-p298-cross-asset-research-design.md)；计划：[2026-06-27-p289-p298-cross-asset-research.md](superpowers/plans/2026-06-27-p289-p298-cross-asset-research.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P289** | Cross-Sectional Dispersion：横截面 std/IQR/MAD/Gini 与机会评分 | ✅ |
| **P290** | Variance Risk Premium：realized/implied variance、VRP、z-score | ✅ |
| **P291** | Pretrade Cost：spread/impact/volatility/participation 事前成本估计 | ✅ |
| **P292** | Ensemble Blending：多预测器融合权重、OOS R²、冗余检测 | ✅ |
| **P293** | Option Implied Moments：IV smile/skew/term 与风险中性矩近似 | ✅ |
| **P294** | Correlation Regime：多资产相关矩阵、最大特征值与体制标签 | ✅ |
| **P295** | Factor Crowding：因子信号/估值/流量拥挤诊断 | ✅ |
| **P296** | Curve Spread：曲线 spread、carry、roll-down、z-score | ✅ |
| **P297** | Turnover Attribution：换手漂移、再平衡、进出资产拆解 | ✅ |
| **P298** | Signal Information Ratio：信号 IR、SNR、稳定性与分桶质量 | ✅ |

**新增端点：** `POST /api/platform/cross-sectional-dispersion`、`/variance-risk-premium`、`/pretrade-cost`、`/ensemble-blending`、`/option-implied-moments`、`/correlation-regime`、`/factor-crowding`、`/curve-spread`、`/turnover-attribution`、`/signal-information-ratio`。

**验证：** 新增纯单元测试 + `tests/platform/test_api_risk_portfolio.py` 可 `--no-cov` 定向运行；当前新增批次验证 243 passed。

**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn，不实现完整 Alphalens/Qlib/Zipline/vectorbt/mlfinlab。

---

## 近期已完成迭代 (2026-06-27) — ML 研究流水线与快速信号验证（10 轮 P279–P288）

> 自主 feature 迭代：承接 P269–P278 因子研究闭环，新增 10 个纯 Python / 零新依赖的预测评估、监督标签、样本权重、研究 bar、因子中性化、特征管道与快速信号回测模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-27-p279-p288-ml-research-pipeline-design.md](superpowers/specs/2026-06-27-p279-p288-ml-research-pipeline-design.md)；计划：[2026-06-27-p279-p288-ml-research-pipeline.md](superpowers/plans/2026-06-27-p279-p288-ml-research-pipeline.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P279** | Forecast Diagnostics：MSE/MAE/bias/directional accuracy/IC/bucket spread | ✅ |
| **P280** | Triple Barrier Labels：profit/stop/time 三重障碍监督标签 | ✅ |
| **P281** | Sample Uniqueness：事件并发度、唯一性、time-decay 权重 | ✅ |
| **P282** | Bar Builder：tick/volume/dollar research bar 构造 | ✅ |
| **P283** | Factor Neutralization：market/group demean、group zscore、OLS residualize | ✅ |
| **P284** | Factor Tearsheet：IC、quantile、turnover、quality 聚合报告 | ✅ |
| **P285** | Feature Pipeline：白名单声明式 return/sma/lag/delta/zscore/rank 特征 | ✅ |
| **P286** | Signal Backtest：entry/exit 或 target position 数组快速回测 | ✅ |
| **P287** | Rolling Tearsheet：多窗口 rolling Sharpe/MDD/beta/alpha | ✅ |
| **P288** | Portfolio Constraints：exposure、turnover、group/capacity violations | ✅ |

**新增端点：** `POST /api/platform/forecast-diagnostics`、`/triple-barrier-labels`、`/sample-uniqueness`、`/bar-builder`、`/factor-neutralization`、`/factor-tearsheet`、`/feature-pipeline`、`/signal-backtest`、`/rolling-tearsheet`、`/portfolio-constraints`。

**验证：** 新增纯单元测试 + `tests/platform/test_api_risk_portfolio.py` 可 `--no-cov` 定向运行；当前新增批次验证 233 passed。

**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn，不实现完整 Qlib/Zipline/vectorbt/mlfinlab。

---

## 近期已完成迭代 (2026-06-27) — 因子研究闭环与策略诊断（10 轮 P269–P278）

> 自主 feature 迭代：承接 P259–P268 平台研究层，新增 10 个纯 Python / 零新依赖的因子研究、信号持续性、策略质量与回测置信模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-27-p269-p278-factor-research-diagnostics-design.md](superpowers/specs/2026-06-27-p269-p278-factor-research-diagnostics-design.md)；计划：[2026-06-27-p269-p278-factor-research-diagnostics.md](superpowers/plans/2026-06-27-p269-p278-factor-research-diagnostics.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P269** | 因子换手率：rank turnover、Top/Bottom bucket 保留率、rank autocorrelation | ✅ |
| **P270** | 因子衰减：多 horizon IC/RankIC、best horizon、half-life horizon | ✅ |
| **P271** | 分组收益：quantile forward return、top-bottom spread、monotonicity | ✅ |
| **P272** | IC 诊断：mean/std、positive ratio、t-like score、cumulative IC drawdown | ✅ |
| **P273** | 因子数据质量：coverage、missing、constant、outlier、stale run | ✅ |
| **P274** | 信号持续性：autocorrelation decay、half-life lag、turnover proxy | ✅ |
| **P275** | 策略质量：SQN、expectancy、win-rate、payoff ratio、sample confidence | ✅ |
| **P276** | Regime 切片绩效：按状态分桶统计 returns / volatility / contribution | ✅ |
| **P277** | 多策略分散化：相关矩阵、平均相关、冗余对、diversification score | ✅ |
| **P278** | 回测置信：bootstrap CI、rolling Sharpe 稳定性、fragility score | ✅ |

**新增端点：** `POST /api/platform/factor-turnover`、`/factor-decay`、`/factor-quantiles`、`/ic-diagnostics`、`/factor-data-quality`、`/signal-persistence`、`/strategy-quality`、`/regime-performance`、`/strategy-diversification`、`/backtest-confidence`。

**验证：** 新增纯单元测试 + `tests/platform/test_api_risk_portfolio.py` 可 `--no-cov` 定向运行；`python3 -m pytest --no-cov ...` 当前 223 passed。

**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn，不实现完整 alphalens/Qlib DSL。

---

## 近期已完成迭代 (2026-06-26) — 时间序列研究与信号稳健性（10 轮 P259–P268）

> 自主 feature 迭代：承接 P243–P258 平台研究层，新增 10 个纯 Python / 零新依赖的时间序列、信号诊断与数据质量模块；全部通过 `/api/platform/*` 只读计算端点暴露，不接入实盘 runner 默认路径。规格：[2026-06-26-p259-p268-time-series-research-design.md](superpowers/specs/2026-06-26-p259-p268-time-series-research-design.md)；计划：[2026-06-26-p259-p268-time-series-research.md](superpowers/plans/2026-06-26-p259-p268-time-series-research.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P259** | 谱分析：DFT periodogram、正半谱主频、谱熵、频段能量 | ✅ |
| **P260** | 周期检测：自相关周期候选、Ljung-Box 近似、seasonal_strength | ✅ |
| **P261** | 变点检测：均值/方差漂移、best-first binary segmentation | ✅ |
| **P262** | 熵与复杂度：Shannon/sample/permutation entropy、Hurst R/S | ✅ |
| **P263** | 滚动特征：mean/std/zscore/skew/kurtosis、EWMA、rolling beta | ✅ |
| **P264** | 因子 IC：Pearson/Spearman rank IC、quantile buckets、ICIR | ✅ |
| **P265** | 特征正交化：Gram-Schmidt、residualize、correlation prune、VIF | ✅ |
| **P266** | 信号组合：zscore/rank/raw 合成、权重归一、风险预算权重 | ✅ |
| **P267** | 回测诊断：expectancy、profit/payoff、streak、bootstrap CI | ✅ |
| **P268** | 数据质量：timestamp/price/OHLC 质量诊断 | ✅ |

**新增端点：** `POST /api/platform/spectral-analysis`、`/cycle-detection`、`/change-point`、`/entropy-complexity`、`/rolling-features`、`/factor-ic`、`/feature-orthogonalization`、`/signal-combination`、`/backtest-diagnostics`、`/data-quality`。

**验证：** 新增纯单元测试均可 `--no-cov` 定向运行；`tests/platform/test_api_risk_portfolio.py` 当前 192 passed。全量回归见本批最终验证记录。

**显式 YAGNI 未做：** 不做前端 UI / Cypress，不新增数据库表，不接真实 broker / runner，不引入 numpy/scipy/pandas/sklearn。

---

## 近期已完成迭代 (2026-06-22) — 平台基础（P149–P150）

> 策略插件 SDK + 统一事件回放与实盘语义。新增 `app/platform/` 层，现有区间策略迁移为首个插件；`PlatformRunner` 支持 backtest/live 模式，事件流可持久化、可回放。规格：[2026-06-22-p149-p158-quant-platform-design.md](superpowers/specs/2026-06-22-p149-p158-quant-platform-design.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P149** | 策略插件 SDK（Strategy Protocol、OrderIntent、StrategyContext、StrategyRegistry） | ✅ |
| **P150** | 统一事件回放与实盘语义（EventBus、EventStore、SimBroker、PlatformRunner、EventReplayer） | ✅ |

**设计要点：**
- **统一事件模型**：`app/platform/events.py` 定义冻结 dataclass 事件，支持 `to_dict` / `from_dict` 与 `EVENT_REGISTRY` 反序列化。
- **策略插件化**：`Strategy` Protocol + `@runtime_checkable`；`IntervalStrategy` 作为首个插件接入 `app/strategies/`。
- **事件总线与持久化**：`EventBus` 内存 pub/sub；`EventStore` 写入 `event_log` 表；`EventReplayer` 可从 store 回放事件到任意 bus。
- **回测撮合**：`SimBroker` 在 backtest 模式下按 bar 触发 LIMIT 单全部成交，fill 时间戳继承触发 bar。
- **特性开关**：`AUTO_TRADE_PLATFORM_MODE` 默认关闭；开启后在 lifespan 中初始化 `PlatformRunner`（live 模式），并挂载到 `app.state.platform_runner`。
- **平台 API**：`GET /api/platform/strategies` 返回已注册策略及其 `parameter_schema`。

**验证：** `pytest tests/` **1228 passed**；`python3 -m basedpyright app/platform/ tests/platform/` 0 errors；`tests/test_platform_api.py` 覆盖平台 API 与 lifespan 开关行为。非平台模块存在若干预存在类型告警，本次未改动。

**显式 YAGNI 未做：** `SimBroker` 部分成交/滑点/延迟、live 模式与 `TradeExecutionService` 完整下单接线、多策略并发运行、跨品种组合风险、portfolio 级 attribution。

---

## 近期已完成迭代 (2026-06-22) — 组合、仿真与风控（P151–P152 + P156）

> 在平台事件流基础上实现多标的组合配置、Paper Broker 真实成交仿真（partial fill / 滑点 / 费用 / 撤改单）、组合级敞口与回撤风控，并通过 `PlatformRunner` 串成多标的闭环。规格：[2026-06-22-p149-p158-quant-platform-design.md](superpowers/specs/2026-06-22-p149-p158-quant-platform-design.md)；计划：[2026-06-22-p151-p156-portfolio-paper-risk.md](superpowers/plans/2026-06-22-p151-p156-portfolio-paper-risk.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P151** | 组合级多标的交易（`PortfolioConfig` 模型/dataclass 校验、`PortfolioAllocator` 再平衡、`PortfolioService` CRUD + `/api/portfolio/*`） | ✅ |
| **P152** | Paper Broker 真实成交仿真（partial fill、滑点、费用、撤改单状态机、`PaperOrder` 持久化；`PlatformRunner` 切换至 PaperBroker 并支持多 symbol） | ✅ |
| **P156** | 组合级风控与熔断（`PortfolioRiskController` 敞口/回撤、`RiskEngine` 事件驱动接入 `PlatformRunner`） | ✅ |

**设计要点：**
- **组合配置**：`PortfolioConfig` 既是 SQLAlchemy 模型（`portfolio_config` 表 + `_ensure_portfolio_config_table`）又是领域 dataclass（`__post_init__` 校验权重和=1、keys 匹配、阈值/敞口为正）；`PortfolioAllocator.rebalance()` 由目标权重 × 总市值反推目标持仓，产出 `OrderIntent` 列表。
- **Paper Broker**：`PaperBroker`（+`PaperOrderState`）按 bar OHLC 撮合 LIMIT 单，支持可配置 `slippage_ticks` / `commission_rate` / `partial_fill_probability`，区分 BUY/SELL 触发方向；`FillEvent` 扩展 `slippage`/`commission`/`partial`，`OrderEvent` 扩展 `reason`。`PaperOrder` 模型持久化留作后续接线（Phase 1 内存撮合为主）。
- **多标的 runner**：`PlatformRunner` 改用 `symbols: list[str]`，保留 `symbol` 参数与只读 `symbol` 属性以兼容 `main.py`/平台 API 测试；backtest/paper 模式注入 `PaperBroker`，bar/quote 按 symbol 路由；`SimBroker` 保留但 runner 不再使用。
- **组合风控**：`PortfolioRiskController.check()` 计算 gross/net 敞口比，超限发 `MAX_GROSS/NET_EXPOSURE_BREACH`（CRITICAL），`drawdown()` 跟踪峰值 NAV 发 `DRAWDOWN_BREACH`（WARNING，阈值 10%）；`RiskEngine` 订阅 fill 更新持仓、每根 bar 调 `evaluate` 经由 runner `_emit` 发出 `RiskEvent`。
- **API**：`GET /api/portfolio/config`、`PUT /api/portfolio/config/{name}`（写审计友好：400 name 不匹配、422 校验/缺字段），router 带 `require_api_key` + `tags=["portfolio"]`。

**验证：** `pytest tests/` **1260 passed, 1 skipped**；平台层 `basedpyright` 0 errors；`tests/platform/test_portfolio_*.py`、`test_paper_broker.py`、`test_portfolio_risk.py`、`test_runner.py`（含多标的 + RiskEngine 接线）与 `tests/test_portfolio_api.py`（CRUD + 400/422）全覆盖。

**显式 YAGNI 未做：** live 模式与 `TradeExecutionService` 完整下单接线、`PaperOrder` 持久化与 broker 全量同步、组合相关性风控、portfolio 级 attribution、多策略并发、前端组合配置 UI（P153–P155/P157/P158 见后续计划）。

---

## 近期已完成迭代 (2026-06-23) — 平台能力闭环与扩展（10 轮 P153–P162）

> 自主 feature 迭代第 17 批（10 轮）。承接 P149–P152 + P156 平台地基，把平台从「地基可用」推进到「研究/执行/归因/部署可观测闭环」。全部后端、`pytest` 可验、特性开关兼容。规格：[2026-06-22-p153-p162-platform-completion-design.md](superpowers/specs/2026-06-22-p153-p162-platform-completion-design.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P153** | 平台回测 API（`POST /api/platform/backtest`：任意已注册策略在 K 线上跑 PaperBroker → equity/fills/positions） | ✅ |
| **P154** | Paper 订单持久化（`PaperBroker` 同步写 `paper_orders`、`from_db` 重载未结订单） | ✅ |
| **P155** | 止损 / 追踪止损 / OCO 订单意图（`OrderIntent.stop_price/trailing_offset/linked_order_id`） | ✅ |
| **P156+** | 集中度与相关性风控（`CONCENTRATION_BREACH`/`CORRELATION_BREACH`，滚动 Pearson） | ✅ |
| **P157** | 组合归因 API（`GET /api/portfolio/attribution`：FIFO realized + unrealized PnL） | ✅ |
| **P158** | 策略参数版本化与回滚（`strategy_param_versions` + `GET /versions` + `POST /{id}/rollback` 写审计） | ✅ |
| **P159** | 平台诊断快照（`GET /api/platform/snapshot`：mode/symbols/持仓/未结 paper 订单） | ✅ |
| **P160** | 事件日志查询与确定性回放（`GET /api/platform/events` + `POST /api/platform/replay`） | ✅ |
| **P161** | 组合运行器（`PortfolioRunner`：allocator 再平衡 → `PlatformRunner.submit_intent`） | ✅ |
| **P162** | 风控 gate 与组合 kill-switch（CRITICAL 即跳过；模块级 `arm/disarm` + API 写审计） | ✅ |

**设计要点：**
- **复用既有抽象**：所有新功能复用 `PlatformRunner`/`PaperBroker`/`EventBus`/`EventStore`/`PortfolioConfig`/`RiskEngine`，不另起并行栈；`PlatformRunner` 仅新增公开 `submit_intent`。
- **事件先行**：归因从 `event_log` 成本基础推导；回放只喂 `BarEvent` 让 runner 自行撮合，避免 fill 双计。
- **特性开关兼容**：`AUTO_TRADE_PLATFORM_MODE=false` 时新只读端点（events/snapshot 404、attribution/backtest 自建 runner）不依赖全局 runner。
- **风控 gate**：`PortfolioRunner.rebalance` 先查 kill-switch，再查 `risk_engine.controller.check` 的 CRITICAL 违规，命中即跳过下单。

**验证：** `pytest tests/` **1299 passed**（基线 1260 → +39）；平台层 `basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报）；`tests/platform/` 覆盖 backtest/paper_broker（含 STOP/TRAILING/OCO + 持久化）/portfolio_risk（集中度+相关性）/attribution/portfolio_runner（kill-switch + CRITICAL gate）/portfolio_kill_switch；`tests/test_platform_api.py` 覆盖 backtest/snapshot/events/replay；`tests/test_portfolio_api.py` 覆盖 attribution + kill-switch；`tests/test_strategy_versions.py` 覆盖版本化 + rollback。

**显式 YAGNI 未做：** live↔`TradeExecutionService` 完整下单接线、TWAP/VWAP 全量执行算法、ML/LLM 训练闭环、因子研究仓、灰度部署管控台、前端组合/平台 UI、多策略并发、portfolio 级 attribution 的 Brinson 分解。

---

## 近期已完成迭代 (2026-06-23) — 参考开源量化平台核心能力的深度迭代（10 轮 P163–P172）

> 自主 feature 迭代第 18 批（10 轮）。参考 Nautilus Trader、QuantConnect Lean、Backtrader、pyfolio/empyrical、vectorbt 的核心能力，把平台从「可跑回测/可归因」推进到「研究级分析 + 工程化执行抽象」。全部后端、`pytest` 可验、加法不破坏默认路径。规格：[2026-06-23-p163-p172-quant-depth-design.md](superpowers/specs/2026-06-23-p163-p172-quant-depth-design.md)。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P163** | 绩效分析（Sharpe/Sortino/maxDD/Calmar/win-rate/profit-factor，并入回测 + `/analyze`） | pyfolio、empyrical、QuantStats | ✅ |
| **P164** | 中央 `Portfolio`（cash/positions/NAV/realized 单一真相源，回测+风控从其读取） | Nautilus `Portfolio`/`Cache` | ✅ |
| **P165** | 仓位定尺 `Sizer`（FixedFractional/FullEquity/ATR + 注册表 + `intent_from_signal`） | Backtrader `Sizer`、Lean `IPortfolioConstructionModel` | ✅ |
| **P166** | 指标即服务 `IndicatorService`（SMA/EMA/RSI/ATR 滚动缓冲+缓存，经 `StrategyContext` 取用） | Backtrader `Indicator`、TA-Lib | ✅ |
| **P167** | 标的全集 `Universe`（Static/TopNByVolume，runner 路由前 gating） | Lean `Universe`、Nautilus `Universe` | ✅ |
| **P168** | `ExecutionClient` Protocol + `LiveExecutionClient`（runner 解耦具体 PaperBroker） | Nautilus `ExecutionClient` | ✅ |
| **P169** | 数据目录 `DataCatalog` + 时间桶重采样（1m→Nm）+ `/bars` | Nautilus `DataEngine`、Lean `History` | ✅ |
| **P170** | 参数寻优 `OptimizerService`（grid + walk-forward IS/OOS）+ `/optimize` | Lean `IOptimizer`、vectorbt | ✅ |
| **P171** | Trade/DrawDown/Returns 分析器（per-trade + underwater + 分布） | Backtrader `Analyzer`、pyfolio | ✅ |
| **P172** | 可插拔成交模型 `FillModel`（Slippage/Commission Protocol，PaperBroker 可选注入） | Nautilus `FillModel`/`CostModel` | ✅ |

**设计要点：**
- **参考而不照抄**：借鉴开源抽象形态（Protocol/注册表/事件订阅/滚动缓冲），实现贴合本仓既有事件流与 `PlatformRunner`，零新依赖。
- **加法不破坏**：Portfolio/ExecutionClient/FillModel/Universe/IndicatorService 均为可选注入；默认路径行为不变（PaperBroker 仍按固定系数撮合，main.py live runner 仍 broker-less）。
- **复用**：回测响应统一含 `analytics`+`realized_pnl`；`OptimizerService` 与 `analyze_backtest` 复用 `PlatformBacktestService`/`PerformanceAnalytics`。

**验证：** `pytest tests/` **1356 passed**（基线 1299 → +57）；平台层 `basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报）；新增 `tests/platform/test_{analytics,portfolio,sizers,indicators,universe,execution,data_catalog,optimizer_service,analyzers,fill_model}.py` 全覆盖，`tests/test_platform_api.py` 覆盖 analyze/bars/optimize。

**显式 YAGNI 未做：** TWAP/VWAP/Iceberg 全量执行算法、ML/LLM 训练闭环与因子研究仓、灰度部署管控台、前端组合/平台 UI、多策略并发、portfolio 级 Brinson 归因、跨进程事件总线（当前 `EventBus` 仅内存）。

---

## 近期已完成迭代 (2026-06-23) — 机构级编排核心能力（10 轮 P173–P182）

> 自主 feature 迭代第 19 批（10 轮）。参考 Nautilus OMS/Position、QuantConnect Lean（构造/调度/CashBook/Transactions）、事件溯源 CQRS、vectorbt/QuantStats，补齐订单管理、持仓引擎、基准相对绩效、组合构造、预热、交易账本、调度器、蒙特卡洛稳健性、事件投影、多币种。全部后端、`pytest` 可验、加法不破坏默认路径。规格：[2026-06-23-p173-p182-platform-depth-design.md](superpowers/specs/2026-06-23-p173-p182-platform-depth-design.md)。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P173** | 中央订单管理 OMS（订阅 order_intent/order/fill，加权均价 fill，按状态查询） | Nautilus `OMS` | ✅ |
| **P174** | 持仓引擎（typed FLAT/LONG/SHORT 净额 + 多空翻转 + 每仓 realized） | Nautilus `Position`/`PositionEngine` | ✅ |
| **P175** | 基准 alpha/beta/tracking-error/information-ratio/up-down capture（`/analyze` 接 benchmark） | pyfolio `benchmark`、empyrical | ✅ |
| **P176** | 组合构造模型（EqualWeight/RiskParity 反波动 + `weights_to_intents`） | Lean `IPortfolioConstructionModel` | ✅ |
| **P177** | 历史预热 WarmupProvider（Lean SetWarmup 语义：预热期不发单） | Lean `SetWarmup`、Backtrader preload | ✅ |
| **P178** | 交易账本（`transactions` 表 per-fill + `GET /transactions` + bus recorder） | pyfolio `transactions` | ✅ |
| **P179** | 调度器（every-N-bars + daily-at-time，runner 自动 tick） | Lean `ScheduledEvent`、Backtrader timer | ✅ |
| **P180** | 蒙特卡洛稳健性（seeded bootstrap 重采样 → 分位/破产概率/路径）+ `/montecarlo` | vectorbt、QuantStats rolling | ✅ |
| **P181** | 事件投影（NavProjection/DailyReturnsProjection + ProjectionEngine，CQRS 读模型） | 事件溯源 CQRS、Lean `Consolidator` | ✅ |
| **P182** | 多币种 CashBook（按币种记账 + FX 聚合基币 NAV，fill 按标的币种路由） | Nautilus currencies、Lean `CashBook` | ✅ |

**设计要点：**
- **参考而不照抄**：借鉴开源抽象形态（Protocol/事件订阅/CQRS 投影），实现贴合本仓事件流与既有原语，零新依赖。
- **加法不破坏**：OMS/PositionEngine/Scheduler/CashBook/Projection 均为可选注入；runner 仅新增 `scheduler`/`warmup` 可选 kwarg，默认路径不变。
- **确定性**：`MonteCarloAnalyzer` 用注入 seed 的 `random.Random`；预热/调度在 runner 上可测试。
- **复用**：投影复用 `Portfolio`；构造/账本复用 `OrderIntent`/事件类型。

**验证：** `pytest tests/` **1405 passed, 1 skipped**（基线 1356 → +49，覆盖率 ~90%）；平台层 `basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报）；新增 `tests/platform/test_{oms,position_engine,benchmark,construction,warmup,transaction_service,scheduler,montecarlo,projections,cashbook}.py` 全覆盖。

**显式 YAGNI 未做：** TWAP/VWAP/Iceberg 全量执行算法、ML/LLM 训练闭环与因子研究仓、灰度部署管控台、前端组合/平台 UI、多策略并发、portfolio 级 Brinson 归因、跨进程事件总线。

---

## 近期已完成迭代 (2026-06-24) — 执行算法与研究层（10 轮 P183–P192）

> 自主 feature 迭代第 20 批（10 轮）。参考 Nautilus ExecutionAlgorithm/TradingSession/MarginModel/LatencyModel、WorldQuant Alpha101/alphalens、Brinson、Optuna/Hyperband、pyfolio/QuantStats，补齐算法执行、因子研究、多策略组合、Brinson 归因、回测运行持久化、智能参数搜索、交易时段过滤、保证金/杠杆、tearsheet 导出、订单延迟。全部后端、`pytest` 可验、加法不破坏默认路径。规格：[2026-06-23-p183-p192-exec-research-design.md](superpowers/specs/2026-06-23-p183-p192-exec-research-design.md)。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P183** | 算法执行（TWAP/VWAP/Iceberg 父单拆子切片 + 注册表） | Nautilus `ExecutionAlgorithm` | ✅ |
| **P184** | 因子库（momentum/volatility/meanrev）+ 横截面信息系数 IC | Alpha101、alphalens、Qlib | ✅ |
| **P185** | 多策略组合 `StrategyCombinator`（加权 alpha 合流，实现 Strategy） | Nautilus 多策略、Lean `AlphaModel` | ✅ |
| **P186** | Brinson-Fachler 归因（allocation/selection/interaction 分解） | Brinson-Fachler | ✅ |
| **P187** | 平台回测运行持久化（`platform_backtest_runs` + list/get/compare） | Lean saved runs | ✅ |
| **P188** | 智能参数搜索（准随机采样 + successive-halving 中位剪枝） | Optuna TPE / Hyperband | ✅ |
| **P189** | 交易时段过滤（pre/rth/post/closed，runner 路由前 gate） | Nautilus `TradingSession`、Lean `MarketHoursDatabase` | ✅ |
| **P190** | 保证金/杠杆模型（FixedMarginModel + LeverageGuard） | Nautilus `MarginModel`、Lean `BuyingPowerModel` | ✅ |
| **P191** | tearsheet 构建与 CSV/JSON 导出端点 | pyfolio、QuantStats | ✅ |
| **P192** | 订单延迟仿真（PaperBroker submit/fill 延迟队列） | Nautilus `LatencyModel` | ✅ |

**设计要点：**
- **参考而不照抄**：借鉴开源抽象形态，实现贴合本仓事件流与既有原语，零新依赖。
- **加法不破坏**：执行算法/会话过滤/保证金/延迟/智能搜索均为可选注入或新模块；PaperBroker 的延迟经 `QUEUED` 状态 + held-fill 队列实现，无延迟时行为完全不变。
- **确定性**：SmartOptimizer/MonteCarlo 用 seeded `random.Random`；延迟以 bar 计可精确测试。

**验证：** `pytest tests/` **1455 passed, 1 skipped**（基线 1405 → +50，覆盖率 ~90%）；平台层 `basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报）；新增 `tests/platform/test_{execution_algorithms,factors,strategy_combinator,brinson,backtest_run_service,smart_optimizer,session_filter,margin,tearsheet,latency}.py` 全覆盖。

**显式 YAGNI 未做：** 真 ML/LLM 训练闭环与因子研究仓、灰度部署管控台、前端组合/平台 UI、跨进程事件总线、暗池 L2 撮合、连续合约换月。

---

## 近期已完成迭代 (2026-06-24) — 参考开源量化核心能力的纵深补齐（10 轮 P193–P202）

> 自主 feature 迭代第 21 批（10 轮）。承接 P183–P192 执行算法与研究层，把平台里反复出现的 YAGNI（跨进程事件总线、L2 撮合、连续合约换月、因子研究仓、多策略隔离、在线信号衰减、TCA、宏观压力、回测过拟合诊断、多期 Brinson）逐条补齐。参考 Nautilus `MessageBus`/`OrderBook`/`ContinuousContract`/`StrategyPool`/`CostModel`、Lean `MessagingBus`/`ContinuousContract`/`FillMatrix`、WorldQuant/alphalens/Qlib 因子研究、RiskMetrics 场景压力、López de Prado 回测过拟合（PBO + Deflated Sharpe）、Brinson-Ibbotson/Frongello 多期链接。全部后端、`pytest` 可验、加法不破坏默认路径（新模块均为可选注入或独立函数，零新依赖）。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P193** | 跨进程事件总线（`Transport` Protocol + `DistributedEventBus` + Redis/InMemory/Null 适配 + 远端订阅回放） | Nautilus `MessageBus`、Lean `MessagingBus` | ✅ |
| **P194** | L2 订单簿撮合（`OrderBook` 价格层 FIFO 队列 + MARKET/LIMIT 跨层 partial + 撮合后挂单余量回挂） | Nautilus `OrderBook`、Lean `FillMatrix` | ✅ |
| **P195** | 连续合约换月 + 调整因子（RATIO/BACKWARD/NONE 三法，按换月点缝合，最近段不调整、历史累计调整） | Nautilus `ContinuousContract`、Lean `ContractDepthOffset` | ✅ |
| **P196** | 因子研究仓库（`factor_snapshots` + `factor_ic_series` 表 + `FactorResearchService` 持久化/排名/IC 时序 + `/api/platform/factors/*`） | alphalens `FactorData`、Qlib `FactorEngine` | ✅ |
| **P197** | 多策略并发执行隔离（`StrategyIsolationManager`：每策略独立 `Portfolio` + 资本预留 + 按 order 路由 fill + 聚合 NAV 与归因） | Nautilus `StrategyPool`、Lean per-portfolio allocation | ✅ |
| **P198** | 在线学习信号衰减（`ExponentialDecay` 半衰期权重 + `RollingWindowDecay` 在线窗口 + `reweight_combinator_weights` 静态×衰减再平衡） | zipline 在线控制、vectorbt 信号再权 | ✅ |
| **P199** | TCA 交易成本分析（`TcaAnalyzer` 按 symbol/side/source/time-bucket 归因已实现滑点+佣金，签约滑点正=不利，参考价可注入 + `/api/platform/tca`） | Nautilus `CostModel` TCA、Lean slippage analysis | ✅ |
| **P200** | 宏观压力场景库（`ScenarioLibrary`：equity_crash/volatility_spike/correlation_breakdown/liquidity_discount 确定性场景 + 按 beta 缩放 + summary 序列化） | RiskMetrics 场景压力、监管宏观情景 | ✅ |
| **P201** | 回测过拟合诊断（PBO CSCV 组合对称交叉验证 + Deflated Sharpe Ratio 多试验/非正态校正，闭式 erf CDF，无 RNG） | Bailey/López de Prado PBO 与 DSR | ✅ |
| **P202** | 多期 Brinson 链接与对账（`brinson_multi_period`：算术 BHB 链接 + Frongello 几何链接 + residual 对账 + `link_arithmetic`/`link_geometric`） | Brinson-Hood-Beebower、Frongello 几何链接 | ✅ |

**设计要点：**
- **参考而不照抄**：借鉴开源抽象形态（Transport Protocol、价格层 FIFO、半衰期衰减、CSCV 组合枚举），实现贴合本仓既有事件流与 `PlatformRunner`，零新依赖。
- **加法不破坏**：10 个模块全部为可选注入或独立纯函数；`DistributedEventBus` 继承 `EventBus` 且无 transport 时行为不变；`OrderBook`/`StrategyIsolationManager`/`TcaAnalyzer`/`ScenarioLibrary` 均不接入默认 runner 路径；`FactorResearchService`/TCA/因子 API 走新 `/api/platform/factors`、`/api/platform/tca` 端点，不影响既有路由。
- **确定性**：PBO 用精确 CSCV 枚举、DSR 用闭式 erf CDF、连续合约按固定换月点缝合、信号衰减无 RNG、压力场景纯函数——全部可精确复现。
- **复用**：因子研究复用 `factors.py` 的 `pearson`（提升为公开 helper）；多策略隔离复用 `Portfolio`；TCA 复用 `transactions` 表；多期 Brinson 复用单期 `brinson_attribution`。

**新增表：** `factor_snapshots`（`_ensure_factor_snapshots_table`）、`factor_ic_series`（同 ensure 内创建）。**新增端点：** `GET/POST /api/platform/factors/snapshots`、`GET /api/platform/factors/ic`、`GET /api/platform/tca`。**新增模块：** `app/platform/{distributed_bus,order_book,continuous_contract,factor_research_service,strategy_isolation,signal_decay,tca,stress_scenarios,overfitting,brinson_multiperiod}.py`。

**显式 YAGNI 未做：** 真 ML 训练闭环、灰度部署管控台、前端组合/平台 UI、L2 暗池行情订阅接入实盘、Redis 生产部署配置（factory 已就绪但默认 Null）。

---

## 近期已完成迭代 (2026-06-24) — 风险科学与投资组合优化（10 轮 P203–P212）

> 自主 feature 迭代第 22 批（10 轮）。承接 P193–P202 平台纵深，把投资组合学（PyPortfolioOpt / 风险科学 / Brinson 之外的两根支柱）补齐：协方差收缩（统计稳健性）、Markowitz + 风险平价 + Black-Litterman + HRP（组合优化四大经典）、VaR/CVaR + 完整风险比率族 + Pain/Ulcer（风险度量）、肥尾稳定分布（分布形态）。参考 PyPortfolioOpt `risk_models` / `efficient_frontier` / `BlackLittermanModel`、Jorion《Value at Risk》、López de Prado《Advances in Financial Machine Learning》、Burke《Ulcer Index》、Kestner / Pedar / Sedlacek、Hill (1975) tail-index estimator、McCulloch 稳定分布方法矩。全部后端、`pytest` 可验、加法不破坏默认路径（10 个新模块均为可选注入或独立纯函数，零新依赖；2 个新 `/api/platform/*` 端点不影响既有路由）。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P203** | VaR/CVaR 历史与参数化（`historical_var/cvar` + `parametric_var/cvar` + Acklam 高精度正态逆 CDF + 多资产 `portfolio_var` + `risk_metrics` 一键报告） | Jorion VaR (2007)、McNeil Frey Embrechts (2015)、empyrical | ✅ |
| **P204** | 高级回撤分析（`drawdown_events` 含 start/trough/end/duration/recovery_time + `drawdown_summary` + `rolling_calmar` + `drawdown_acceleration` 二阶导 + `underwater_curve`） | Magdon-Ismail Atiya (2004)、Chekhlov Uryasev Young (2005) | ✅ |
| **P205** | 完整风险比率族（Sharpe / Sortino / Information / Treynor / Modigliani M² / Omega + 滚动 `rolling_sharpe`） | Sharpe (1966)、Sortino (1991)、Treynor (1965)、Modigliani (1997)、Keating Shadwick Omega (2002) | ✅ |
| **P206** | Markowitz mean-variance + 有效前沿（`min_variance_weights` 闭式 Σ⁻¹·1 + `max_sharpe_weights` + `efficient_frontier` 网格采样 + `MeanVarianceModel` 实现 Protocol，可接 `cov`） | Markowitz (1952)、PyPortfolioOpt `EfficientFrontier` | ✅ |
| **P207** | Black-Litterman 投资组合（`market_implied_returns` 反优化 prior + `View` dataclass + `black_litterman` posterior 闭式 + `BlackLittermanModel` 实现 Protocol） | Black & Litterman (1991)、PyPortfolioOpt `BlackLittermanModel` | ✅ |
| **P208** | Ledoit-Wolf 协方差收缩（`sample_covariance` + `covariance_to_correlation` + `ledoit_wolf_shrinkage` 常相关目标 + 强度 δ ∈ [0,1] + `portfolio_variance`） | Ledoit & Wolf (2004) "Honey, I Shrunk the Sample Covariance Matrix"、PyPortfolioOpt `risk_models` | ✅ |
| **P209** | 层次风险平价 HRP（`correlation_distance` + 单链 quasi-diagonalize + `recursive_bisection` 反方差加权 + `HRPModel` 实现 Protocol） | López de Prado HRP (2016)、PyHRP | ✅ |
| **P210** | Pain / Ulcer / MAR / Kestner（`pain_index` 均值水下 + `ulcer_index` RMS 水下 + `mar_ratio` CAGR/|maxDD| + `kestner_ratio` CAGR/UI + `pain_metrics_report` 一键报告） | Pedar PI (1989)、Burke UI (1994)、Sedlacek MAR、Kestner (1996) | ✅ |
| **P211** | 收益分布稳定分布拟合与肥尾（`excess_kurtosis` + `skewness` + `hill_estimator` 尾部指数 α + `tail_ratio` 实证/Gaussian VaR + `stable_fit` 4 参数 + `fat_tail_report`） | Hill (1975)、McCulloch (1986)、Fama-Roll (1968/1971) | ✅ |
| **P212** | 风险科学 + 投资组合优化 API（`POST /api/platform/risk-metrics` 统一入口接 VaR/CVaR/drawdown/pain/tail/ratios；`POST /api/platform/portfolio-optimize` 4 种 method：`min_variance`/`max_sharpe`/`hrp`/`black_litterman`，接 `returns_panel` + 可选 `market_weights`/`views`） | 平台 `/api/platform/*` 一致端点约定 | ✅ |

**设计要点：**
- **参考开源成熟形态但零新依赖**：借鉴 PyPortfolioOpt 抽象（`risk_models`/`efficient_frontier`/`BlackLittermanModel`）、Jorion/López de Prado/Burke/Kestner/Hill/McCulloch 论文，全部用纯 Python + `math` 实现，`dict` 键值 I/O 与平台事件流对齐。
- **加法不破坏**：10 个模块全部为可选注入或独立纯函数；`MeanVarianceModel` / `BlackLittermanModel` / `HRPModel` 接入既有 `PortfolioConstructionModel` Protocol，与 `EqualWeight`/`RiskParity` 并列可热插拔；风险指标纯函数（`risk_metrics(returns)` / `drawdown_summary(equity)` / `pain_metrics_report(equity)` / `fat_tail_report(returns)`）可独立调用、与 runner 解耦。
- **确定性**：所有方法为纯函数（`ledoit_wolf_shrinkage` / `min_variance_weights` / `hrp_weights` / `black_litterman` / `hill_estimator` / `omega_ratio`）—— 零 RNG、给定输入恒定输出，可精确测试与回放。
- **复用**：组合优化全部走 `ledoit_wolf_shrinkage` 得到稳定协方差；`portfolio_variance` 复用同一协方差计算；`portfolio_optimize` 端点统一返回 `weights` + `expected_return` + `volatility` + `sharpe` + `shrinkage_intensity`，便于跨方法对比。
- **正态逆 CDF**：P203 闭式 Acklam 多段式算法（`0.02425` / `0.97575` 分段），无 `statistics.NormalDist` 之外的新依赖，准确度 ~1e-9。
- **风险比率族完备**：Sharpe / Sortino / Information / Treynor / Modigliani M² / Omega 6 个比率一次 `all_ratios()` 拿全；Modigliani 用 `(Sharpe_p × σ_b + rf) × periods_per_year` 年化，与标准定义一致。

**新增端点：** `POST /api/platform/risk-metrics`（统一风险入口；`returns` 或 `equity_curve` 两种入参；422 缺字段）、`POST /api/platform/portfolio-optimize`（4 method；`returns_panel` 必填；`black_litterman` 必填 `market_weights`；422 校验）。**新增模块：** `app/platform/{covariance,mean_variance,black_litterman,hrp,risk_metrics,drawdown_analysis,risk_ratios,pain_metrics,fat_tail}.py`。**无新表**（纯计算），**无新 runner 接线**（纯函数 + 端点暴露）。

**验证：** `pytest tests/` **1700 passed, 1 skipped**（基线 1565 → +135，含 2 个 pre-existing `test_config.py` 失败，与本批无关）；平台层 `basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报，与本批无关）；新增 12 个 `tests/platform/test_*.py` 全覆盖；`tests/platform/test_api_risk_portfolio.py` 覆盖 2 个新端点（含 4 method × 4 payload 形态 + 422 缺字段）。

**显式 YAGNI 未做：** Black-Litterman 多视图（Omega 矩阵非对角线）— 当前实现走对角 Ω 假设；HRP 完整 linkage（scipy 风格）— 当前用单链 greedy 简化；稳定分布 MLE 拟合（McCulloch quantile method）— 当前是方法矩；多期肥尾（time-varying α）— 当前单期；前端口径展示（tearsheet 视图）— 后续轮次；CVaR optimization（Rockafellar-Uryasev）— 当前只度量不优化。

---

## 近期已完成迭代 (2026-06-25) — 风险研究 + 执行深度 II（10 轮 P213–P222）

## 近期已完成迭代 (2026-06-25) — 配对/微观/执行/风险研究 III（10 轮 P223–P232）

> 自主 feature 迭代第 24 批（10 轮）。承接 P203–P222 风险研究，补齐剩余支柱：统计套利地基（协整/配对）、仓位定尺（Kelly）、波动率预测、市场微观结构（VPIN/OFI）、最优执行（Almgren-Chriss）、自激发事件（Hawkes）、历史场景压力、Barra 式因子风险分解、参数重要性（Saltelli Sobol）、极值理论尾部外推。参考 Engle-Granger 协整、Thorp/Kelly 仓位管理、RiskMetrics/Bollerslev GARCH、Easley-López de Prado-O'Hara VPIN、Cont-Kukanov-Stoikov OFI、Almgren-Chriss 最优执行、Hawkes 自激发过程、FRB CCAR 历史场景、Barra/Axioma 因子风险、Saltelli fANOVA、McNeil-Frey EVT/GPD。全部后端、`pytest` 可验、加法不破坏默认路径（10 个新模块均为可选注入或独立纯函数，零新依赖；10 个新 `/api/platform/*` 端点不影响既有路由）。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P223** | 协整与配对交易诊断（Engle-Granger OLS 对冲比 + OU 半衰期 + z-score + Durbin-Watson） | Engle-Granger、Vidyamurthy/Chan pairs | ✅ |
| **P224** | Kelly 仓位定尺（binary/continuous Kelly + 分数 Kelly + 期望对数增长 + 破产概率） | Thorp、Kelly、Vince | ✅ |
| **P225** | 波动率预测（EWMA RiskMetrics + GARCH(1,1) + Parkinson 高低价） | RiskMetrics、Bollerslev、Parkinson | ✅ |
| **P226** | 微观结构（VPIN 等量分桶 + OFI 订单流不平衡 + Kyle λ 价格冲击） | Easley-LdP-O'Hara、Cont et al.、Kyle | ✅ |
| **P227** | Almgren-Chriss 最优执行（风险厌恶轨迹 + 成本/风险 + 执行有效前沿） | Almgren-Chriss (2000) | ✅ |
| **P228** | Hawkes 自激发过程（branching ratio + 递归对数似然 + 强度路径） | Hawkes、Lewis/Ozaki、Filimonov-Sornette | ✅ |
| **P229** | 历史场景压力（episode 库 + apply_scenario + 资本充足率） | FRB CCAR、Basel ICAAP | ✅ |
| **P230** | 因子风险分解（Barra 式 wᵀBF Bᵀw + 特质 + per-factor 贡献） | Barra、Menchero、Grinold-Kahn | ✅ |
| **P231** | 参数重要性（Saltelli 一阶/全阶 Sobol + 交互 + 排名） | Saltelli、Optuna fANOVA、Hutter | ✅ |
| **P232** | 极值理论（POT + GPD MoM 拟合 + 尾部 VaR/CVaR 外推） | McNeil-Frey、Davison-Smith、Embrechts | ✅ |

**设计要点：**
- **参考而不照抄**：借鉴开源文献与库的数学形态（Engle-Granger 2-step、Lewis 2011 递归 Hawkes 似然、Hosking-Wallis GPD MoM、Almgren-Chriss sinh 轨迹、Saltelli 全方差分解），实现贴合本仓既有事件流与 `PlatformRunner`，零新依赖、确定性（无 RNG/优化器）。
- **加法不破坏**：10 个新模块均为可选注入或独立纯函数；10 个新 `/api/platform/*` 端点全部带 `require_api_key`、422 守卫、与既有路由互不冲突；`AUTO_TRADE_PLATFORM_MODE=false` 默认路径行为不变。
- **复用**：`kelly` 复用 `risk_ratios` 的年化口径思路、`historical_scenarios` 与 P200/P221 parametric stress 互补、`sensitivity` 复用 `stability_analysis` 的 records 形状、`extreme_value` 与 `risk_metrics` VaR 互补。

**验证：** `pytest tests/` **2014 passed**（基线 1863 → +151，含 10 个新 `tests/platform/test_*.py` 全覆盖（共 119 个新用例）+ `test_api_risk_portfolio.py` 覆盖 10 个新端点的 200/422 路径）；新增 10 个模块全部纯 Python（numpy 仍未引入），零新依赖；`basedpyright` 对 10 个新模块 0 errors（预存在的 `trade_excursion.py`/`shortfall.py`/`stability_analysis.py` 类型告警与本批无关）。

**显式 YAGNI 未做：** 协整 Johansen 多方程 / 协整向量的 VECM 预测（当前 Engle-Granger 2-step 单方程）；Kelly 多资产（multi-asset Kelly 需协方差，当前单资产/单序列）；GARCH(p,q)/EGARCH/DCC（当前 GARCH(1,1) 单序列）；VPIN 的 VPIN-v1 体积时钟 + tick-rule 分类（当前用 bar close/open bulk-classification）；Almgren-Chriss 永久冲击 + 非线性冲击（当前线性临时冲击）；Hawkes MLE 网格寻优 + 标记点过程（当前矩估计默认）；历史场景因子叠加 / 反向场景搜索（当前单层应用）；因子风险时变暴露 + 条件协方差（当前单期截面）；Saltelli 蒙特卡洛抽样估计（当前 grid 方差分解）；EVT 的 Hill 估计器 + 稳定分布（当前 GPD MoM）。前端 UI 口径展示（tearsheet 视图）— 后续轮次。


> 自主 feature 迭代第 23 批（10 轮）。承接 P203–P212 风险科学与投资组合优化，补齐机构级风险研究 + 执行分析的剩余支柱：市场状态识别、组合交叉验证防泄漏、收益风格归因、换手感知再平衡、凸风险预算 ERC、单笔交易 MFE/MAE、Implementation Shortfall、收益日历热力图、压力场景报告聚合、walk-forward 参数稳定性。参考 Nautilus `MarketRegimeModel`、López de Prado CPCV、Sharpe 1992 Style Analysis、PyPortfolioOpt turnover constraint、Maillard-Roncalli/Spinu ERC、vectorbt MFE/MAE、Perold Implementation Shortfall、pyfolio returns calendar、FRB CCAR stress、Optuna parameter stability。全部后端、`pytest` 可验、加法不破坏默认路径（10 个新模块均为可选注入或独立纯函数，零新依赖；8 个新 `/api/platform/*` 端点不影响既有路由，2 个扩展现有 `portfolio-optimize` method）。

| 代号 | 主题 | 参考 | 状态 |
|------|------|------|------|
| **P213** | 市场状态识别（`Regime` BULL/BEAR/SIDEWAYS：SMA 交叉斜率 + Wilder ADX + 实现波动 + slope/vol fallback；`RegimeModel` 流式喂 BarEvent 状态变化发 `RegimeEvent` + `rolling_regime`/`regime_report`） | Nautilus `MarketRegimeModel`、RiskMetrics/MSCI regime | ✅ |
| **P214** | 组合交叉验证分割器 CPCV（`C(N,k)` 组合枚举 + purge 对称 + embargo 后置 + `cpcv_oos_paths` 贪心不交集 + `cpcv_pbo` 复用 `overfitting` PBO） | López de Prado《Advances in Financial Machine Learning》Ch.7/Ch.11 | ✅ |
| **P215** | 收益风格分析 Sharpe 1992（纯 Python Lawson-Hanson active-set NNLS + 等式约束 simplex KKT，`none`/`sum_le_one`/`sum_eq_one` 三模式 + R²/tracking_error/annualized + `StyleAnalysisModel`） | Sharpe 1992、pyfolio `style_analysis`、Lawson-Hanson 1974 | ✅ |
| **P216** | 换手感知组合优化（`wᵀΣw − λμᵀw + γ·L1turnover`，projected subgradient + Duchi simplex 投影 + 可选 `delta_cap` L1 上限 proximal 投影，`TurnoverAwareModel` 实现 Protocol） | PyPortfolioOpt turnover constraint、Mitchell-Braun transaction-cost Markowitz | ✅ |
| **P217** | 凸风险预算/ERC（Maillard-Roncalli/Spinu Newton 迭代 log-barrier 目标 `f(y)=½yᵀΣy − Σb·ln(y)`，Hessian 对角障碍项恒 PD 即使 Σ 奇异，`risk_contributions`/`relative_risk_contributions`，`RiskBudgetingModel`） | Maillard-Roncalli-Taiïletche (2010)、Spinu (2013)、riskfolio-Lib | ✅ |
| **P218** | 单笔交易 MFE/MAE + 持仓周期（per-trade Max Favorable/Adverse Excursion + holding_bars + entry/exit timing rank + `trades_from_fills` FIFO 配对 + 百分位汇总 + mfe_mae_ratio） | vectorbt `portfolio.trades`、Nautilus `PositionAnalysis`、pyfolio trade table | ✅ |
| **P219** | Implementation Shortfall TCA（Perold 1988：realized/opportunity/timing/fees + VWAP/arrival/close benchmark + bps + participation_rate + `shortfall_from_tca` 复用 `TcaFill` + `ShortfallAnalyzer` 读 transactions 账本） | Perold 1988 "Implementation Shortfall"、Nautilus `CostModel`、Kissell cost decomposition | ✅ |
| **P220** | 收益日历热力图（monthly/yearly/weekday/streaks + 合成日历 epoch 2000-01-03 Mon + `monthly_returns_table` 透视 + NaN/inf drop + win_rate/best/worst） | pyfolio `timeseries.returns_table`、QuantStats `stats.distribution` | ✅ |
| **P221** | 压力场景报告聚合（聚合 `stress_scenarios`+`risk_metrics`+`drawdown_analysis`：per-scenario PnL/VaR + worst-scenario + capital adequacy ratio + `StressReportBuilder`） | RiskMetrics scenario aggregation、FRB CCAR stress summary | ✅ |
| **P222** | walk-forward 参数稳定性（IS/OOS 退化比 clip + IS-vs-OOS Spearman rank-corr + 邻域稳定性 Hamming-1 + 最优参数漂移 numeric stddev/range、categorical modal mismatch） | Optuna parameter importance/stability、Lean walk-forward、López de Prado stability matrix | ✅ |

**设计要点：**
- **参考开源成熟形态但零新依赖**：借鉴 Nautilus/López de Prado/Sharpe/PyPortfolioOpt/Spinu/vectorbt/Perold/pyfolio/CCAR/Optuna 的抽象与算法，全部用纯 Python + `math` 实现（**numpy 仍未引入**，与 P203–P212 一致）；`dict` 键值 I/O 与平台事件流对齐。
- **加法不破坏**：10 个模块全部为可选注入或独立纯函数；`RegimeModel` 可选注入 `PlatformRunner`（默认 None，零行为变更）；`TurnoverAwareModel`/`RiskBudgetingModel`/`StyleAnalysisModel` 接入既有 `PortfolioConstructionModel` Protocol，与 `MeanVarianceModel`/`HRPModel` 并列可热插拔；`ShortfallAnalyzer` 复用 `transactions` 表（P178）与 `ReferencePriceProvider`。
- **确定性**：全部纯函数 + 确定性起始点 + 固定迭代（Newton/subgradient/active-set），零 RNG，给定输入恒定输出，可精确测试与回放。
- **复用**：`cpcv_pbo` 复用 `overfitting.probability_of_backtest_overfitting`；`shortfall_from_tca` 复用 `TcaFill`；`stress_report` 复用 `stress_scenarios`+`risk_metrics.historical_var`+`drawdown_analysis`；`stability_analysis` 消费 `OptimizerService.walk_forward` 输出 shape。
- **事件流扩展**：`events.py` 新增 `RegimeEvent`（`event_type='regime'`）并注册进 `EVENT_REGISTRY`（9 → 含 regime）；`RegimeModel.on_bar` 在状态变化时经 `EventBus.publish` 发出，供 OMS/风控订阅。

**新增端点：** `POST /api/platform/regime` + `POST /api/platform/cpcv` + `POST /api/platform/style-analysis` + `POST /api/platform/trade-excursion` + `POST /api/platform/shortfall` + `POST /api/platform/returns-calendar` + `POST /api/platform/stress-report` + `POST /api/platform/stability`，并扩展 `POST /api/platform/portfolio-optimize` 新增 `turnover`/`risk_budgeting` method（含 `risk_contributions`/`turnover` 诊断字段）。**新增模块：** `app/platform/{regime,cpcv,style_analysis,turnover_optimization,risk_budgeting,trade_excursion,shortfall,returns_analysis,stress_report,stability_analysis}.py`。**无新表**（纯计算 + 读既有 transactions）。**无新 runner 接线**（`RegimeModel` 注入点预留但默认 None）。

**验证：** `pytest tests/` **1863 passed**（平台层 P149–P222 全部已并入，基线 1700 → +163，含 10 个新 `tests/platform/test_*.py` 全覆盖 + `test_api_risk_portfolio.py` 覆盖 8 个新端点 + 2 个新 method × 4 payload 形态 + 422 缺字段）；新增 11 个模块全部纯 Python（numpy 仍未引入），零新依赖；`basedpyright` 0 真实错误（仅 sqlalchemy/pytest/fastapi 的 `reportMissingImports` venv 误报，与本批无关）。

**显式 YAGNI 未做：** regime 隐马尔可夫多状态模型（当前是规则阈值）— 当前实现走 SMA+ADX+vol 规则分类；CPCV 滚动回测集成（当前是分割器 + PBO）— 未把 CPCV splits 接入 `OptimizerService.walk_forward`（可选注入点已预留，默认路径不变）；风格分析动态因子暴露时序（当前是单期截面）— 未做滚动窗口时变暴露；turnover 多周期成本模型（当前单期 L1）— 未建模多周期累积成本；risk budgeting 多资产带负债/约束（当前长 only 全额）— 未做带 liabilities 的 ERC；MFE/MAE 与止损策略联合回测（当前只分析不优化）— 未做 MFE/MAE 驱动的自适应止损；Implementation Shortfall 多日父单 VWAP 执行回放（当前单 order）— 未做跨日父单；returns calendar 季度/滚动 12 月视图（当前 monthly/yearly/weekday）— 未做滚动窗口；stress report 反向压力测试（当前正向场景）— 未做"找使 VaR 突破的临界场景"；stability analysis Bayesian 参数后验（当前频率派 rank/drift）— 未做贝叶斯参数分布。前端 UI 口径展示（tearsheet 视图）— 后续轮次。

---

## 近期已完成迭代 (2026-06-21) — 运维效率与个性化（10 轮 P139–P148）

> 自主 feature 迭代第 15 批（10 轮）。主题：高级用户效率层 + 可持久化个性化。承接 P129–P138 的运营健康基础（复用 `useConnectionHealth`、`useSymbolStore`、`utils/clipboard.ts`）。全部**纯前端**，复用既有 API，**不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径**。规格：[2026-06-21-p139-p148-power-user-productivity-design.md](superpowers/specs/2026-06-21-p139-p148-power-user-productivity-design.md)。

| 代号 | 主题 | 页面 / 组件 | 状态 |
|------|------|-------------|------|
| **P139** | 命令面板 shell（Cmd/Ctrl+K）：分组命令 / fuzzy / 键盘导航 / 最近使用 | `CommandPalette.vue` + App.vue | ✅ |
| **P140** | 面板：标的速跳（strategy + watchlist → 仪表盘图表） | `useSymbolStore` + Dashboard | ✅ |
| **P141** | 面板：最近访问页面排序（路由访问驱动） | `useRecentPages` + App.vue | ✅ |
| **P142** | 全局密度切换（el-config-provider，持久化） | `useDensity` + App.vue | ✅ |
| **P143** | NotificationCenter 表格列显隐（级别/结果/错误，持久化） | `usePersistedColumns` + NotificationCenter | ✅ |
| **P144** | DecisionTimeline 行键盘导航（↑/↓/j/k + Enter 详情） | DecisionTimeline | ✅ |
| **P145** | `/` 聚焦搜索快捷键（跨视图）+ Esc 失焦 | App.vue + 4 视图 | ✅ |
| **P146** | 仪表盘固定标的速跳栏（📌 + chip） | `usePinnedSymbols` + Dashboard | ✅ |
| **P147** | 面板「刷新当前页」命令（视图刷新注册表） | `useViewRefreshRegistry` + Dashboard/Watchlist/Reports | ✅ |
| **P148** | 统一帮助抽屉（快捷键 + tips，取代旧 dialog） | App.vue | ✅ |

**设计要点：**
- **命令面板**：模块单例 `useCommandPalette`；`Cmd/Ctrl+K` 在 App.vue 的 `handleKeydown` 修饰键守卫之前拦截；纯前端 fuzzy 子串评分（无新依赖）；导航/控制/标的/工具分组；最近使用持久化。控制命令复用既有 `api` 客户端（破坏性动作 ElMessageBox 确认）+ `useConnectionHealth.refreshNow()` 同步状态。
- **个性化持久化**：密度、列显隐、固定标的、最近页面、最近命令 → 统一 `auto_trade.*` localStorage 命名空间。
- **跨视图刷新**：因 provide/inject 只能父→子，而面板在视图祖先，改用 `useViewRefreshRegistry` 模块单例桥接（视图 mount 注册、unmount 仅在仍持有者时清空）。
- **键盘守卫**：`/`、`j/k`、单字母导航仅在非输入框聚焦时生效（复用 `isTypingTarget`）；面板内部键由其自身处理。
- **回归保护**：列显隐默认全显；帮助抽屉保留 `nav-shortcuts` / `shortcuts-dialog` testid + 页面导航列表（含「仪表盘」），既有 `keyboard_shortcuts.cy.ts` 不受影响。

**验证：** `vue-tsc` 0 errors、`npm run build` 通过；`build:check-chunks`（35 chunks，max 512 KB）、`build:check-element-plus`（7 chunks）均不退步；新增 `command_palette.cy.ts`。后端 `pytest` 不受影响（纯前端）。本机无法 headless 运行 Cypress，按既有约定仅类型/构建校验，spec 交 CI。

**显式 YAGNI 未做：** 自定义快捷键绑定、命令面板插件化、i18n、服务端用户偏好同步、列拖拽重排、面板命令历史撤销、图表主题随密度联动。

---

## 近期已完成迭代 (2026-06-21) — 运营健康与数据可信度（10 轮 P129–P138）

> 自主 feature 迭代第 14 批（10 轮）。主题：把「数据可信度 / 连接健康」显式化、全局化。全部**纯前端**，复用既有 `/api/status`、`/api/calendar/session` 等只读响应，**不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径**。规格：[2026-06-21-p129-p138-operational-health-data-trust-design.md](superpowers/specs/2026-06-21-p129-p138-operational-health-data-trust-design.md)。

| 代号 | 主题 | 页面 / 组件 | 状态 |
|------|------|-------------|------|
| **P129** | 实时连接上提为 App 级单例 + 全局 header 健康徽标 | `useConnectionHealth` + App.vue | ✅ |
| **P130** | 相对时间工具 + 徽标显示数据年龄（>10s 琥珀 / >30s 红） | `utils/time.ts` + App.vue | ✅ |
| **P131** | Dashboard 过期数据水印（≥15s 琥珀告警 + 价格面板标签 + 一键重连） | Dashboard | ✅ |
| **P132** | 交易时段感知横幅（非 RTH 全局可关闭 alert，按阶段 dismiss） | `useMarketSession` + App.vue | ✅ |
| **P133** | 共享 `<DataState>` 组件（loading/error/empty）+ 接入 NotificationCenter / Watchlist | components + 2 views | ✅ |
| **P134** | 数字/百分比格式工具扩展 + `<MetricStat>` 组件 + Dashboard 格式函数收敛 | `utils/format.ts` + Dashboard | ✅ |
| **P135** | 剪贴板工具 + `<CopyButton>` + 订单号复制（TradeHistory / DecisionTimeline） | `utils/clipboard.ts` + 2 views | ✅ |
| **P136** | 断连/恢复反馈 toast（`realtimeStatus` 转换 watcher） | App.vue | ✅ |
| **P137** | 各页「更新于」刷新时间戳（Dashboard 行情 / Reports 报告） | Dashboard + Reports | ✅ |
| **P138** | 复制健康快照（报障）按钮 | App.vue 健康弹窗 | ✅ |

**设计要点：**
- **连接上提**：`useStatusStream` 的 WS/轮询所有权从 Dashboard 组件上提到 `useConnectionHealth` 模块单例，在 App shell 启动、全页常驻；Dashboard 改为消费单例。保留 Cypress `polling` 短路与 3s 轮询回退、`realtimeStatus` 枚举不变。删除 `useStatusStream.ts`。
- **数据年龄**：单例 `lastDataAt`（WS 帧 / 成功轮询 / 手动刷新时更新）+ 1s ticker 派生 `ageSeconds`，P130 徽标 / P131 水印 / P138 快照共用。
- **会话横幅 vs SessionClockPanel**：后者展示下次开盘倒计时；前者仅在非 RTH 提示「行情可能延迟」，可关闭，dismiss 按阶段存 localStorage（阶段变化会重新弹出）。
- **共享基础件**：`DataState` / `MetricStat` / `CopyButton` 为受控展示件，无新依赖；数字格式函数收敛进 `utils/format.ts`（输出与 Dashboard 原本地实现逐字一致）。
- **剪贴板降级**：`navigator.clipboard` 不可用时回退 `execCommand`，失败不抛。
- **toast 防抖**：仅在 `realtimeStatus` 真正转换时触发（掉线→warning，恢复→success），且仅在发生过掉线后才报恢复；`polling` 稳态静默。

**验证：** `vue-tsc` 0 errors、`npm run build` 通过；`build:check-chunks`（38 chunks，max 512 KB）、`build:check-element-plus`（7 chunks）均不退步；新增 3 个 Cypress spec（`connection_health` / `session_banner` / `clipboard_copy`）。后端 `pytest` 不受影响（纯前端）。本机无法 headless 运行 Cypress，按既有约定仅类型/构建校验，spec 交 CI。

**显式 YAGNI 未做：** WS 消息级 QoS / 丢帧检测、按渠道分别健康、健康历史落库、PWA 离线健康缓存、跨设备健康同步。

---

## 近期已完成迭代 (2026-06-19) — Dashboard 深度 + 跨页 UX（10 轮 P119–P128）

> 自主 feature 迭代第 13 批（10 轮）。混合 Dashboard 面板派生统计、Backtest 深度与跨页 UX（快捷键 / 深色模式 / URL 深链接 / 书签备份）。全部纯前端，复用既有 API 响应字段，**不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径**。

| 代号 | 主题 | 页面 / 组件 | 状态 |
|------|------|-------------|------|
| **P119** | Dashboard 运行诊断快照 CSV 导出 | Dashboard | ✅ |
| **P120** | 持仓浮盈派生统计（盈/亏数、最大贡献、集中度）+ 持仓 CSV | PositionPnlPanel | ✅ |
| **P121** | 交易时段下次开盘 1s 实时倒计时 | SessionClockPanel | ✅ |
| **P122** | 权益曲线派生统计（峰值/谷值/区间回报/最佳最差日） | EquityCurvePanel | ✅ |
| **P123** | 标的归因派生期望（realized/笔数）+ 盈亏表现标注 | SymbolAttributionPanel | ✅ |
| **P124** | 回测对比表 CSV 导出 + 保存 run 搜索 + 盈亏平衡费率插值 | Backtest | ✅ |
| **P125** | 全局键盘快捷键导航（字母键切路由 + ? 帮助，输入框聚焦时忽略） | App | ✅ |
| **P126** | 决策时间线书签 JSON 导入导出（localStorage 备份/合并） | DecisionTimeline | ✅ |
| **P127** | 深色模式切换（Element Plus dark css-vars + localStorage 持久化） | App | ✅ |
| **P128** | 交易历史往返筛选 URL 深链接（query 同步/水合，可分享） | TradeHistory | ✅ |

**设计要点：**
- **派生统计**：四个 Dashboard 面板（持仓/时段/权益/归因）全部新增客户端 `computed`，复用已加载响应字段，不发新请求。
- **共享 CSV 工具**：继续复用 `utils/csv.ts`，Dashboard 诊断、持仓、对比表均走同一导出路径。
- **盈亏平衡费率**：对 `fee_sensitivity` 相邻点线性插值求 pnl 跨零处，纯客户端。
- **全局快捷键**：`window` keydown 监听，输入框/textarea/select/contenteditable 聚焦或带修饰键时跳过，`?` 打开帮助；路由用 hash。
- **深色模式**：`main.ts` 显式导入 `element-plus/theme-chalk/dark/css-vars.css`，切换 `html.dark` 并持久化。
- **URL 深链接**：TradeHistory 筛选状态防抖写入 `route.query`，挂载时水合；hash 路由下 query 在 hash 内。
- **回归修复**：新增全局 equity/pnl-by-symbol stub 使面板在测试中常显数据；App 导航栏改 `flex-wrap + height:auto`，避免窄视口下操作按钮被 `el-main` 遮挡 / 末位菜单链接被按钮覆盖。

**验证：** `vue-tsc` 0 errors、`npm run type-check` 通过；Cypress 全量 **216** 项全部通过（5:12）；`pytest tests/` **1178 passed, 1 skipped**（覆盖率 **89%**，无回归）。

**显式 YAGNI 未做：** 服务端主题偏好同步、快捷键自定义、列显示/隐藏持久化、深色模式图表配色微调、URL 深链接覆盖更多页面。

---

## 近期已完成迭代 (2026-06-19) — 全域只读可观测性与本地导出（10 轮 P109–P118）

> 自主 feature 迭代第 12 批（10 轮）。延续低风险前端路线：全部为复用既有 API 响应字段的客户端派生增强，**不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径**。新增共享 `utils/csv.ts`（无依赖 CSV 构造 + UTF-8 BOM 下载）。

| 代号 | 主题 | 页面 | 状态 |
|------|------|------|------|
| **P109** | Watchlist 派生价差列 + 评分过期徽标 + 当前快照 CSV 导出 | Watchlist | ✅ |
| **P110** | Experiments 展开 run 完整参数/错误 + 状态(完成/失败)页内过滤 + 当前页 CSV | Experiments | ✅ |
| **P111** | Lab 预算用量进度条 + Symbol 状态/LLM 交互客户端 CSV 导出 | Lab | ✅ |
| **P112** | Reports 每日明细列排序 + 日 PnL 一致性(均值/标准差/稳定性) + 本地 CSV 导出 | Reports | ✅ |
| **P113** | DecisionTimeline 本页汇总 chips + payload 展开行 + 列排序 + 按行标的快速过滤 | DecisionTimeline | ✅ |
| **P114** | TradeHistory 往返 CSV 导出 + 成交质量滑点标签 + 「仅看有笔记」过滤 | TradeHistory | ✅ |
| **P115** | Strategy LLM 建议/应用/配置一致性提示 + 区间毛利-费用估算 + LLM 交互 CSV/成功率 | Strategy | ✅ |
| **P116** | Credentials 严重度覆盖矩阵 + 通知渠道 JSON 导入导出 + dirty diff 摘要 | Credentials | ✅ |
| **P117** | AlertRules 规则 JSON 导出/批量导入（复用 create 端点，只增不删） | AlertRules | ✅ |
| **P118** | NotificationCenter 严重度分布柱图 + 成功/失败比率（当前页派生） | NotificationCenter | ✅ |

**设计要点：**
- **只读派生**：所有新增信息来自已加载响应的 `computed`，不发写请求、不发新请求；分页语义明确标注「当前页/当前已加载」。
- **共享 CSV 工具**：`utils/csv.ts` 的 `buildCsv`/`downloadCsv` 被 6 个页面复用，统一 RFC 4180 转义 + CJK BOM。
- **覆盖矩阵**：按 `severity_floor` 推导 INFO/WARNING/CRITICAL 是否有渠道接收，缺失级别显示告警，避免某级告警静默丢失。
- **一致性提示**：Strategy 比对 LLM `current_suggestion` / `applied_values` / 表单三态，显式「建议未应用 / 配置已偏离」。
- **导入语义**：AlertRules/通知渠道 JSON 导入为只增合并，逐条报错不中断，不覆盖既有规则。
- **滑点归一**：TradeHistory 滑点按买卖方向归一为「正=不利」，跨方向颜色一致。

**验证：** `vue-tsc` 0 errors、`npm run type-check` 通过；Cypress 全量 **197** 项全部通过；`pytest tests/` **1178 passed, 1 skipped**（覆盖率 **89%**，无回归）。

**显式 YAGNI 未做：** 新后端聚合端点、跨页全量统计、服务端 PDF 导出、规则导入冲突合并策略、Webhook 模板服务端校验。

---

## 近期已完成迭代 (2026-06-18) — 复盘/回测/通知闭环（5 轮 P104–P108）

> 自主 feature 迭代第 11 批（5 轮）。覆盖复盘风险可视化、策略配置完整互通、凭证模板提示、回测结果导出、通知失败重试，前后端各新增一个端点，不改动 runner/order/risk。

| 代号 | 主题 | 页面 / 端点 | 状态 |
|------|------|-------------|------|
| **P104** | Review 风险历史趋势图：风险历史 sparkline + 关键状态标注 | Review | ✅ |
| **P105** | Strategy 完整配置 JSON 导入导出：导出全部可配字段、导入后一键保存 | Strategy | ✅ |
| **P106** | Credentials Webhook Payload 模板变量提示与实时预览 | Credentials | ✅ |
| **P107** | Backtest 结果 CSV 导出：多 section CSV（参数/指标/交易/权益/跳过/费用敏感性） | `POST /api/backtest/export` | ✅ |
| **P108** | NotificationCenter 失败通知重试按钮：详情弹窗 + 卡片一键重发 | `POST /api/notifications/{id}/retry` | ✅ |

**新增端点：** `POST /api/backtest/export`（回测结果多 section CSV 导出）、`POST /api/notifications/{id}/retry`（使用当前凭证重发并原地更新状态）。

**设计要点：**
- **Review 风险历史**：复用 `RiskHistoryPanel`，在复盘页以卡片形式展示风险指标时序与 sparkline。
- **完整策略配置 JSON**：`buildStrategyConfig` 聚合表单全部字段，导入后通过深拷贝替换表单并触发保存可用状态。
- **Webhook 模板预览**：`previewTemplate` 替换 `{title}/{content}/{severity}/{timestamp}/{source}`，实时渲染示例，Cypress 通过预置 webhook 渠道模板绕过 `el-textarea` 输入事件转发问题。
- **回测 CSV 导出**：后端用标准 `csv` 模块生成含 BOM 的多 section CSV；前端通过 Blob + Content-Disposition 解析文件名触发下载。
- **通知重试**：后端用当前 `CredentialConfig` 构造 `MultiChannelNotifier`，重发后原地更新原 `NotificationLog` 的 `success`/`error`/`created_at`；前端在卡片与详情弹窗均提供重试入口，成功后刷新列表与未读 badge。

**验证：** `vue-tsc` 0 errors、`npm run type-check` 通过；`pytest tests/` **1178 passed, 1 skipped**，覆盖率 **89%**；Cypress 全量 **168** 项全部通过。

**显式 YAGNI 未做：** 通知重试审计日志、回测导出为 Excel/多 sheet、webhook 模板服务端语法校验、策略配置 JSON Schema 版本号。

---

## 近期已完成迭代 (2026-06-17) — TradeHistory 分析复盘增强（10 轮 P94–P103）

> 自主 feature 迭代第 10 批（10 轮）。继续只读前端增强：复用既有 `/api/trades` 与 `/api/trades/analytics/*` 响应，不新增后端 API、不新增表、不触碰 broker/order/runner/risk 写路径。规格：[2026-06-17-p94-p103-tradehistory-analytics-polish-design.md](superpowers/specs/2026-06-17-p94-p103-tradehistory-analytics-polish-design.md)。计划：[2026-06-17-p94-p103-tradehistory-analytics-polish.md](superpowers/plans/2026-06-17-p94-p103-tradehistory-analytics-polish.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P94** | 往返摘要：当前筛选结果、胜/败、净盈亏、费用、平均净盈亏 | ✅ |
| **P95** | 往返快捷过滤：全部、胜、败、多、空 | ✅ |
| **P96** | 往返 symbol 搜索：当前加载 round trips 本地过滤 | ✅ |
| **P97** | 往返洞察：最佳/最差 filtered round trip | ✅ |
| **P98** | 往返展开详情：entry/exit order id、时间、gross/net、费用拖累 | ✅ |
| **P99** | 交易日历洞察：最佳日、最差日、最活跃日 | ✅ |
| **P100** | 持仓时长洞察：最佳/最差非空 bucket | ✅ |
| **P101** | 盈亏分布平衡：亏损/盈利 bucket 数与净 PnL 平衡 | ✅ |
| **P102** | 月度趋势洞察：最新月、最佳月、最大回撤月 | ✅ |
| **P103** | 星期归因洞察：最佳/最差星期 | ✅ |

**设计要点：**
- **当前加载数据语义**：round-trip filters/search/summary 只作用于当前已加载的 200 条往返数据，避免暗示全量聚合。
- **只读派生**：全部新增值由已加载 response `computed` 派生，无新请求、无写操作。
- **渐进增强**：保留既有 round-trip 表格和 5 个 analytics 卡片，只新增上方摘要/洞察与展开详情。
- **TDD 覆盖**：先新增 Cypress RED，再实现 GREEN；覆盖 filters/search/empty match/expand detail 与 5 类 analytics insights。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/trade_roundtrips.cy.ts,cypress/e2e/history.cy.ts` 11 passed；`npm run type-check` 通过。

**显式 YAGNI 未做：** 后端聚合、跨页全量搜索、导出 analytics、图表库、持久化筛选、修改配对算法。

---

## 近期已完成迭代 (2026-06-17) — Review 复盘明细可观测性（5 轮 P89–P93）

> 自主 feature 迭代第 9 批（5 轮）。继续只读前端增强：复用既有 `/api/review` 响应中的 day/order/event/snapshot 字段，不新增后端 API、不改导出、不触碰 broker/order/runner/risk 写路径。规格：[2026-06-17-p89-p93-review-detail-observability-design.md](superpowers/specs/2026-06-17-p89-p93-review-detail-observability-design.md)。计划：[2026-06-17-p89-p93-review-detail-observability.md](superpowers/plans/2026-06-17-p89-p93-review-detail-observability.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P89** | Day composition strip：每日 LLM / 订单 / 事件 / 快照 / 错误计数 | ✅ |
| **P90** | Day state badges：盈利/亏损/打平、有交易/无交易、有错误/无错误 | ✅ |
| **P91** | Order fill detail：broker id、成交数量、成交价、成交时间 | ✅ |
| **P92** | Event payload preview：从 `payload_json` 派生紧凑 payload 摘要 | ✅ |
| **P93** | Snapshot delta context：触发价、价格相对触发价 delta、连亏次数、快照时间 | ✅ |

**设计要点：**
- **只读派生**：所有新增信息来自 `ReviewResponse` 已有字段，无新增请求。
- **行内增强**：保留原有 Review timeline 结构，只补充 day-level 与 row-level 可解释细节。
- **防御展示**：缺失成交/无效 payload/无触发价时显示 `-` 或 `payload -`，不阻断页面。
- **时区稳定测试**：成交时间在新详情中用 ISO 分钟片段展示，避免本地时区导致 E2E 不稳定。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/review_runtime_history.cy.ts` 3 passed；`npm run type-check` 通过；`npm run build` 通过。

**显式 YAGNI 未做：** 新后端复盘聚合、payload JSON 展开编辑器、订单详情弹窗、跨日排序/筛选、图表改造。

---

## 近期已完成迭代 (2026-06-17) — 告警与通知运维可观测性（10 轮 P79–P88）

> 自主 feature 迭代第 8 批（10 轮）。继续低风险只读前端增强：复用既有 `/api/notifications`、`/api/alert-rules` 与 `/api/alert-rules/{id}/history`，不新增后端 API、不新增表、不改通知发送/告警评估/交易路径。规格：[2026-06-17-p79-p88-alerts-notifications-observability-design.md](superpowers/specs/2026-06-17-p79-p88-alerts-notifications-observability-design.md)。计划：[2026-06-17-p79-p88-alerts-notifications-observability.md](superpowers/plans/2026-06-17-p79-p88-alerts-notifications-observability.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P79** | 通知级别摘要：当前页总数、成功/失败、INFO/WARNING/CRITICAL 计数 | ✅ |
| **P80** | 通知搜索：标题/内容/错误/级别的本地关键字过滤 | ✅ |
| **P81** | 通知快捷过滤：全部、失败、CRITICAL、WARNING、INFO | ✅ |
| **P82** | 通知按日分组：当前筛选结果按 API 日期归组展示 | ✅ |
| **P83** | 通知空状态/结果文案：显示当前页与筛选后数量，空结果可解释 | ✅ |
| **P84** | 告警规则健康卡：总数、启用、停用、最近触发、从未触发 | ✅ |
| **P85** | 告警规则快捷过滤：启用/停用/最近触发/从未触发 | ✅ |
| **P86** | 最近触发规则摘要：按 `last_fired_at` 展示最近触发规则 | ✅ |
| **P87** | 触发历史统计：次数、最新触发值、平均触发值、最大触发值 | ✅ |
| **P88** | 触发历史严重度摘要：按严重度计数并保留消息上下文 | ✅ |

**设计要点：**
- **只读派生**：全部新增能力在前端从已加载数据 `computed` 派生，不发写请求。
- **分页语义明确**：通知统计标注为当前页/当前筛选结果，避免暗示全量聚合。
- **测试驱动**：先写 Cypress RED，确认缺失 UI 失败，再实现 GREEN。
- **时区稳定**：通知按 API 日期前缀归组，避免本地时区让 UTC 晚间通知跨日。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/notification_center.cy.ts,cypress/e2e/alert_firings.cy.ts` 4 passed；`npm run type-check` 通过；`npm run build` 通过。

**显式 YAGNI 未做：** 通知重发、通知确认/删除、告警静音、后端聚合统计、WebSocket 实时推送、跨页全量通知统计。

---

## 近期已完成迭代 (2026-06-17) — Lab LLM 运维可观测性（5 轮 P74–P78）

> 自主 feature 迭代第 7 批（5 轮）。延续低风险只读前端路线：复用既有 `/api/strategy/llm-interval/status` 与 `/api/strategy/llm-interval/interactions`，不新增后端 API、不改调度/预算算法、不触发 analyze/preview/enable/disable。规格：[2026-06-17-p74-p78-llm-observability-design.md](superpowers/specs/2026-06-17-p74-p78-llm-observability-design.md)。计划：[2026-06-17-p74-p78-llm-observability.md](superpowers/plans/2026-06-17-p74-p78-llm-observability.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P74** | 运行总览：启用状态、分析间隔、最近/下次分析时间、当前建议与应用值 | ✅ |
| **P75** | 预算仪表：每轮 symbol、每小时上限、跟踪标的、有效预算、已用/剩余次数 | ✅ |
| **P76** | Symbol 状态：主标的、挂单、最近/下次分析、最近状态与跳过原因 | ✅ |
| **P77** | 最近交互：analyze/preview 记录、成功/失败、应用状态、订单动作、错误信息 | ✅ |
| **P78** | 健康提示：预算耗尽、未启用、缺少下次分析、symbol 跳过/挂单等只读提醒 | ✅ |

**设计要点：**
- **Lab 新页签 + lazy-load**：新增「运行状态」tab，只有用户打开时才请求 LLM runtime 数据。
- **只读可观测性**：全部信息来自现有 status/interactions 响应；不暴露写操作，不改变 LLM 调度。
- **运维优先信息架构**：总览/预算/健康提示优先，symbol 明细与最近交互向下展开，便于排查「为什么没分析/没下单」。
- **无新依赖**：Element Plus 表格、统计卡、alert；不引入图表库或全局状态。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/lab.cy.ts` 5 passed；`npm run type-check` 通过；`npm run build` 通过。

**显式 YAGNI 未做：** 新后端 API、WebSocket 实时推送、prompt diff、重新执行 LLM、预算算法调整、跨页面共享运行状态。

---

## 近期已完成迭代 (2026-06-17) — Reports 只读增强（5 轮 P69–P73）

> 自主 feature 迭代第 6 批（5 轮）。延续低风险前端增强路线：复用既有 `/api/reports/range` 的 `metrics` / `daily_points` / `attribution` / `details` 响应，不改后端、不新增表、不触碰 broker/order path。规格：[2026-06-17-p69-p73-reports-readonly-enhancement-design.md](superpowers/specs/2026-06-17-p69-p73-reports-readonly-enhancement-design.md)。计划：[2026-06-17-p69-p73-reports-readonly-enhancement.md](superpowers/plans/2026-06-17-p69-p73-reports-readonly-enhancement.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P69** | 快捷区间：Reports 增加近 7/30/90 天快捷查询 | ✅ |
| **P70** | 交易归因：展示归因标签、交易数、PnL、胜率、占比 | ✅ |
| **P71** | 每日 drill-down：每日明细可展开订单行 | ✅ |
| **P72** | 报告洞察：最佳日、最差日、盈利/亏损日、最大回撤日 | ✅ |
| **P73** | 导出/空状态 polish：当前查询范围、导出文件名预览、归因/明细空状态 | ✅ |

**设计要点：**
- **只读派生**：所有新增 UI 均由 `ReportResponse` 已有字段派生，无新增请求。
- **快捷查询不绕过校验**：快捷区间复用 `handleSearch()`，保持现有表单校验和错误反馈。
- **明细按日展开**：每日表通过 Element Plus expandable row 展示 `details[].orders`，空日显示明确空状态。
- **轻量复盘摘要**：洞察卡片用 `computed` 从 `daily_points` 派生，不引入图表库。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/reports.cy.ts` 6 passed；`npm run type-check` 通过；`npm run build` 通过。

**显式 YAGNI 未做：** 后端 CSV 明细模式、报表模板保存、跨标的组合报告、独立 BI 页面、通知推送。

---

## 近期已完成迭代 (2026-06-17) — Trade Analytics 前端补齐（5 轮 P64–P68）

> 自主 feature 迭代第 5 批（5 轮）。选型为低风险前端补齐：后端 `/api/trades/analytics/*` 只读端点已存在，本批不改后端、不碰 runner/order/risk、不新增表。规格：[2026-06-17-p64-p68-trade-analytics-frontend-design.md](superpowers/specs/2026-06-17-p64-p68-trade-analytics-frontend-design.md)。计划：[2026-06-17-p64-p68-trade-analytics-frontend.md](superpowers/plans/2026-06-17-p64-p68-trade-analytics-frontend.md)。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P64** | 交易日历：按平仓日展示交易数、标的、净 PnL | ✅ |
| **P65** | 持仓时长分布：按 `<5m / 5m-1h / 1h-1d / 1d-1w / >=1w` 展示交易数、胜率、净 PnL | ✅ |
| **P66** | 盈亏分布：按亏损/打平/盈利区间展示交易数和净 PnL | ✅ |
| **P67** | 月度汇总：展示月度交易数、胜率、累计 PnL、回撤 | ✅ |
| **P68** | 星期归因：展示 Mon–Sun 的交易数、胜率、净 PnL | ✅ |

**新增前端能力：** `frontend/src/api/trades.ts` 增加 5 个 typed API client；`frontend/src/types/index.ts` 增加对应响应类型；TradeHistory 新增默认折叠的「交易分析（只读）」区；Cypress `history.cy.ts` 覆盖 5 个卡片。

**设计要点：**
- **默认折叠 + lazy-load**：History 首屏不立即触发 5 个聚合请求；用户展开「交易分析（只读）」时再加载。
- **辅助信息不阻塞主表**：「拉取」按钮只跟随往返成交表 loading；analytics 有独立 loading，且只在分析区展开时刷新。
- **局部容错**：5 个 analytics 请求用 `Promise.allSettled` 独立更新，一个端点失败不影响其他卡片。
- **轻量 UI**：不引入图表库；使用 Element Plus + CSS grid + 简短列表，保持 TradeHistory 主订单表优先级。

**验证：** `npm run cypress:run -- --config baseUrl=http://127.0.0.1:3000 --spec cypress/e2e/history.cy.ts` 7 passed；`npm run type-check` 通过；`npm run build` 通过。

**显式 YAGNI 未做：** 独立导航页、高级图表库、导出、实时 WebSocket 推送、后端聚合算法调整、跨页面共享筛选状态。

---

## 近期已完成迭代 (2026-06-16) — 已实现盈亏分析簇 + 告警触发历史（5 轮 P59–P63）

> 自主 feature 迭代第 4 批（5 轮）。judge-panel workflow（4 只读探索 agent + 打分）选型；偏离两处以降风险：① 往返成交改**只读**（不落库透写缓存，遵循本仓库「读时重算」约定）② 第 5 轮用**告警触发历史**替换「净/毛切换」（更独立、零实盘风险；净 PnL 已内建进配对服务）。基线 `pytest 1091 passed` → 交付后 `pytest 1143 passed`（**+52，0 回归**），`basedpyright` 新增代码 0/0/0，`vue-tsc` 0，build pass，chunk 预算不退步。规格：[2026-06-16-realized-pnl-analytics-cluster-design.md](superpowers/specs/2026-06-16-realized-pnl-analytics-cluster-design.md)。

**共享基础：** `DailyPnlService.pair_round_trips()` —— 复用既有 `_fill_from_order` 的 FIFO entry↔exit 配对（**不触碰** `calculate()`/`_apply_fill`，零风控回归），每笔平仓 fill 产出一个 `ClosedRoundTrip`（加权入场均价 / 首入场时间 / `est_fees` via `app/core/fees.py` / `net_pnl` / `holding_seconds`）。P60–P62 全在此返回上做纯聚合。只读、不落库、不下单。

| 代号 | 主题 | 端点 | 状态 |
|------|------|------|------|
| **P59** | 往返成交（lot 级 FIFO entry↔exit 配对 + 净/毛 + 持仓时长） | `GET /api/trades` | ✅ |
| **P60** | 交易统计 + 连胜/连亏 / 期望 / 盈亏比 / 最长连胜败 | `GET /api/trades/stats` | ✅ |
| **P61** | 权益曲线（账户级累计已实现 PnL + 回撤，常驻 Dashboard） | `GET /api/equity/curve` | ✅ |
| **P62** | 按标的归因（组合级已实现 PnL / 胜率 / 贡献占比） | `GET /api/pnl/by-symbol` | ✅ |
| **P63** | 告警触发历史（append-only `alert_firings` + 每规则时间线） | `GET /api/alert-rules/{id}/history`、`GET /api/alert-firings` | ✅ |

**新增端点：** `GET /api/trades`、`/api/trades/stats`、`/api/equity/curve`、`/api/pnl/by-symbol`、`/api/alert-rules/{id}/history`、`/api/alert-firings`。**新增表：** `alert_firings`（`_ensure_alert_firings_table`，无 FK，删规则留历史）。**新增纯聚合服务：** `trade_stats_service` / `equity_curve_service` / `symbol_attribution_service`。**新增前端：** TradeHistory 往返表 + 统计条、Dashboard `EquityCurvePanel` + `SymbolAttributionPanel`、AlertRules 触发历史弹窗。

**设计要点：**
- **读时重算不落库**：`pair_round_trips` 每次请求 replay fills（个人交易量级，微秒级），避免写透缓存的一致性负担与 fill 路径风险；与 `DailyPnlService.calculate` / `/api/metrics/summary` 同约定。
- **净/毛内建**：`ClosedRoundTrip` 同时带 `gross_pnl`/`est_fees`/`net_pnl`；费率读活跃 `StrategyConfig`（`_active_fee_rates`），**修复一处 falsy-or bug**：`getattr(x,"fee_rate_us",None) or 0.0005` 会把合法 `0.0`（禁用费率）当 falsy 折成默认 0.0005，改为仅在 `None` 时回退。
- **win/loss 按 net_pnl 分类**：streak 在平仓时间序列上跑 run；breakeven（恰好 0）既非胜也非败、打断当前 run。
- **告警触发记录隔离**：`evaluate` 先提交 `last_fired_at`（主事务），再逐条 best-effort `_record_firing`（独立 commit+rollback），firing 写失败绝不污染规则更新事务。
- **去重**：`/api/trades` 粗代理 metrics/summary 无逐笔行；`/api/reports` 单标的按 side；`/api/equity/curve` 账户级常驻（vs Reports 单标的查询态）；`/api/pnl/by-symbol` 组合级 symbol 维度（vs 报告 side 维度）。

**显式 YAGNI 未做：** 往返成交落库（透写缓存）、按渠道分别记录通知、walk-forward 落库、净值曲线含未实现浮盈叠加、告警规则复合条件、Cypress 本机 headless 运行（spec 已编写，仅类型/构建校验）。

---

## 近期已完成迭代 (2026-06-16) — 回测参数扫描 (P43)

> 自主 feature 迭代（ultracode judge-panel 选型：trader-value 10 / eng-fit 10 / 36 分胜出）。基线 `pytest 954 passed, 2 failed`（pre-existing `test_watchlist_score`）→ 交付后 `pytest 988 passed, 2 failed`（**+34，0 回归**），`basedpyright` 0/0/0，`vue-tsc` 0，build pass，chunk 预算不退步。规格：[2026-06-16-backtest-parameter-sweep-design.md](superpowers/specs/2026-06-16-backtest-parameter-sweep-design.md)。

**目标：** 把回测从「验证单一配置」升级为「找到好配置」——在当前 CSV 上对区间/盈利参数做网格搜索，按风险调整收益排名，直接回答「该用什么 `buy_low` / `sell_high` / `min_profit`」。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `sweep_backtest()` 纯函数：Cartesian product 网格搜索，复用 `BacktestEngine._validate_params` 跳过无效组合，按 Sharpe/Sortino/Calmar/PF/总回报 排名（`None` 排末），生成 `(buy_low, sell_high)` 最优热力图 | ✅ |
| **T2** | `POST /api/backtest/sweep` + schemas；`BacktestMetrics` 补齐 `sortino_ratio`/`calmar_ratio`；`/run` 与 `/sweep` 统一返回完整 5 项比率指标 | ✅ |
| **T3** | 前端 Backtest.vue 参数扫描区：网格输入 + 排名表（Top-20）+ 热力图，点击行回填表单；`runBacktestSweep` + TS 类型 | ✅ |
| **T4** | 测试：`test_backtest_sweep.py`（引擎 + API + schema，31 项）+ Cypress `backtest.cy.ts` 扫描用例 | ✅ |

**新增端点：**
- `POST /api/backtest/sweep` —— 即时、离线、纯内存参数扫描；`base` + `grid`（复用实验网格 `value`/`values`/`range`）+ `sort_by` + `max_combinations`（默认 2000、上限 10000）。422 = 网格超限 / 参数非法。

**设计要点：**
- **纯离线**：`app/core/backtest.py` 仅引入 `csv/io/dataclasses/datetime/typing/itertools`，零 SQLAlchemy / broker / runner 耦合，零实盘下单风险。
- **无新表 / 无迁移**：扫描结果是响应体瞬态分析，不落库（区别于 DB 持久化、异步轮询的「实验」系统）。
- **无效组合静默跳过**：`buy_low ≥ sell_high` 等组合经 `_validate_params` 拒绝后计入 `skipped_count`，不报错（镜像 `ExperimentGridService.expand` 策略）。
- **行 `params` 返回原始 dict**：网格可能把 `fee_rate`/`slippage_pct` 推到 `BacktestParams` 显示上限之外（引擎接受、schema 拒绝），故返回引擎实际执行的原始值。

**与「实验（StrategyExperiment）」区分：** 扫描 = 即时扫描当前 CSV（in-memory）；实验 = 保存并批量回测（DB-backed、异步、可跨日）。

**显式 YAGNI 未做：** 持久化「最优配置」、Bayesian/SMAC 代理模型优化、并发内层循环、walk-forward 样本外验证、每组合 equity_curve（点击行走 `/run` 取完整曲线）。

---

## 近期已完成迭代 (2026-06-16) — 通知中心 / 通知分发日志 (Notification Log) (P58)

> 自主 feature 迭代第 16 轮（第三批 5 轮最后）。让 P5/P9/P47 的通知可审计。`pytest +7`（1084→1091），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 每条发出的通知（风控 / 告警 / 日报）都留痕——回答「系统到底推过哪些通知、哪些失败了」。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `NotificationLog` 模型 + `_ensure_notifications_table` + `NotificationLogService`（list + severity 过滤分页）+ `NotificationLogSink`（session_factory，best-effort 吞异常）+ `get_notification_sink` 单例 | ✅ |
| **T2** | `MultiChannelNotifier` 加可选 `sink`（`__init__`/`from_credential_config` 透传，`send` 后调用）；runner 构建 notifier 时注入 `sink=get_notification_sink().record` | ✅ |
| **T3** | `GET /api/notifications` + 前端新页「通知中心」（`/notifications` 路由 + 导航，级别筛选 + 分页）+ api + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/notifications`。**新增表：** `notifications`。

**设计要点：**
- **单一切点**：sink 挂在 `MultiChannelNotifier.send`（所有通知的唯一出口），runner 注入一次即覆盖风控/告警/日报全部来源。
- **best-effort 不阻断**：sink 写库失败仅 `logger.debug`，绝不影响真实通知发送（通知 > 日志）。
- **core 不依赖 DB**：sink 以 `Callable` 注入，`MultiChannelNotifier` 仍零 SQLAlchemy 耦合。
- **失败也记录**：`success=False` + `error`，方便排查渠道故障。

**显式 YAGNI 未做：** 按渠道分别记录、通知重发按钮、保留期自动清理、WebSocket 实时推送通知、按标的过滤。

---

## 近期已完成迭代 (2026-06-16) — 交易时段时钟 (Market Session Clock) (P57)

> 自主 feature 迭代第 15 轮（第三批 5 轮之一）。`pytest +8`（1076→1084），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 一眼看清当前是 RTH / 盘前 / 盘后 / 午休 / 休市——避免在非交易时段误判策略为何不动。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `market_calendar.session_status(market, instant)`（`rth`/`pre`/`post`/`lunch`/`closed`，复用 `is_rth` + 节假日 + 午休） | ✅ |
| **T2** | `GET /api/calendar/session?symbol=`（按后缀推断市场，返回 status + 本地时间 + 下次开盘）+ `MarketSessionStatus` schema | ✅ |
| **T3** | 前端独立组件 `SessionClockPanel`（彩色状态徽章 + 本地时间 + 下次开盘，每 60s 刷新，`symbol` prop）嵌入 Dashboard + api + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/calendar/session`。**无新表**（纯计算）。

**设计要点：**
- **复用既有日历**：`is_rth`（已含午休/节假日/周末）+ `next_session_open`，零新日历逻辑。
- **状态优先级**：休市日 → rth → 午休 → 盘前 → 盘后；HK 午休窗口 `[12:00,13:00)`，13:00 即恢复 RTH。
- **市场推断**：`.HK` → HK，否则 US（与 `market_for_symbol` 一致）。
- **轮询 60s**：时段变化慢，低频刷新省请求。

**显式 YAGNI 未做：** 多市场并排时钟、精确扩展交易时段（4:00-9:30/16:00-20:00）窗口、到开盘倒计时、节假日名称展示。

---

## 近期已完成迭代 (2026-06-16) — LLM 交互详情 (LLM Interaction Detail) (P56)

> 自主 feature 迭代第 14 轮（第三批 5 轮之一）。`pytest +4`（1072→1076），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 决策时间线里看到 LLM 事件后，一键查看完整 prompt / 原始响应 / 解析结果 / 上下文快照，便于调优 prompt。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `LLMInteractionService.get_detail(id)`（含 JSON 文本列防御式解析）+ `LLMInteractionDetail` schema（含 prompt/raw_response/parsed/context） | ✅ |
| **T2** | `GET /api/llm-interactions/{id}`（404 缺失） | ✅ |
| **T3** | 前端 DecisionTimeline llm 行「详情」按钮 + 弹窗（`<details>` 折叠 prompt/响应/解析/上下文）+ `getLLMInteraction` + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/llm-interactions/{id}`。**无新表**（读既有 `llm_interactions`）。

**设计要点：**
- **轻量列表 vs 重详情分离**：列表响应（`LLMInteractionResponse`）仍不带 prompt/响应（省带宽），仅详情端点返回重字段。
- **JSON 文本列防御解析**：`parsed_response`/`context_snapshot` 存为 Text，`_json_loads_dict` 解析失败回退 `{}`。
- **`<details>` 折叠**：默认只展开 prompt，原始响应/解析/上下文按需展开，避免长内容撑爆弹窗。

**显式 YAGNI 未做：** prompt diff（版本对比）、重新执行、token 用量统计、导出。

---

## 近期已完成迭代 (2026-06-16) — 交易笔记分析 (Trade Journal Analytics) (P55)

> 自主 feature 迭代第 13 轮（第三批 5 轮之一）。基于 P44 交易笔记做只读聚合。`pytest +1`（1071→1072），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 把零散的交易笔记变成可读的复盘信号——笔记数、平均评分、评分分布、热门标签、覆盖标的数。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `TradeNoteService.analytics()`（总数 / 已评分 / 平均评分 / 评分分布 1-5 / 热门标签 top10 / 标的数）+ `TradeNoteAnalytics/TagCount` schema | ✅ |
| **T2** | `GET /api/trade-notes/analytics`（路由置于 `/{order_id}` 之前避免被 int 路径吞掉） | ✅ |
| **T3** | 前端 TradeHistory.vue 分析卡片（总数 / `el-rate` 平均 / 热门标签），保存/删除后自动刷新 + Cypress | ✅ |

**新增端点：** `GET /api/trade-notes/analytics`。**无新表**（读 P44 的 `trade_notes`）。

**设计要点：**
- **纯只读、内存聚合**：笔记量级小（个人交易），全表加载后 Python 聚合，零复杂 SQL。
- **路由顺序**：`/analytics` 必须声明在 `/{order_id}`（int）之前，否则被路径参数拦截。
- **保存/删除即刷新**：前端 upsert/delete 后重载笔记 + 分析，卡片始终最新。

**显式 YAGNI 未做：** 按标的/时段细分、评分与盈亏相关性、标签云可视化、笔记导出。

---

## 近期已完成迭代 (2026-06-16) — 行情 K 线拉取 (Broker Candles → Backtest) (P54)

> 自主 feature 迭代第 12 轮（第三批 5 轮之一）。连接实盘行情与离线回测。`pytest +5`（1066→1071），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 回测不再依赖手填 CSV——直接从券商拉真实 K 线填入回测框，回答「这段真实历史上我的策略会怎样」。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `GET /api/broker/candles`（`BrokerGateway.get_candlesticks` → 过滤无效 K 线 → `BacktestPricePoint[]` + 预格式化 `csv_text`）；period 白名单 + 422/503 语义 | ✅ |
| **T2** | 前端 Backtest.vue「从行情拉取」控件（标的 / 周期 / 数量 + 一键填入 CSV）+ `getBrokerCandles` + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/broker/candles`。**无新表。**

**设计要点：**
- **复用 `BrokerGateway.get_candlesticks`**（含分档重试），零新券商代码。
- **无效 K 线过滤**：`close<=0` 或 `high<low` 等直接丢弃，保证返回结果可直接喂回 `parse_backtest_csv`。
- **broker 不可用降级**：`runner.broker is None` 或 `get_candlesticks` 抛错 → 503（前端提示「券商可能未连接」）。
- **UTC CSV**：时间统一 `%Y-%m-%dT%H:%M:%SZ`，与示例 CSV 一致。

**显式 YAGNI 未做：** 调整方式（前复权/后复权）、跨周期合成、缓存、直接 `POST /backtest/from-broker`（当前拉取后由用户点「运行回测」）。

---

## 近期已完成迭代 (2026-06-16) — 每日风险历史 (Daily Risk History) (P53)

> 自主 feature 迭代第 11 轮（第二批 5 轮最后）。`pytest +5`（1061→1066），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 把风控状态可视化到时间轴——读 `runtime_state_snapshots`，展示日内盈亏趋势 + 连续亏损 + 暂停/熔断事件，回答「最近风控状态怎么走的」。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `RiskHistoryService`（读 `runtime_state_snapshots`，时序正序 + `latest`，limit 上限 500）+ `RiskHistoryPoint/Response` schema | ✅ |
| **T2** | `GET /api/risk/history?symbol=&limit=`（只读） | ✅ |
| **T3** | 前端独立组件 `RiskHistoryPanel`（日内盈亏 SVG 趋势线 + 最新值卡片 + 暂停/熔断标签）嵌入 Dashboard + api + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/risk/history`。**无新表**（读既有 `runtime_state_snapshots`）。

**设计要点：**
- **纯只读、无新表**：直接查既有快照表，零写入、零实盘风险。
- **SVG 趋势线自绘**：不引图表库，`viewBox` 内用 polyline + 零轴，轻量；`Math.min/max` 含 0 保证零线可见。
- **时序正序**：服务端按 `created_at desc` 取最近 N 条后 `reversed`，前端直接画。

**显式 YAGNI 未做：** 风险事件（暂停/熔断）独立时间线、回撤曲线、按交易聚合的风险归因、WebSocket 实时风险推送。

---

## 近期已完成迭代 (2026-06-16) — 策略参数预设 (Strategy Presets) (P52)

> 自主 feature 迭代第 10 轮（第二批 5 轮之一）。`pytest +4`（1057→1061），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 把常用策略配置存为命名预设（如「保守 / 激进」），一键切换，免去重复改表单。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `StrategyPreset` 模型 + `_ensure_strategy_presets_table`（name + params_json） | ✅ |
| **T2** | `StrategyPresetService`（create/list/get/get_params/delete，JSON 防御式解析）+ schema | ✅ |
| **T3** | `/api/strategy-presets` CRUD + `POST /{id}/apply`（取预设 params → `StrategyService.update_config`，写审计 `STRATEGY_PRESET_APPLY`，返回变更字段） | ✅ |
| **T4** | 前端 Strategy 页「参数预设」区（存当前表单 / 下拉选择应用 / 删除）+ api + 类型 + Cypress | ✅ |

**新增端点：** `/api/strategy-presets` CRUD + `/{id}/apply`。**新增表：** `strategy_presets`。

**设计要点：**
- **复用 `update_config`**：apply 直接把预设 params 喂给既有 `StrategyService.update_config`（含审计 diff、updatable_fields 校验），无新校验逻辑。
- **params 通用 dict**：存可更新策略字段子集（费率等已在客户端按 API 格式缩放），apply 端按字段名落库。
- **前端缩放一致**：存预设时按策略 PUT 同款 `/100` 缩放费率，避免精度错位。

**显式 YAGNI 未做：** 预设导入/导出、预设分类/标签、默认预设、跨账户同步。

---

## 近期已完成迭代 (2026-06-16) — 条件告警规则 (Conditional Alert Rules) (P51)

> 自主 feature 迭代第 9 轮（第二批 5 轮之一）。差异化维度 judge 第一（10/10）。`pytest +9`（1048→1057），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 让用户自定义阈值告警——价格上/下穿、日内亏损，触发后经既有通知渠道推送，无需手动盯盘。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `AlertRule` 模型 + `_ensure_alert_rules_table`（`rule_type`/`threshold`/`severity`/`cooldown_seconds`/`last_fired_at`） | ✅ |
| **T2** | `AlertRuleService`（CRUD + `evaluate(runner)`：价格类读 broker 行情、`daily_loss` 读 `runtime_state.daily_pnl`，冷却节流，broker/notifier 注入可测）+ schema | ✅ |
| **T3** | `/api/alert-rules` CRUD + `/evaluate`（写审计）+ `_alert_rules_cron` 后台任务（每 60s，镜像 LLM/报告 cron，挂 lifespan） | ✅ |
| **T4** | 前端新页「告警规则」（`/alerts` 路由 + 导航）：表格 + 新建/编辑弹窗 + 启停开关 + 立即评估 + Cypress | ✅ |

**新增端点：** `/api/alert-rules` CRUD + `/evaluate`。**新增表：** `alert_rules`。**新增 cron：** `_alert_rules_cron`（60s）。

**设计要点：**
- **避开热路径**：评估跑在独立后台 cron（非 `_on_quote`），只读 + 通知，不发单；judge eng-fit 原本因热路径扣分，此方案化解。
- **可测**：`evaluate` 注入 `runner`（broker + notifier），测试用 `FakeBroker`/`FakeNotifier`，无需真实行情。
- **冷却**：`last_fired_at + cooldown_seconds` 防刷屏；`evaluate` 返回 `evaluated/fired/skipped_cooldown` 计数。
- **行情缺失降级**：取不到 quote/state 时静默跳过（不抛、不触发）。

**显式 YAGNI 未做：** 复合条件（AND/OR）、跨标的组合、移动平均/技术指标触发器、告警历史落库（当前只记 `last_fired_at`）、WebSocket 实时推送告警。

---

## 近期已完成迭代 (2026-06-16) — 回测结果对比 (Backtest Run Comparison) (P50)

> 自主 feature 迭代第 8 轮（第二批 5 轮之一）。`pytest +6`（1042→1048），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 把不同参数的回测结果存为命名快照，多选后横向对比——回答「这几组参数谁更好」。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `BacktestRun` 模型 + `_ensure_backtest_runs_table`（name/symbol/params_json/metrics_json，不存 equity 曲线） | ✅ |
| **T2** | `BacktestRunService`（save/list/get/delete/compare，JSON 防御式回填）+ `BacktestRunSaveRequest/Out/Page/Compare` schema | ✅ |
| **T3** | `/api/backtest/runs` CRUD + `/runs/compare?ids=`（去重保序、上限 8）+ 前端 Backtest.vue「结果对比」区（保存/多选/转置表）+ Cypress | ✅ |

**新增端点：** `POST/GET /api/backtest/runs`、`GET /runs/compare`、`GET/DELETE /runs/{id}`。**新增表：** `backtest_runs`。

**设计要点：**
- **只存 params + metrics**：equity 曲线不持久化（要细节就再 `/run`），行体积小。
- **转置对比表**：前端 metric × run 横向表，后端只回原始 run，前端格式化。
- **compare 去重保序 + 上限 8**：避免重复 id 与过大对比表。
- **JSON 防御式回填**：`_to_out` 解析失败时回退默认 params/metrics，不抛。

**显式 YAGNI 未做：** equity 曲线叠加对比图、自动选最优快照、跨 symbol 对比、快照标签/分组。

---

## 近期已完成迭代 (2026-06-16) — 运维统一时间线 (Ops Unified Timeline) (P49)

> 自主 feature 迭代第 7 轮（第二批 5 轮之一）。`pytest +8`（1034→1042），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 单一时间线看全部运维事件——把 `llm_interactions` 与 `risk_events` 并入既有 `trade_events ∪ audit_logs` union，前端一键切换 5 个来源。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `event_list_service`：`SourceFilter` 扩展 `llm`/`risk`；`_llm_row_to_out` / `_risk_row_to_out` mapper；单源分支 + `all` 四表合并（沿用 fetch_n 上限防深翻页） | ✅ |
| **T2** | `TimelineEventResponse.source` Literal 扩展；`GET /api/events` 的 `source` pattern 扩展 `^(trade\|audit\|llm\|risk\|all)$` | ✅ |
| **T3** | 前端 DecisionTimeline 增 LLM / 风控 来源按钮 + `sourceLabel`/`sourceTagType` 着色；`TimelineSource` 类型扩展 + 测试 | ✅ |

**改动端点：** `GET /api/events`（`source` 新增 `llm`/`risk`）。**无新表。**

**设计要点：**
- **纯只读、纯增量**：复用既有 union 分页/搜索/skip_category 逻辑；`skip_category`（交易专属）置位时跳过其他三源，与原 audit 行为一致。
- **严重度映射**：LLM `success`→INFO / 失败→WARNING；风控事件默认 WARNING；`result` 反映 LLM `applied`。
- **fetch_n 上限不变**（`_MAX_MERGED_FETCH=2000`），四表合并仍 O(1) 深翻页安全。

**显式 YAGNI 未做：** 通知发送记录入表（当前通知只发不存）、跨源去重、FTS5 全文索引。

---

## 近期已完成迭代 (2026-06-16) — What-If 压力测试 / 蒙特卡洛 (P48)

> 自主 feature 迭代第 6 轮（补充第 6 个 feature）。完成「回测分析三部曲」（参数扫描 P43 / 时序稳定性 P45 / 路径敏感性 P48）。`pytest +8`（1026→1034），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 回答「若实际价格路径略不同于历史，结果会多差？」——对当前 CSV 做确定性蒙特卡洛扰动（每根 K 线 OHLC 按幅度随机缩放），重跑 N 次，给出收益分布与尾部风险。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `stress_test()` 纯函数：seeded `random.Random` 对 OHLC 做均匀缩放扰动（保 high≥low 不变性），收集 N 个 `total_return_pct`/`max_drawdown_pct`，汇总基线/中位/P5/P95/最差/盈利场景占比；`_percentile`/`_jitter_bars` 辅助 | ✅ |
| **T2** | `POST /api/backtest/stress` + `StressTestRequest/Result` schema（scenarios 1-1000、jitter 0-20、seed≥0） | ✅ |
| **T3** | 前端 Backtest.vue「What-If 压力测试」折叠区（场景数/扰动%/种子 + 6 项分布卡片）+ `runStressTest` + 类型 + Cypress | ✅ |

**新增端点：** `POST /api/backtest/stress`。

**设计要点：**
- **确定性**：固定 `seed` → 同输入同输出（`random.Random(seed)`）；测试无需 sleep。
- **扰动保不变性**：OHLC 同乘正因子 `1+uniform(-j,j)`，保持 high≥low、high≥open/close，不会产生非法 bar。
- **零扰动 = 基线**：`jitter_pct=0` 时每个场景恒等于基线回报（可验证）。
- **与 sweep/walk-forward 正交**：sweep=参数寻优，walk-forward=时序稳定，stress=路径敏感（模型不确定性），三者互不重叠。
- **纯离线**：同源 `app/core/backtest.py`，零 DB/broker/runner 耦合。

**显式 YAGNI 未做：** 偏向性扰动（adverse-only）、波动率聚类（GARCH 式）、参数×扰动联合扫描、分布直方图可视化（当前仅 P5/P50/P95/最差）、并发场景（顺序即可）。

---

## 近期已完成迭代 (2026-06-16) — 定时绩效报告 (P47)

> 自主 feature 迭代第 5 轮（5 轮计划最后一轮）。`pytest +7`（1019→1026），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 让绩效自动找上门——后台 cron 按 `StrategyConfig.report_schedule_*` 周期构建日报（复用 `ReportService`）经 `MultiChannelNotifier` 推送，无需手动打开页面。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `StrategyConfig` 3 列（`report_schedule_enabled`/`interval_hours`/`symbol`）+ `_ensure_strategy_config_report_schedule_columns` + schema（input/merged/response）+ `strategy_service.updatable_fields` | ✅ |
| **T2** | `ReportScheduleService`：`build_summary`（日报 → 通知文案，不抛）+ `maybe_send`（读配置、内存单调时钟节流、注入 `clock`/`state`/notifier 可测） | ✅ |
| **T3** | `POST /api/reports/schedule/run`（手动触发 / 测试按钮，写审计 `REPORT_SCHEDULE_SEND`）+ `_report_schedule_cron` 后台任务（镜像 `_llm_analysis_cron`，挂 lifespan） | ✅ |
| **T4** | 前端 Strategy 页「定时报告」区（开关 + 标的 + 间隔 + 立即发送测试）+ `runScheduledReportNow` + Cypress | ✅ |

**新增端点：** `POST /api/reports/schedule/run`。**新增配置列：** `StrategyConfig.report_schedule_{enabled,interval_hours,symbol}`。

**设计要点：**
- **复用既有基础设施**：`ReportService.get_daily_report`（日报）+ `MultiChannelNotifier.send`（推送）+ `_llm_analysis_cron` 模式（后台任务），无新依赖。
- **节流可测**：`maybe_send(clock=..., state=...)` 注入单调时钟与状态字典，测试无需 `time.sleep`。
- **重启安全**：内存节流在进程重启后「等一个间隔再发」（state 为空时首次不立即发），避免重启刷屏。
- **只读分析 + 通知**：不发单、不改策略；`build_summary` 失败仅记录并回退为「报告生成失败」文案。

**显式 YAGNI 未做：** 报告落库历史、多渠道分别调度、按周/月cron 表达式（当前固定间隔小时）、PDF/图表附件、失败重试队列（已有 `NotificationRetryQueue` 通用兜底）。

---

## 近期已完成迭代 (2026-06-16) — 持仓浮盈 / 实时未实现 P&L (P46)

> 自主 feature 迭代第 4 轮（5 轮计划之一）。`pytest +7`（1012→1019），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 让用户看见实时敞口——用 `tracked_entries` 的加权入场成本（比券商 `avg_price` 准，CLAUDE.md 已述）× 实时行情，计算每持仓与总未实现盈亏。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `PositionPnlService(db, quote_provider)`：broker 无关（注入 `QuoteProvider` 协议，可离线 mock）；读 `tracked_entries`（`quantity!=0`），多/空盈亏，行情缺失时仅展示成本基础 | ✅ |
| **T2** | `GET /api/positions/pnl`（`api/positions.py`）+ `PositionPnlRow/Result` schema；行情失败 `available=false` 不抛 | ✅ |
| **T3** | 前端独立组件 `PositionPnlPanel.vue`（汇总卡片 + 持仓表）嵌入 Dashboard；`getPositionPnl` + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/positions/pnl`。

**设计要点：**
- **成本来源**：`tracked_entries.cost`（加权均价），非券商 `avg_price`（partial fill / 拆股会偏）。
- **broker 无关**：service 注入 `QuoteProvider`；endpoint 用 `get_runner().broker`，测试用 `FakeBroker`。
- **行情缺失降级**：`has_quote=False`、`unrealized=0`、`available=false`，UI 展示 warning + 成本基础。
- **做空**：`quantity<0` 时 `pnl=(cost-last)*|qty|`。
- **只读**：不写审计、不下单。

**显式 YAGNI 未做：** 按券商 `get_positions()` 对账 tracked（已有 `TRACKED_ENTRY_DRIFT` 审计）、已实现+未实现合并的总权益、浮盈历史曲线、WebSocket 推送浮盈。

---

## 近期已完成迭代 (2026-06-16) — Walk-Forward 滚动窗口回测 (P45)

> 自主 feature 迭代第 3 轮（5 轮计划之一）。复用 P43 的 `sweep_backtest`。`pytest +12`（1000→1012），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 检测过拟合——把数据切成连续「训练 / 测试」窗口，每个训练窗口用扫描网格寻优，再在紧随其后的测试窗口（样本外）评估，汇总跨窗口稳定性。全段好看但跨窗口回报方差大 / 盈利窗口占比低 = 配置脆弱。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `walk_forward_backtest()` 纯函数：滚动 `(train, test)` 窗口，每窗口 `sweep_backtest` 寻优 → `BacktestEngine` 样本外评估；空 grid 退化为纯滚动评估；汇总（均值/中位回报、均值指标、盈利窗口占比、回报标准差） | ✅ |
| **T2** | `POST /api/backtest/walk-forward` + `WalkForwardRequest/Result/Window/Summary` schema；`step` 默认 = `test_size`（不重叠） | ✅ |
| **T3** | 前端 Backtest.vue 「Walk-Forward」折叠区：窗口/步长/指标/寻优开关 + 稳定性卡片 + 逐窗口表；`runWalkForward` + 类型 + Cypress | ✅ |

**新增端点：** `POST /api/backtest/walk-forward`。

**设计要点：**
- **纯离线**：与 sweep 同源（`app/core/backtest.py`），零 DB / broker / runner 耦合，零实盘风险。
- **复用 P43 sweep**：每训练窗口调用 `sweep_backtest` 取最优 params，`best` 为 None 时该窗口样本外指标置 None。
- **稳定性指标**：`profitable_window_pct`（盈利窗口占比）、`test_return_std_pct`（回报标准差）直接量化「跨窗口一致性」。
- **步长语义**：`step=None`/`0` → 不重叠（= `test_size`）；`step<test_size` → 重叠训练窗口。
- **样本外窗口数不足**：返回 `window_count=0`（不报错），前端展示空态。

**显式 YAGNI 未做：** 多参数联合 walk-forward 元优化、按窗口变化的 `train/test` 比例、蒙特卡洛扰动窗口、walk-forward 结果落库。

---

## 近期已完成迭代 (2026-06-16) — 交易笔记 / 复盘日志 (P44)

> 自主 feature 迭代第 2 轮（5 轮计划之一）。`pytest +12`（988→1000），`basedpyright` 0/0/0，`vue-tsc` 0，build pass。

**目标：** 闭环「复盘/学习」——给订单（每单一笔记）挂上自由笔记、标签、1-5 评分，交易历史页内联编辑。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `TradeNote` 模型 + `_ensure_trade_notes_table`（唯一 `order_id`、`tags_json` JSON 文本、`symbol,updated_at` 索引） | ✅ |
| **T2** | `TradeNoteService`（list/get/upsert/delete，tags 防御式解析）+ `TradeNoteUpsert/Out/Page` schema（tags 去重/trim/上限） | ✅ |
| **T3** | `/api/trade-notes` CRUD：GET 列表(分页/`symbol`)、GET/PUT/DELETE `/{order_id}`；PUT/DELETE 写审计 `TRADE_NOTE_*`；订单不存在 404；DELETE 幂等 204 | ✅ |
| **T4** | 前端 TradeHistory.vue：「笔记」列（已有笔记显示 📝）+ 编辑弹窗（textarea / 多选标签 / `el-rate` 1-5）+ api/tradeNotes.ts + 类型 + Cypress | ✅ |

**新增端点：** `GET /api/trade-notes`、`GET|PUT|DELETE /api/trade-notes/{order_id}`。

**设计要点：**
- **挂载到订单 DB `id`**（非 `broker_order_id`）：`OrderResponse.id` 已暴露；仅持久化订单（`id>0`）可附笔记，broker-only 订单（`id=0`）PUT 自然 404。
- **每单一笔记（upsert）**：`order_id` 唯一；PUT 创建或更新。
- **tags 存 JSON 文本**：项目不用 SQLAlchemy JSON 类型，统一 Text + `json.dumps`（镜像 `parameter_grid_json`）。
- **审计**：`TRADE_NOTE_UPSERT` / `TRADE_NOTE_DELETE` 写 `audit_logs`（复用 `get_audit_logger` + `extract_actor`），失败仅 warning 不抛。

**显式 YAGNI 未做：** 按「往返回合」（BUY+SELL 配对）聚合笔记（当前挂单粒度更简单明确）、笔记全文搜索、笔记附件/图片、跨设备同步。

---

## 近期已完成迭代 (2026-05-26)

> 对应 commit `323743b feat: add audit notifications and trading safety`。基线 `pytest 485 passed`，`basedpyright` 0/0。规格：[2026-05-26-audit-notification-trading-safety-design.md](superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md)，实施计划：[2026-05-26-audit-notification-trading-safety.md](superpowers/plans/2026-05-26-audit-notification-trading-safety.md)。

| Task | 主题 | 状态 |
|------|------|------|
| **T1** | `AuditLog` 模型 + `_ensure_audit_log_table` + `AuditLogger` + 9 个写端点审计 | ✅ |
| **T2** | 交易时段守卫（`trading_session_mode` + 双层 gate + `SESSION` skip + `TRADING_SESSION_BLOCKED`） | ✅ |
| **T3** | `BrokerGateway._call_with_retry` 分档退避（订单全量、行情低重试） + `BROKER_RETRY` 审计 | ✅ |
| **T4** | `NotifierInterface` + `MultiChannelNotifier` + `ServerChan` / `Webhook` + severity 分级 | ✅ |
| **T5** | 前端集成：Credentials 通知渠道、Strategy 交易时段、Decision Timeline `source` + 多选 + 审计卡片、Dashboard SESSION 指示器 | ✅ |
| **T6** | 测试与 lint：~50 项 pytest 新增、3 个 cypress spec、`basedpyright` 0/0 | ✅ |

**新增端点 / 改造端点**：
- `GET /api/events` 现支持 `source=trade|audit|all` 与 `event_type` 重复参数（跨表 union 分页）。
- 9 个写端点（control / strategy PUT / credentials PUT / orders cancel）记录 `audit_logs`，含 `actor_hash`（SHA-256 X-API-Key 前 16 hex）、`source_ip`、脱敏 `request_summary`、`result`、`severity`。

**新增环境变量**：`AUTO_TRADE_BROKER_RETRY_MAX`、`AUTO_TRADE_BROKER_QUOTE_RETRY_MAX`、`AUTO_TRADE_BROKER_RETRY_BASE_MS`、`AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT`。

**显式 YAGNI 未做**（仍在 Roadmap 边缘）：节假日历、审计 CSV/JSON 导出、Webhook 模板编辑器、通知重发队列。

---

## 近期已完成迭代 (2026-06-14) — 20 轮自主迭代

> 用户指令："对这个项目你自主进行20轮迭代，迭代方向自定，可以使用 subagent"。本批次由主循环驱动 + 子 agent 做专项审计，**全部 commit 在 worktree**。
> 起点 `pytest 692 passed` → 终点 `pytest 903 passed`（**+211 项**），pytest-cov 覆盖率 **87%**，前端 `vue-tsc --noEmit` 0 error。

| # | 主题 | 关键交付 |
|---|------|----------|
| **01** | 全项目 review-fix 续篇 | broker 交换 race、unknown symbol fallback、CORS、audit O(n²)、daily_pnl NULL、CSV BOM、symbol replace、runner XFF 等 8 项修复 |
| **02** | Backtest CSV BOM / UX | `parse_backtest_csv` 去 BOM、Backtest.vue dirty 检测 + 覆盖前确认 |
| **03** | 交易日历增强 | `holiday_calendar.py`（NYSE + HKEX 2024-2026 静态闭市日）+ `GET /api/calendar/{today,closures,lookup}` |
| **04** | 审计日志导出 | `GET /api/audit-logs/export?format=csv\|json&action=&severity=` |
| **05** | Webhook 模板编辑器 | token 白名单（`title/content/severity/timestamp/source`）+ JSON 解析后字符串注入 |
| **06** | 通知重发队列 | `NotificationRetryQueue`（daemon 线程 + 指数退避 cap 60s + max_attempts 4） |
| **07** | 仪表盘关键指标 | Sharpe / Profit Factor / 盈亏比 / 最大回撤 / 平均 PnL / 交易笔数 |
| **08** | 凭证管理 UX | 脱敏 toggle + "测试连接" 按钮（`POST /api/credentials/test`） |
| **09** | 风控参数一致性 | `validate_strategy_consistency`（min_profit vs fee、max_daily_loss vs min_profit、sell_high vs buy_low）|
| **10** | 错误处理一致性 | `CredentialIntegrityError` 等自定义异常 + 全链路 try/except |
| **11** | 前端类型契约 | `utils/validator.ts`（`object/string/number/...` + `safeValidate`）+ WS schema 校验 |
| **12** | 覆盖率 | pytest-cov + `.coveragerc` omit 启动入口 + `CLAUDE.md` 基线记录（87%） |
| **13** | 内存泄漏审计 | `_recent_quotes` 与 `runtime.recent_quotes` 改 `deque(maxlen=...)` + 单边窗口淘汰；`_prune_llm_per_symbol_caches` 周期清理 |
| **14** | 前端 a11y | `aria-label` / `aria-pressed` / `role="region"` / `aria-labelledby` 覆盖控制按钮、kill switch、quick-actions 面板 |
| **15** | Backtest 风险调整指标 | `_calc_sortino_ratio` + `_calc_calmar_ratio` 接入 `BacktestMetrics` + 4 个测试 |
| **16** | Docker 多阶段 | `backend/Dockerfile`：`builder` 编 wheel 进 venv → `runtime` 仅复制 venv + tini；根 + 后端双层 `.dockerignore` |
| **17** | 观察列表 LLM 评分 | `WatchlistScore` 模型 + `WatchlistScoreService`（fallback 4 源）+ 3 个端点 + 前端可排序评分列 + 操作按钮 |
| **18** | 决策时间线搜索 + 书签 | `q` 查询参数（OR ILIKE 4 列）+ 前端搜索框 + localStorage 书签（最多 20） |
| **19** | 后端性能热点 | subagent 审计 22 项；实施 3 项（deque bound、broker 批量、单边窗口淘汰）；903/903 测试通过 |
| **20** | 总结 | 本节 + 状态快照表更新 |

**新增端点**：
- `GET /api/calendar/today` / `closures` / `lookup`
- `GET /api/audit-logs/export`
- `GET /api/metrics/summary?days=`
- `POST /api/credentials/test`
- `POST /api/watchlist/score` / `GET /api/watchlist/scores` / `GET /api/watchlist/scored-snapshots`
- `GET /api/events?q=`（OR 全文搜索）

**新增数据表**：`watchlist_scores`（symbol, score, rationale, confidence, recommended_action, source, created_at, expires_at + 复合索引）

**新增服务**：`WatchlistScoreService` / `NotificationRetryQueue` / `metrics.py`

**关键修复（性能）**：
- `_remember_quote` / `_remember_symbol_runtime_quote` 改用 `deque(maxlen=_recent_quotes_cap)`，O(n) 列表重建 → O(1) 摊销单边淘汰
- `_quote_for_llm_order` / `_refresh_quote_if_stale` 改用 `broker.get_quotes([symbol])` 复用单次往返
- `float(quote.last_price)` 在 `_remember_quote` 内只计算一次（原两次）

**未做（YAGNI 显式排除）**：
- 观察列表评分 cron 自动周期打分（手动触发即可，LLM 配额不允许）
- 全局多设备书签同步（localStorage 即可）
- FTS5 全文索引（搜索量级尚未触发）
- AsyncIO retry（当前 retry 仍在 worker 线程，可接受）

---

## 近期已完成迭代 (2026-05-25)

> 对应 commit `feat: exchange trade days, tracked entries, and real LLM market data`。用户向说明已同步至 `README.md` / `CLAUDE.md`。

| 代号 | 主题 | 状态 |
|------|------|------|
| **P1** | DataAggregator 真实 K 线 + `BrokerGateway.get_candlesticks` | ✅ |
| **P3'** | `market_calendar` + 风控/日 PnL 按市场切日 + HK tick | ✅ |
| **P4'** | `tracked_entries` 持久化 + 启动 `TRACKED_ENTRY_DRIFT` 对账 | ✅ |
| **P5'** | lifespan 非阻塞启动；RTH 内推送静默重订（~90s） | ✅（未接 SDK disconnect 回调） |
| **P6'** | 批量 quote、`/api/orders` 默认读 DB + `refresh`、`_recent_quotes` 上限 500 | ✅ |
| **P7'** | IntervalApplication 与文档对齐（保留追价加仓） | ✅ |
| **P8'** | 删除 `_wait_for_order_completion`、`Settings.frontend_port` | ✅ |
| **P2** | API 鉴权收紧 | ❌ 明确不做（owner decision 2026-05-25） |

审计项 **#1、#3、#4、#6、#7、#8（部分）、#11、#12、#13、#14、#15** 已随上表修复；**#2**（区间应用）按 P7' 方案 B 保留实现；**#5** 保留为已知风险但不纳入 Roadmap；**#9、#10、#17** 仍开放。

---

## 近期已完成迭代 (2026-05-17)

- **可维护性重构 (Maintainability Refactor)**
  - 提取 `TradeExecutionService`：将订单执行、状态查询、通知、PnL 从 `AppRunner` 中解耦。
  - 提取 `RuntimeStateService`：将引擎与风控状态的加载/持久化解耦。
  - 重构 `AppRunner`：成为纯粹的生命周期协调器（订阅行情、路由事件、后台保活）。
  - 前端 API 层拆分为按域模块 (`client.ts`, `strategy.ts`, `credentials.ts`, `trade.ts`)。
  - 引入 Vue Composables (`useDashboardData`, `useStatusStream`, `useAccountRefresh`, `useFormState`) 清理页面级逻辑。
  - 补充 E2E 测试（Cypress 全页面导航、控制、策略、凭证、历史），Dashboard 可用性区分不可用与零值。

## 2026-05-24 全面审计与 2026-05-25 决策记录

> 本节为 2026-05-24 一次完整代码审计后的产物。2026-05-25 用户决策明确不实施 P2；在已落地的正确性修复之后，下文 P4 成为当前下一迭代。
>
> 审计基线（2026-05-24）：`pytest 374 passed, 1 skipped`。P1–P8' 交付后 **`pytest 417 passed`**；P4 交付后（2026-05-25）**`pytest 433 passed`**，`basedpyright` 0 errors。

### 审计发现（按严重度）

#### A. 交易正确性 / 数据真实性

1. **`DataAggregator` 完全伪造 LLM 用的历史 K 线**（`backend/app/services/data_aggregator.py:47-81`）
   - `_fetch_daily_candles` / `_fetch_minute_candles` 只调用 `broker.get_quote()` 一次，用 `last_price * 0.98/1.02` 合成 1 根假 OHLC 返回。
   - ATR(14) 需要 ≥5 根 → 永远返回 `0`；布林带需要 ≥10 根 → 永远返回 `(0, 0, 0)`。
   - `LLMAdvisorService.analyze` 把这一根伪造数据塞进 prompt 的"最近 7 天日 K"表，**整个 LLM 顾问回路一直在假数据上跑。**
   - longport SDK 实际暴露 `QuoteContext.candlesticks` / `history_candlesticks_by_date` / `history_candlesticks_by_offset`，但 `BrokerGateway` 没包装。

2. **`IntervalApplicationService` LONG/SHORT 行为与设计文档相反**（`backend/app/services/interval_application_service.py:146-183`）
   - CLAUDE.md / 原设计：`LONG → 只允许上抬 sell_high`，`buy_low 忽略`。
   - 实现：`if new_buy_low <= old_buy_low: config.buy_low = new_buy_low` —— 实际允许 LONG 状态**下调** buy_low（即追加买入），SHORT 镜像同理。
   - 必须二选一：要么改代码恢复"只放宽不收紧"，要么改文档承认追价行为。

3. **`daily_pnl` / `consecutive_losses` 按 UTC 日切日**（`backend/app/core/risk.py:165`、`backend/app/services/daily_pnl_service.py:65`）
   - 美股 RTH = UTC 14:30~21:00；港股 RTH = UTC 01:30~08:00。UTC 00:00 切日点落在两边都不合理的时刻。
   - 结果：日内连损/亏损上限会在不该重置的时点重置。

4. **HK 标的没有 tick 量化**（`backend/app/services/trade_execution_service.py:217`）
   - `_normalize_limit_price` 只处理 `.US` 的 0.01 tick；HK 阶梯 tick（0.001/0.005/0.01/0.02/0.05）未实现，下单价格可能被券商拒。

#### B. 安全

5. **`require_api_key` 实际只挂在 `/strategy/llm-interval/preview` 一个端点**（`backend/app/api/auth.py` + `backend/app/api/llm_advisor.py:124`）
   - 全部变更类 endpoint 裸奔：`/api/control/{start,stop,pause,resume,kill-switch,disable-kill-switch}`、`PUT /api/strategy`、`PUT /api/credentials`、`POST /api/orders/{id}/cancel`、`WS /ws`。
   - 即使是挂了的端点，`auth.py:21` 的 `if settings.env in ("dev","test") and not provided: return` 让 dev/test 下"不带 header 即放行"。
   - 内网假设下尚可接受，但同时也意味着 `AUTO_TRADE_API_KEY` 实际形同虚设。

#### C. 可靠性 / 恢复

6. **`runner.start()` 在 `lifespan` 中同步阻塞事件循环**（`backend/app/main.py:146`）：`_initialize_runner` 包含多次券商网络调用，会让 FastAPI 启动期阻塞秒级。
7. **`TradeExecutionService._entry_positions` 仅在内存**：commits `e094691`/`1499828` 引入加权入场成本修复 broker `avg_price` 偏差，但进程重启后丢失，退回到被修复前的状态。
8. **Longport WebSocket 没有重连**（`backend/app/core/broker.py:253`）：只能靠 `_refresh_quote_if_stale` 每 15s 主动拉 quote 续命，底层订阅断了不会重订。
9. **缺少交易时段守卫**：盘前/盘后/休市 quote 也会触发下单。
10. **券商调用无 retry/backoff**：限流靠 `_is_auto_resumable_pause_reason` 字符串匹配（含中文 `限流` `频率`），易漏判。

#### D. 性能

11. **`/api/account` 对每个持仓循环 `broker.get_quote()`**（`backend/app/api/trade.py:353`）—— N 次往返，每次刷新都触发。
12. **`/api/orders?scope=today` 每次都去券商拉**，与 runner 后台 15s 同步重复，浪费 quota。
13. **`TradeExecutionService._wait_for_order_completion` 是死代码**（已被 pending reconcile 取代，且内含阻塞 `time.sleep`）。
14. **`AppRunner._recent_quotes` 无上限**，只靠时间窗剪枝。

#### E. 测试覆盖盲区

15. `DataAggregator` 零测试 —— 假数据 bug 因此长期未被发现。
16. `IntervalApplicationService._apply_long` 无对照规范的断言，行为漂移没被守护。
17. 无"重启 + pending 订单存在"的端到端集成测试，仅有单元级 mock。 ✅ **已关闭 2026-06-03**（P23a' 交付：新增 `tests/test_e2e_restart.py` 端到端覆盖 5 个场景——`tracked_entries` 持久化 + drift 对账、`unresolved live order` 触发风控暂停、`pending` 超时回收、runner refresh 与 DB 同步、start/stop 状态机无残留；详见 P23a' 段。）

### 迭代排序（2026-05-24 起执行）

| 顺序 | 代号 | 主题 | 价值 | 预估工时 |
|------|------|------|------|----------|
| 1 | **P1** | DataAggregator 真实 K 线 | 直接修复 LLM 决策基础 | 1~2 天 |
| 2 | **P2** | API 鉴权收紧 | ❌ 不实施（保留审计记录；内网部署决策） | — |
| 3 | **P3'** | 交易日 + HK tick | 多市场正确性，避免风控/日 PnL 错切日 | 2~3 天 |
| 4 | **P4'** | 入场成本持久化 + 重启对账 | 修复重启后 PnL 计算回退到 broker `avg_price` | 2 天 |
| 5 | **P5'** | lifespan 非阻塞 + WS 重订 | 提升运行时韧性 | 1~2 天 |
| 6 | **P6'** | 性能：批量 quote + 去重 today_orders | 降低券商 quota 与前端延迟 | 0.5 天 |
| 7 | **P7'** | `IntervalApplicationService` 对齐文档 | 终止行为与文档不一致 | 0.5 天 |
| 8 | **P8'** | lint / 死代码清理 | 还清现存类型债 | 0.5 天 |
| ✅ | **P4** | 交易执行安全与成本控制增强 | 手续费门槛、撤单前改价保护、跳过原因可见（2026-05-25 交付） | — |
| 后续 | 沿用下文 | 审计日志、移动端、复盘工作台、观察列表 | P4 后顺序执行 | — |

### P1：DataAggregator 真实 K 线 ✅（2026-05-25 完成）

> **目标：** 让 LLM 顾问拿到真实历史，恢复 ATR/布林带的有效性。

#### 范围

- `backend/app/core/broker.py`
  - 新增 `BrokerGateway.get_candlesticks(symbol: str, period: str, count: int) -> list[BrokerCandle]`，封装 `quote_ctx.history_candlesticks_by_offset(symbol, period, AdjustType, count, direction)`。
  - 数据结构 `BrokerCandle(timestamp, open, high, low, close, volume)`，统一返回 `datetime` 而非字符串。
- `backend/app/services/data_aggregator.py`
  - `_fetch_daily_candles` 改为请求最近 30 根日 K（保留 `slice[-7:]` 给 prompt 但 ATR/布林带用全部）。
  - `_fetch_minute_candles` 改为请求最近 60~120 根 1 分钟 K。
  - 删除 `quote.last_price * 0.98/1.02` 合成代码；如果 broker 不可用就返回空列表并让 prompt 提示"历史数据不可用"。
  - `fetch_market_data` 接受外部传入的 `BrokerGateway`（避免每次新建 + close），由 `LLMAdvisorService` 注入 `runner.broker` 或独立实例。
- `backend/tests/test_data_aggregator.py`（新）
  - 用 mock broker 验证 7 根日 K → ATR(14) 非零、布林带三值与 mean/std 一致。
  - 验证 broker 抛错时 `fetch_market_data` 返回空列表而非合成数据。
- `backend/tests/test_broker.py` 增 candlesticks 包装测试（mock longport）。

#### 验证

- [x] `pytest tests/test_data_aggregator.py tests/test_broker.py tests/test_llm_advisor.py -v` 全通。
- [ ] `basedpyright` 不引入新错误（P8' 部分待清）。
- [ ] 手工：开启 `AUTO_TRADE_ENV=dev`，触发 `/api/strategy/llm-interval/preview`，检查 prompt 文本里 `日 K 表` 行数 >1 且各行 OHLC 不再相同。

### P2：API 鉴权收紧 ❌（owner decision 2026-05-25：不实施）

> **审计结论保留：** `require_api_key` 覆盖范围有限；若服务暴露到不可信网络，变更类 endpoint 存在未授权操作风险。
>
> **决策：** 项目当前按可信内网部署运行，用户明确选择不将 API 鉴权收紧纳入 Roadmap，以避免引入额外运维负担。未来如部署边界改变，须重新立项评估。

#### 范围

- 不实施代码或测试改造。
- `README.md` 已声明服务仅供可信内网使用且 `AUTO_TRADE_API_KEY` 为可选配置。
- 若未来恢复该主题，需要重新确认所有写路由、WebSocket 和部署边界，而不是把本节视为待办。

### P3'：交易所感知交易日 + HK tick ✅（2026-05-25 完成）

> **目标：** 用标的所在市场的"交易日"切风控/日 PnL；让 HK 限价单价格落到合法 tick。

#### 范围

- 新建 `backend/app/core/market_calendar.py`
  - `trade_day_for(market: str, instant: datetime) -> date`：US 用 ET、HK 用 HKT 的**本地日历日**（午夜切日；不含节假日历）。`is_trading_hours` 用于 RTH 窗口判断（如行情重订）。
  - `is_trading_hours(market: str, instant: datetime) -> bool`（为后续交易时段守卫预留）。
- `backend/app/core/risk.py`：`RiskController` 接受 `trade_day_provider: Callable[[], date]`，默认仍 UTC（保持向后兼容），由 `AppRunner` 注入 market-aware 版本。
- `backend/app/services/daily_pnl_service.py`：`calculate(trade_day=...)` 调用方传入按市场计算的 `target_day`。
- `backend/app/services/trade_execution_service.py`：`_normalize_limit_price` 加入 HK 阶梯 tick 表（来自港交所规则），同 `BUY` 向下、`SELL` 向上的取整方向。
- 测试：
  - `tests/test_market_calendar.py`（新）：覆盖 US/HK 交易日边界、夏令时切换。
  - `tests/test_risk.py`、`tests/test_daily_pnl_service.py` 补"交易日切日 = 交易所收盘"用例。
  - `tests/test_trade_execution_service.py` 补 HK tick 量化用例。

#### 验证

- [x] `tests/test_market_calendar.py` 覆盖 US/HK 边界；`test_risk` / `test_daily_pnl_service` 补切日用例。
- [x] HK tick：`tests/test_trade_execution_service.py`。

### P4'：入场成本持久化 + 重启对账 ✅（2026-05-25 完成）

> **目标：** 进程重启后仍能用加权入场成本计算平仓 PnL，避免回到 broker `avg_price` 偏差。

#### 范围

- 新表 `tracked_entries(symbol PK, quantity, cost, updated_at)`，`database._ensure_tracked_entries_table` 补丁旧库。
- `TradeExecutionService._record_entry_price` / `_consume_entry_quantity` 改为同步落表（保留内存缓存当 fast path）。
- `AppRunner._initialize_runner` 启动时把 `tracked_entries` 注入回 `_trade_svc._entry_positions`。
- 启动对账：若 `tracked.quantity` 与 broker `position.quantity` 偏差 > 5% 且绝对差 ≥ 1 股，写 **`TRACKED_ENTRY_DRIFT`** 事件（决策时间线）。
- 测试：`tests/test_trade_execution_service.py` 加"重启 → tracked 复原 → 平仓 PnL 用 tracked avg"用例；`tests/test_runner.py` 加 reconcile 漂移用例。

#### 验证

- [x] 单元：`test_trade_execution_service` / `test_runner`（load + drift 事件）。
- [ ] 端到端：`BUY 100@10` → 重启 → broker 改 `avg=11`（mock）→ `SELL 100@12`，PnL 仍按 10 算 ≈ 200。

### P5'：lifespan 非阻塞 + WS 重订 ✅（2026-05-25 部分完成）

> **目标：** FastAPI 启动不被券商网络阻塞；行情 WebSocket 断线能自愈。

#### 范围

- `backend/app/main.py:lifespan`：`await asyncio.to_thread(get_runner().start)`，捕获返回 False 时仅记 warning 不阻塞。
- `backend/app/core/broker.py`：
  - 利用 longport SDK 的 disconnect 回调（如有）；若无，新增"看门狗"：`AppRunner._run_loop` 已每 5s 跑，扩展为检测 `_last_quote_at` 超过阈值且 quote_ctx 仍声称 alive 时强制 `unsubscribe + subscribe`。
- 测试：`tests/test_runner.py` 用 fake broker 注入"60s 无 quote"场景，断言触发重订。

#### 验证

- [x] `asyncio.to_thread(runner.start/stop)`；`test_runner` 重订用例。
- [ ] 启动期 `/api/health` 在 1s 内返回（即便 broker 凭证为空）— 待手工压测。
- [ ] 模拟订阅丢失后，30s 内 quote 恢复推送 — 依赖 RTH + 90s 静默阈值。

### P6'：性能优化 ✅（2026-05-25 完成）

#### 范围

- `backend/app/api/trade.py:get_account` 用 `broker.quote([s for s in positions])` 一次取回所有报价。
- `backend/app/api/trade.py:get_orders` 默认从 DB 读 today 订单，仅当 `?refresh=1` 时触发 broker 同步；前端按钮触发 refresh。
- `backend/app/runner.py:_remember_quote` 给 `_recent_quotes` 加 `len ≤ 500` 硬上限。

#### 验证

- [x] `test_account_api` 批量 quote；`test_api` orders 默认本地；`test_runner` recent_quotes cap。

### P7'：IntervalApplicationService 对齐文档 ✅（2026-05-25 完成 — 方案 B）

- 决策：**保留现行追价/加仓逻辑**，更新 Roadmap 的"迭代 0 → 0.2 渐进式平滑过渡策略"小节，明确描述实际行为是"LLM 在持仓状态下可以下调 buy_low 追价加仓 / 上抬 sell_high 拉高目标价"。
- 行为对照表见上文 `迭代 0 / 0.2`。
- `tests/test_interval_application.py` 已包含此行为断言；后续可在该文件加注释链接本节作为权威说明。

### P8'：lint / 死代码清理 ✅（2026-05-25 部分完成）

- [ ] 修 `basedpyright` 3 处错误（用 `isinstance` 或显式 cast）— `llm_advisor` / `runner` 已加 coerce，待全量 pyright 清零。
- [x] 删除 `TradeExecutionService._wait_for_order_completion`。
- [x] 删除 `Settings.frontend_port`（未使用）。

---

## 后续迭代计划（2026-05-25 更新）

> 注：P2 已由 owner 决策排除。P4 是当前已确认的下一迭代，设计见 `docs/superpowers/specs/2026-05-25-trade-execution-safety-design.md`。

当前系统已经完成 LLM 自动决策上下文、主动价格刷新、订单同步、今日订单分页与撤单、决策时间线、Dashboard 图表化监控。后续计划按“先降低交易风险，再增强复盘能力，再提升运维体验”的顺序推进。

### P3：回测与参数验证 MVP ✅（已完成）

> **目标：** 在实盘继续运行前，用历史价格序列验证当前策略区间、最低盈利金额、止损/撤单规则的收益和风险。

#### 已交付

- 后端 `BacktestEngine` + `POST /api/backtest/run`（CSV：`timestamp,open,high,low,close,volume`）
- 前端 **Backtest** 页：CSV 上传/粘贴、参数表单、收益曲线、交易明细、手续费敏感性
- pytest + Cypress `backtest.cy.ts` 覆盖主流程

#### 后续可选增强

- Sharpe / 盈亏比等扩展指标（Roadmap 原列项，当前 UI 未展示）

### P4：交易执行安全与成本控制增强 ✅（2026-05-25 交付，提交范围 29b4890 .. 4a9f36f）

> **目标：** 进一步降低”无价值撤单重挂、重复 LLM 发单、手续费吞噬收益、跳过原因不可见”的风险。
>
> **规格文档：** `docs/superpowers/specs/2026-05-25-trade-execution-safety-design.md`。

#### P4 交付摘要

- **配置持久化（Task 1）**：`StrategyConfig` 新增 `fee_rate_us`、`fee_rate_hk`、`min_repricing_pct`、`llm_action_cooldown_seconds`；`_ensure_strategy_config_trade_safety_columns` 补旧表；API schema 与前端 Strategy 表单同步。
- **费用后收益门槛（Task 2）**：`_profit_guard_for_exit` 叠加 round-trip 费用估算；净收益不足时 `ORDER_SKIPPED` + `skip_category=”FEE”`；`allow_loss_exit=True` 绕过门槛。
- **LLM 改价与冷却 Gate（Task 3）**：`execute_llm_order_decision` 在撤单前执行改价阈值（`REPRICING`）与同方向冷却（`COOLDOWN`）检查；`_last_llm_action_at[(symbol, side)]` 仅在成交/提交后更新；`CANCEL_PENDING` 不受 gate 影响。
- **回测跳过原因分类（Task 4）**：`BacktestEngine` 跳过事件携带 `skip_category`，不读实盘 `fee_rate_us/hk`，离线模型保持独立。
- **Strategy 表单（Task 5）**：前端 Strategy 页面新增四个执行保护字段，含帮助提示与校验，`PUT /api/strategy` 持久化。
- **Decision Timeline + Dashboard 展示（Task 6）**：`skipCategoryLabel` 统一渲染六类跳过标签（FEE / REPRICING / COOLDOWN / RISK / PENDING / POSITION）；Dashboard 最近动作与 Timeline 详情同步展示。

#### 验证结果（本轮交付后）

- [x] `pytest 433 passed`（+16 项，相比 P4 前 417 项）
- [x] `basedpyright` 0 errors, 0 warnings
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过
- [x] 边界检查：`backtest.py` 中无 `fee_rate_us` / `fee_rate_hk` 引用（回测费率配置独立）

### P5+：操作审计 + 多渠道报警 + 交易可靠性补强 ✅（2026-05-28 交付）

> **目标：** 把运维行为、风险事件、关键交易动作完整审计化；支持 Server 酱 + Webhook 多渠道通知；补齐交易时段守卫与 Broker 调用重试。
>
> **规格文档：** `docs/superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md`
>
> **基线（交付后）：** `pytest 487 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — AuditLog 基础设施**：`audit_logs` 表 + `_ensure_audit_log_table` + `AuditLogger` 工具类（摘要截断/脱敏/异常吞掉不抛）+ DI helper `extract_actor` + `get_audit_logger`。9 个写端点（control ×5 / strategy / credentials / order cancel / kill-switch）全部接入审计写入。
- **T2 — 交易时段守卫（双层）**：`StrategyConfig.trading_session_mode`（默认 `ANY`，零行为变更上线）+ `AppRunner._check_trading_session`（撤单前 gate）+ `TradeExecutionService.execute` 二次 gate；`CANCEL_PENDING` 显式放行；`skip_category="SESSION"`。
- **T3 — Broker retry/backoff**：`BrokerGateway._call_with_retry` 分档（订单 `broker_retry_max=3` / 行情 `broker_quote_retry_max=1`）+ 指数退避；优先结构化异常，否则降级字符串匹配（`限流`/`频率`/`timeout`）；每次重试写 `BROKER_RETRY` 审计。
- **T4 — Notifier 抽象 + Webhook + 分级**：`NotifierInterface` Protocol + `MultiChannelNotifier` fan-out（按 `severity_floor` 过滤）+ `WebhookNotifier`（JSON payload）+ `ServerChanNotifier` 迁入。`KILL_SWITCH` endpoint 补齐 `notify_risk_event("KILL_SWITCH", ..., severity="CRITICAL")`。`CredentialConfig.notification_channels` 持久化。
- **T5 — 前端集成**：Strategy 表单加 `trading_session_mode` 单选（ANY/RTH_ONLY）+ 不含节假日提示；Credentials 增加通知渠道列表（Server酱/Webhook+severity_floor）；Decision Timeline 支持 `source=trade|audit|all` 切换 + 多选 event_type 过滤 + 审计卡片（severity/actor_hash/source_ip）；Dashboard skipCategoryLabel 增加 `SESSION` 标签。
- **T6 — 测试与 lint 清零**：pytest 新增 ~52 项（总计 487）；Cypress 新增 3 个 spec（`credentials_notifications`、`decision_timeline_audit`、`strategy_session_guard`），共 14 个 E2E spec；`basedpyright` 0 errors。

#### 验证

- [x] `pytest 487 passed`
- [x] `basedpyright` 0 errors / 0 warnings
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过
- [x] Cypress 新增 3 个 spec 均通过

### P6：移动端与应急操作体验

> **目标：** 手机上能可靠查看状态和执行紧急操作，避免桌面不可用时无法止损/暂停。

#### 范围

- App 顶部导航移动端改为紧凑菜单或底部 Tab。
- Dashboard 移动端优先展示：价格、持仓、盈亏、暂停/Kill Switch、最近订单。
- 图表在小屏幕可折叠，避免横向滚动。
- 增加移动端 Cypress 视口测试：iPhone、Pixel。
- 可选：基础 PWA manifest，让页面可添加到主屏幕。

#### 验证

- Cypress 移动端视口无横向滚动，关键按钮可点击，文字不溢出。
- 浏览器手工验证 Dashboard、Strategy、History、Decision Timeline。

### P7：策略复盘与 LLM 优化工作台 🚧（当前迭代）

> **目标：** 利用已沉淀的 LLM 交互、状态快照、订单事件，按”交易日 × 当前 symbol”复盘，反哺 prompt 调优。
>
> **规格：** `docs/superpowers/specs/2026-05-26-replay-llm-workshop-design.md`

#### 范围

- 新增”Replay”页面：按交易日展示 K 线走势 + LLM 建议 × 实际成交 × 真实 PnL。
- 关联键：`LLMInteraction.order_id` ↔ `OrderRecord.broker_order_id`，**不增表、不加 FK**，查询时内存 JOIN。
- 5 个错误标签按优先级评估：`MISSED_STOP` / `PREMATURE_ENTRY` / `FREQUENT_REPRICE` / `LOW_PROFIT` / `NORMAL`；阈值为代码常量。
- 价格曲线优先来自 `BrokerGateway.get_candlesticks`，broker 不可用 / 历史超出保留时回退 `RuntimeStateSnapshot`。
- 导出 JSON（细粒度，完整 prompt + 关联订单）与 CSV（扁平表格），浏览器原生下载。
- `realized_pnl` 复用 `DailyPnlService.calculate` 重算，不修改写路径。
- `list_days` 固定按 `orders` / `llm_interactions` / `runtime_state_snapshots` 三类数据并集构造（market-aware trade day），仅返回有数据日，不补齐空白日历日。
- API 语义固定：参数非法返回 `422`；参数合法即使空数据也返回 `200`（空数组结构），不使用 `404` 表示“合法但无数据”。

#### 显式不做

- 多标的复盘（待 P8 Watchlist）；标签阈值可配置；历史 `StrategyConfig` 快照；反向 FK 字段。

#### 验证

- 后端 pytest ~25 新增（聚合主路径 / trip 划分 / 5 个标签 / 三端点 / 导出格式 / 合法空数据返回 200）。
- Cypress `replay.cy.ts` 覆盖列表、切日、导出、标签视觉。
- `basedpyright` 0/0；`npm run type-check` + `build`。

### P8：多标的观察列表（暂不自动交易）

> **目标：** 先支持多个标的的行情和 LLM 观察，暂不允许多标的自动下单，避免交易风险突然扩大。

#### 范围

- 支持 Watchlist：多个 symbol 的价格、波动、LLM 建议、风险摘要。
- 每次只允许一个“交易标的”处于自动交易状态。
- Dashboard 增加观察列表表格，不改现有单标的交易引擎。

#### 验证

- 后端测试覆盖观察列表 CRUD 和行情聚合。
- Cypress 覆盖添加/删除观察标的、切换交易标的前的确认流程。

### P9：LLM Prompt Engineering Optimization ✅（2026-05-29 交付）

> **目标：** 通过模块化 prompt 架构、技术指标扩展和 A/B 测试支持，提升 LLM 交易顾问的决策质量和系统性能。
>
> **规格文档：** `docs/superpowers/specs/2026-05-28-llm-prompt-engineering-optimization-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-28-llm-prompt-optimization.md`
>
> **基线（交付后）：** `pytest 549 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### Phase 1 交付摘要

- **T1-T5 — 模块化 Prompt 架构**：`PromptModule` 抽象基类 + `SystemModule`（角色/规则）+ `ContextModule`（K 线/指标/情绪）+ `StrategyModule`（持仓/风控）+ `OutputModule`（JSON 格式）+ `PromptBuilder` 编排器。`DataAggregator.build_prompt()` 重构为模块化组合。
- **T6 — 技术指标扩展**：RSI(14)、MACD(12,26,9)、成交量分析（均量/量比/趋势）集成到 `ContextModule`。
- **T7 — DataAggregator 集成**：`fetch_market_data()` 返回扩展指标数据，`PromptBuilder` 自动渲染。
- **T8 — 数据库模型**：`PromptVersion` + `ExperimentResult` 表 + `_ensure_*` 迁移补丁。
- **T9 — ABTestManager**：prompt 变体选择 + 交互结果记录 + 胜率计算。
- **T10 — Experiments API**：`/api/experiments` CRUD + 结果查询。
- **T11 — LLMAdvisorService 集成**：使用 `PromptBuilder`，支持实验变体选择。

#### Phase 2 交付摘要

- **T13 — 市场情绪模块**：`SentimentAnalyzer` 分析价格动量、波动率、成交量异常，输出情绪评分（bearish/neutral/bullish）。
- **T14 — 情绪集成**：`ContextModule` 渲染情绪分析结果到 prompt。
- **T15 — 多时间框架分析**：日 K + 周 K 趋势对齐检测，输出 alignment 信号。
- **T16 — 性能追踪器**：`PerformanceTracker` 记录 LLM 建议 vs 实际结果，计算准确率/收益/回撤；`/api/performance` 查询 API。

#### 验证结果（本轮交付后）

- [x] `pytest 549 passed`（+56 项，相比 P9 前 493 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（3.30s）

### P10：LLM 特征工程扩展 — 技术指标深度优化 ✅（2026-05-29 交付）

> **目标：** 扩展经典技术指标覆盖，为 LLM 提供更全面的市场分析维度。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-feature-engineering-expansion-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-feature-engineering-expansion.md`
>
> **基线（交付后）：** `pytest 586 passed, 1 skipped`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — OBV（能量潮）**：量价背离检测，输出 OBV 序列 + 趋势 + 价格-OBV 背离信号。
- **T2 — ADX（平均趋向指数）**：趋势强度判断，输出 ADX 值 + 趋势强度分类 + DI+/DI-。
- **T3 — Stochastic（随机指标）**：超买超卖检测，输出 %K、%D、信号。
- **T4 — CCI（商品通道指数）**：价格偏离度识别，输出 CCI 值 + 信号。
- **T5 — Williams %R**：超买超卖检测（更敏感），输出 %R 值 + 信号。
- **T6 — VWAP（成交量加权平均价）**：机构成本参考，输出 VWAP 值 + 价格相对位置。
- **T7 — aggregate_signals()**：综合 7 个指标信号，输出 overall_signal + confidence + summary。
- **T8 — DataAggregator 集成**：`fetch_market_data()` 返回所有新指标数据。
- **T9 — ContextModule 渲染**：LLM prompt 新增"技术指标扩展"区块。
- **T10 — 最终验证**：pytest 586 passed, 1 skipped，basedpyright 0 errors，前端构建通过。

#### 验证结果（本轮交付后）

- [x] `pytest 586 passed, 1 skipped`（+38 项，相比 P9 前 549 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（3.64s）

### P11：LLM 自适应特征选择 ✅（2026-05-29 交付）

> **目标：** 实现 LLM 自主特征选择，根据市场状态自动选择最相关的技术指标，减少无关指标干扰，提升决策质量。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-adaptive-feature-selection-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-adaptive-feature-selection.md`
>
> **基线（交付后）：** `pytest 607 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

#### 交付摘要

- **T1 — MarketStateDetector**：基于 ADX/BB/ATR/Volume 检测市场状态（trending/ranging/volatile/neutral），输出状态 + 置信度 + 推荐指标。
- **T2 — SelectionModule**：渲染市场状态和可用指标列表，引导 LLM 选择 3-5 个最相关指标。
- **T3 — FeatureSelector**：解析 LLM 返回的 JSON 指标选择，过滤上下文只保留选中指标。
- **T4 — DataAggregator 集成**：`fetch_market_data()` 返回市场状态数据。
- **T5 — ContextModule 过滤**：根据 `selected_indicators` 只渲染选中指标。
- **T6 — LLMAdvisorService 集成**：prompt 中加入 SelectionModule，LLM 分析时考虑指标选择。
- **T7 — 最终验证**：pytest 607 passed，basedpyright 0 errors，前端构建通过。

#### 验证结果（本轮交付后）

- [x] `pytest 607 passed`（+20 项，相比 P10 前 587 项）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（4.50s）
### P12：LLM 优化工作台前端化 ✅（2026-05-29 交付）

> **目标：** 将 P9 后端已有的实验管理、性能追踪、技术指标等 API 暴露为可用的前端 Lab 页面，供用户直接查看和管理 Prompt 优化工作台。
>
> **说明：** 本迭代与远端独立推进的 P10（特征工程扩展）/P11（自适应特征选择）并行开发，集成时重新编号为 **P12** 以避免与已交付的 P10 冲突。
>
> **规格文档：** `docs/superpowers/specs/2026-05-29-llm-lab-frontend-design.md`
>
> **实施计划：** `docs/superpowers/plans/2026-05-29-llm-lab-frontend.md`
>
> **基线（集成后）：** rebase 到 origin/main（含 P10/P11）之上，全量 `pytest 621 passed`（P11 的 607 项 + 本迭代新增），`npm run type-check` + `npm run build` 通过，Cypress `lab.cy.ts` 4/4 通过。

#### 交付摘要

- **后端 3 个只读端点**：
  - `GET /api/experiments`（列出实验名称，复用 P9 `ExperimentResult` 表）
  - `GET /api/indicators?symbol=`（实时技术指标快照：ATR、RSI、MACD、布林带、成交量、情绪、多时间框架；broker 缺失时 `available=false`）
  - `GET /api/performance/{stats,compare,recommendations}?experiment=`（A/B 性能统计，补充响应模型 schema）
- **前端 Lab 页（`/#/lab`）**：`Lab.vue` 三页签：
  - **实验与版本**：Prompt 版本表格（`PromptVersion` 列表、激活操作）+ 版本创建表单 + 实验摘要选择查看
  - **性能看板**：选择实验后展示 A/B 汇总统计（总交易/胜率/总 PnL/均 PnL）+ 变体对比表 + 优化建议列表
  - **指标面板**：输入标的后查询实时技术指标，`available=false` 时显示"行情不可用"水印，否则渲染 6 张指标卡片
- **Cypress E2E**：新增 `frontend/cypress/e2e/lab.cy.ts`，4 个测试用例覆盖三页签渲染、性能加载、指标不可用/可用

#### 验证结果

- [x] `pytest 621 passed`（rebase 到 P11 的 607 项之上，本迭代新增端点测试全绿；并修复一个因时区/午夜边界导致的 `test_orders_default_returns_local_today_orders_with_pagination` 偶现失败）
- [x] `basedpyright` 0 errors, 0 warnings, 0 notes
- [x] `npm run type-check` 通过
- [x] `npm run build` 通过（7.30s）
- [x] `Cypress lab.cy.ts` 4/4 通过（本地 Vite dev server `CYPRESS_BASE_URL=http://localhost:3001`）

### 建议执行顺序

| 顺序 | 迭代 | 状态 | 原因 |
|------|------|------|------|
| 已完成 | P3 回测与参数验证 MVP | ✅ 已交付 | 已为实盘调参提供历史验证基础。 |
| 已完成 | **P4 交易执行安全与成本控制增强** | ✅ 2026-05-25 | 直接回应手续费、重复 LLM 动作和无价值撤单重挂风险。 |
| 已完成 | **P5+ 操作审计 + 多渠道报警 + 交易可靠性补强** | ✅ 2026-05-28 | 合并 Roadmap 原 P5 与审计遗留 #9/#10；487 项 pytest + 14 Cypress spec 全绿。 |
| 已完成 | **P6 移动端与应急操作体验** | ✅ 2026-05-28 | 底部 Tab 导航 + Dashboard 图表折叠 + 表单单列布局；15 个文件改动。 |
| 已完成 | **P7 策略复盘与 LLM 优化工作台** | ✅ 2026-05-28 | 新增 ReviewService + /api/review/export + Review.vue；pytest 493 passed。 |
| 已完成 | **P8 多标的观察列表** | ✅ 2026-05-28 | WatchlistItem 模型 + CRUD API + 行情聚合 + Watchlist.vue；pytest 11 passed。 |
| 已完成 | **P9 LLM Prompt Engineering Optimization** | ✅ 2026-05-29 | 模块化 Prompt 架构 + 技术指标（RSI/MACD/Volume）+ A/B 测试 + 市场情绪 + 多时间框架 + 性能追踪；pytest 549 passed。 |
| 已完成 | **P10 LLM 特征工程扩展** | ✅ 2026-05-29 | 新增 OBV/ADX/Stochastic/CCI/Williams %R/VWAP 六个技术指标 + aggregate_signals() 综合信号；pytest 587 passed。 |
| 已完成 | **P11 LLM 自适应特征选择** | ✅ 2026-05-29 | MarketStateDetector + SelectionModule + FeatureSelector，LLM 基于市场状态自主选择指标；pytest 607 passed。 |
| 已完成 | **P12 LLM 优化工作台前端化** | ✅ 2026-05-29 | 暴露 P9 后端能力：3 个只读端点 + Lab 三页签前端；Cypress 4/4。 |
| 已完成 | **P13 加仓 + 成本锚定 LLM** | ✅ 2026-05-31 | Engine LONG→BUY 加仓 + ContextModule 持仓成本区块 + 冷却/优先级集成；pytest +8，basedpyright 0/0。 |
| 已完成 | **P14 保证金下单量** | ✅ 2026-05-31 | margin_safety_factor 配置化 + BrokerGateway margin 路径验证；pytest 628 passed，frontend type-check + build 通过。 |
| 已完成 | **P15 Dashboard & 配置性能优化** | ✅ 2026-05-31 | `/api/account` 短 TTL 缓存 + Dashboard 分区加载 + 配置页初始 loading guard；pytest 633 passed / basedpyright 0/0 / frontend build / 新增 Cypress 4 项通过。 |
| 已完成 | **P16 策略实验平台 Phase 1：批量回测 + 排行榜** | ✅ 2026-05-31 | 新增 `/api/strategy-experiments`、参数网格服务、批量回测持久化、Experiments 页面；pytest 678 passed / basedpyright 0/0 / frontend type-check + build / Cypress 77 passed。 |
| 已完成 | **P17 策略实验平台 Phase 2：LLM 评分 + 导出 + Strategy 草稿带回** | ✅ 2026-05-31 | `LLMRecommendationEvaluator`（6 类标签：EFFECTIVE/INEFFECTIVE/TOO_EARLY/TOO_LATE/RISKY/INSUFFICIENT_DATA）+ `GET /api/strategy-experiments/llm-evaluations`；实验 CSV/JSON 导出 + `GET /api/strategy-experiments/{id}/export`；Strategy 草稿带回（`/#/strategy?draftExperimentRunId=xxx`）；前端 Experiments 页面扩展；pytest +12，Cypress +3，frontend type-check + build 通过。 |
| 已完成 | **P18 技术债清理：basedpyright 错误清零** | ✅ 2026-05-31 | 修复 app/ 42 处类型错误（dict/Callable/Generator 泛型补齐、Optional 访问保护、常量重定义消除）；修复 tests/ 约 120 处类型错误（MissingTypeArgument、OptionalMemberAccess、Generator 返回类型等）；pytest 691 passed / basedpyright 0 errors / frontend build / Cypress 80 passed。 |
| 已完成 | **P19 A/B Testing 集成：LLM Prompt 变体实验** | ✅ 2026-05-31 | `LLMInteraction` 增 `prompt_variant` 字段 + `_ensure_llm_interaction_variant_column` 迁移；`Settings.llm_experiment_name` 配置；`LLMAdvisorService._select_variant` 确定性按 symbol hash 分配变体；`_build_prompt` 支持自定义 template；`analyze`/`preview` 全流程透传变体标识并写入 interaction 日志；pytest 696 passed / basedpyright 0 errors。 |
| 已完成 | **P20 策略实验平台扩展指标：Sharpe / Profit Factor / 盈亏比** | ✅ 2026-06-01 | `BacktestEngine` 计算 sharpe_ratio、profit_factor、profit_loss_ratio；`StrategyExperimentRun` 持久化新字段；排行榜支持排序与扩展指标展示；pytest 699 passed / basedpyright 0 errors / frontend build / Cypress 通过。 |
| 已完成 | **P22 LLM 波动率触发补全** | ✅ 2026-06-01 | `_llm_analysis_tick` 提取提升可测试性；模块级 `_last_llm_trigger_price` + `_should_run_llm_analysis` 双门控（时间间隔 OR 价格波动 ≥ `llm_interval_volatility_threshold_pct`）；`RTH_ONLY` 交易时段守卫前置到分析层；pytest 715 passed / basedpyright 0 errors / frontend type-check + build 通过。 |
| 已完成 | **P23a' 审计 #17 端到端重启场景补齐** | ✅ 2026-06-03 | Part A（`llm_advisor_service._call_deepseek` 异常分层 + 9 个新单测：httpx 异常族 4 + 失败落库 2 + parse/preview 边界 2 + Task A1 timeout 1；`preview` 注释修正）。Part B（新增 `tests/test_e2e_restart.py` 5 个端到端场景：`tracked_entries` 持久化 + drift 对账 / unresolved live order 风险暂停 / `pending` 超时回收 / runner refresh 与 DB 同步 / start-stop 状态机无残留）。Part C（跨栈验证 + Roadmap 关闭 #17）。**未 commit**（用户决策）。pytest 730 passed（baseline 715 → +15）/ basedpyright 0 errors / 0 warnings / 0 notes / `npm run type-check` + `npm run build` 通过。**已知行为差异（不属于 #17 关闭范围）：** `_pause_if_unresolved_live_order_exists` 仅 `logger.warning + risk.pause`，不写 `RISK_PAUSED` 事件；记录为 Concern B，待后续审计项评估。 |
| 已完成 | **P21 CI 质量门禁：测试/type-check 阻断坏提交** | ✅ 2026-06-01 | `.github/workflows/dockerhub.yml` 扩展为统一 CI：新增 `backend-test`（pytest + basedpyright）和 `frontend-check`（type-check + build）作业，`dockerhub` 作业依赖两者成功后才推送镜像；Cypress E2E 作为独立作业仅在 PR/手动触发运行，不阻塞主线发布；pip/npm 缓存已配置；pytest 715 passed / basedpyright 0 errors / frontend type-check + build 通过。 |
| 已完成 | **P24 Wave 1：后端韧性（SDK disconnect + RISK_PAUSED 事件 + 测试加固）** | ✅ 2026-06-04 | 关闭 Roadmap P5' SDK disconnect 回调 + P23a' Concern B `RISK_PAUSED` 事件补写。BrokerGateway disconnect hook 机制 + AppRunner 自动重订 + 审计 `BROKER_DISCONNECT` / `BROKER_RETRY_EXHAUSTED`。`RISK_PAUSED` trade_event 含完整 payload。测试加固：freezegun 集成、DST 边界、并发死锁防护。pytest 750 passed / basedpyright 0/0/0 / vue-tsc clean / build 通过。 |
| 已完成 | **P24 Wave 2：质量清扫（死代码 + ai-slop + 测试加固）** | ✅ 2026-06-04 | Task C：删除 5 个未使用 import/局部变量（vulture+pyflakes 扫描，双向 grep 确认）。Task F：提取常量到 `frontend/src/utils/constants.ts`（EVENT_TYPE/ORDER_STATUS/RUNNER_STATUS/PROMISE_STATUS），Dashboard 控制按钮补 6 个 data-testid。Task D：无新增 flaky。pytest 749+ passed / basedpyright 0/0/0 / vue-tsc clean / build 通过。 |
| 已完成 | **P24 Wave 3：P23 前端实时通知中心 + 分支清理** | ✅ 2026-06-04 | Task P23：`useNotificationStream` composable（severity 分级 / 1s 节流 / CRITICAL 5条/分钟上限 / localStorage 偏好 / 断线补齐）。Dashboard 启用通知流。App.vue 右上角"通知偏好"弹窗。Task E：删除 4 个过时本地分支及 worktree。vue-tsc clean / build pass / Cypress spec 创建。 |
| 已完成 | **P25 运行时策略参数热重载：`margin_safety_factor` 链路修复** | 已完成 2026-06-04 | 修复 API → StrategyService → Runner → TradeExecutionService 全链路：`PUT /api/strategy` 可持久化保证金安全系数，`reload_strategy()` 与冷启动 `_initialize_runner()` 都会注入 `_trade_svc.margin_safety_factor`，保证下次保证金下单量使用最新配置；`RuntimeStateService.load()` 返回已加载配置，避免启动期重复查询。新增冷启动回归测试，并修复 orders today 分页测试在本地日跨 UTC 日时的时间冻结问题。验证：P25 focused tests 8 passed；`pytest tests/ -v` 759 passed；`basedpyright` 0/0/0。 |
| 已完成 | **P26 多标的自动交易扩展评估与边界切分** | 已完成 2026-06-04 | 完成多标的状态边界评估：`StrategyEngine` / `RuntimeState` / quote subscription / recent quotes 必须按 symbol 隔离；`RiskController` 保持组合级全局风控；`TrackedEntry` / `OrderRecord` / `TradeEvent` / `DailyPnlService` symbol 过滤能力可复用。产出 spec：`docs/superpowers/specs/2026-06-04-p26-multi-symbol-boundary-design.md`；产出 P27 implementation plan：`docs/superpowers/plans/2026-06-04-p27-readonly-multi-symbol-monitoring.md`。本轮不改变实盘交易行为。 |
| 已完成 | **P27 多标的配置与只读监控 MVP** | 已完成 2026-06-04 | 新增 `/api/watchlist/snapshots` 只读快照 API，聚合 Watchlist + 当前 StrategyConfig 交易标的 + Broker quotes；新增 `WatchlistSnapshot` 后端/前端类型、`useMultiSymbolSnapshots` 轮询 composable、Dashboard “多标的观察”表；保持 `/api/status`、WebSocket、`AppRunner`、自动交易路径单标的不变。验证：`test_watchlist.py` 14 passed / basedpyright 0/0/0 / frontend type-check + build 通过 / Dashboard Cypress 15 passed。 |
| 已完成 | **P28 Wave 1：pending order 按 symbol 隔离** | 已完成 2026-06-04 | `TradeExecutionService` pending order 从单槽改为 symbol-keyed dict；保留旧 `has_pending_order` / `pending_order` / `cancel_pending_order()` 语义；新增 `pending_order_for(symbol)` / `cancel_pending_order_for_symbol(symbol)`；同一 symbol 仍阻止重复下单，不同 symbol 可并存 pending，为后续 runner 多 symbol 状态隔离打基础。验证：focused pending tests 6 passed / backend pytest 765 passed / basedpyright 0/0/0。 |
| 已完成 | **P28 Wave 2：Runner 多标的 runtime 状态骨架** | 已完成 2026-06-04 | 新增 `SymbolRuntime` 与 `AppRunner._symbol_runtimes`，从 Watchlist + 当前策略标的加载 symbol runtime；quote 会写入对应 runtime 的 engine / recent_quotes；非主交易标的 quote 不触发 `self.engine.update_price()` 或下单，保持 `/api/status` 与自动交易路径单主标的兼容。验证：focused runner tests 2 passed / `test_runner.py` 63 passed / backend pytest 767 passed / basedpyright 0/0/0。 |
| 已完成 | **P28 Wave 3：RuntimeState / Snapshot 按 symbol 持久化** | 已完成 2026-06-04 | `runtime_state` 与 `runtime_state_snapshots` 增加 `symbol` 维度及 SQLite 兼容迁移；`StrategyService` runtime 读写按 symbol 隔离，默认 `symbol=''` 保持旧主路径兼容；`RuntimeStateService` 新增 `load_symbol_runtime()` / `persist_symbol()`，runner 会加载与持久化 secondary `SymbolRuntime` engine 状态；自动交易仍保持单主标的。验证：focused symbol persistence tests 5 passed / runtime+database+runner 89 passed / API 56 passed / backend pytest 772 passed / basedpyright 0/0/0。 |
| 已完成 | **P28 Wave 4：多标的 quote-trigger 自动交易启用** | 已完成 2026-06-04 | secondary `SymbolRuntime` quote 不再提前返回；按 quote symbol 选择 runtime engine、symbol-scoped pending guard、symbol market/cash/fee 参数与 engine-specific rollback callback 执行订单；`AppRunner.engine`、`/api/status`、LLM/manual order path 仍保持主交易标的兼容。验证：secondary focused tests 2 passed / `test_runner.py` 65 passed / backend pytest 773 passed / basedpyright 0/0/0。 |
| 已完成 | **P29：Runner 生产诊断快照** | 已完成 2026-06-04 | 新增只读 `GET /api/diagnostics` 与 `AppRunner.diagnostics()`，暴露 runner/thread/quote stream/pending symbols/global risk/symbol runtimes 健康状态；不触发 broker、DB 写入、订单、通知等副作用。验证：diagnostics focused tests 2 passed / `test_runner.py + test_api.py` 123 passed / backend pytest 775 passed / basedpyright 0/0/0。 |
| 已完成 | **P30 Wave 1：LLM 订单执行按 symbol 隔离** | 已完成 2026-06-04 | `execute_llm_order_decision()` 支持显式 `symbol`，可对 secondary `SymbolRuntime` 执行 LLM order action；pending/cancel-replace/cooldown/engine state/market/cash/fee/rollback 均按目标 symbol/runtime 处理；未携带 `symbol` 的决策保持主标的兼容。`_account_context()` 改为读取请求 symbol 的 pending order。验证：LLM symbol focused tests 3 passed / `test_runner.py + test_api.py` 126 passed / backend pytest 778 passed / basedpyright 0/0/0。 |
| 已完成 | **P30 Wave 2：多标的 LLM 调度预算与状态** | 已完成 2026-06-04 | 新增 `llm_max_symbols_per_cycle` / `llm_max_analyses_per_hour` 设置；`GET /api/strategy/llm-interval/status` 扩展返回 `budget` 与 `symbol_statuses`；`AppRunner.llm_symbol_statuses()` 只读暴露各 symbol 的 pending 与 BUY/SELL LLM cooldown 剩余时间，不改变实际调度频率。验证：LLM budget focused tests 2 passed / `test_runner.py + test_api.py` 128 passed / backend pytest 780 passed / basedpyright 0/0/0。 |
| 已完成 | **P31 Wave 1：路由懒加载与前端 JS chunk 拆分** | 已完成 2026-06-04 | 新增 build chunk 预算校验脚本；非 Dashboard 路由与通知偏好弹窗改为懒加载；Vite 对 Vue/Router/Axios/Element Plus 进行手动拆 chunk，生产 JS 已不再出现 >500k 单 chunk。验证：chunk budget RED→GREEN；`test_runner.py + test_api.py` 不受影响；frontend `type-check + build + build:check-chunks` 通过；navigation Cypress 通过。 |
| 已完成 | **P32 Wave 1：Review 按 symbol 查看运行时历史** | 已完成 2026-06-04 | `/api/status/history` 新增可选 `symbol` 过滤并返回 point.symbol；Review 查询同一 symbol/date range 的 runtime history，渲染价格/盈亏图表，不影响 Dashboard 现有 `/api/status` 主驾驶舱路径。验证：status history symbol focused test 1 passed / review runtime history Cypress 1 passed / backend pytest 781 passed / basedpyright 0/0/0 / frontend `type-check + build + build:check-chunks` 通过。 |
| 已完成 | **P32 Wave 2：Dashboard 多标的历史切换** | 已完成 2026-06-04 | Dashboard 图表区新增 symbol 选择器，候选来自主标的 + 观察列表；`PriceChart` / `PnLChart` 按所选 symbol 调用 `/api/status/history?symbol=` 加载历史，默认仍展示主标的，驾驶舱其余状态保持 primary-only。验证：dashboard chart switch Cypress 2 passed / frontend `type-check + build + build:check-chunks` 通过。 |
| 已完成 | **P33 Wave 1：多标的 quote 订阅与断线重订阅** | 已完成 2026-06-04 | runner 启动、断线恢复、静默重订阅、凭证重载、策略重载均改为按 `SymbolRuntime` 集合订阅全部 quote symbols，而非仅主标的；多标的自动交易链路首次具备完整实时行情输入面。验证：multi-symbol subscription E2E RED→GREEN；`test_e2e_restart.py + test_runner.py` 76 passed / basedpyright 0/0/0。 |
| 已完成 | **P33 Wave 2：多标的 pending/fill/restart 生命周期 E2E** | 已完成 2026-06-04 | runner 启动后会从 DB live `OrderRecord` 恢复 symbol-scoped pending orders；broker today-order sync 也会同步刷新内存 pending 状态，保证重启后 pending symbol 与 filled symbol 不串线。验证：multi-symbol pending restart E2E RED→GREEN；`test_runner.py + test_e2e_restart.py + test_trade_execution_service.py` 125 passed / basedpyright 0/0/0。 |
| 已完成 | **P34：诊断面板前端化** | 已完成 2026-06-04 | Dashboard 新增“运行诊断”只读面板，展示线程、quote stream、pending symbols 与全部 symbol runtime 健康；Review 新增“运行诊断快照”，聚焦当前查询 symbol 的 runtime 与 quote stream 状态。验证：diagnostics Cypress RED→GREEN；`dashboard.cy.ts + review_runtime_history.cy.ts` 19 passed / frontend `type-check + build + build:check-chunks` 通过。 |
| 已完成 | **P38：多标的控制面一致性** | 已完成 2026-06-04 | Dashboard 操作控制区补充“全局控制，作用于全部标的运行时”文案；风控状态明确“全局紧急停止 / 全局暂停状态”；诊断面板补充运行时总数，降低多标的运维误解。验证：dashboard Cypress RED→GREEN；`dashboard.cy.ts` 17 passed / frontend `type-check + build + build:check-chunks` 通过。 |
| 已完成 | **P35 Wave 1：多标的 LLM cron 预算裁剪与 secondary 执行** | 已完成 2026-06-04 | `_llm_analysis_tick()` 改为按 primary + `SymbolRuntime` 集合分发分析；受 `llm_max_symbols_per_cycle` / `llm_max_analyses_per_hour` 预算限制；primary 仍是唯一 interval suggestion 持久化目标，secondary symbol 仅执行 `order_action`，避免覆盖全局策略配置。验证：`test_main.py + test_llm_advisor.py` 64 passed / basedpyright 0/0/0。 |
| 已完成 | **P35 Wave 2：多标的 LLM 调度状态落盘与跳过原因** | 已完成 2026-06-04 | 新增 `LLMSymbolScheduleState` 持久化 per-symbol `last_analysis_at` / `next_analysis_at` / `last_status` / `last_skip_reason`；`/api/strategy/llm-interval/status` 增加 `used_analyses_last_hour`、`remaining_analyses_this_hour`，并合并各 symbol 的持久化调度状态；cron 在 budget exhausted / interval gate / non-RTH / failed 场景都会写入状态。验证：wave2 focused tests 2 passed / `test_main.py + test_api.py + test_llm_advisor.py` 126 passed / basedpyright 0/0/0。 |
| 已完成 | **P36 Wave 1：Element Plus 自动按需装配与 chunk 收敛** | 已完成 2026-06-04 | 前端改为通过 `unplugin-vue-components` / `unplugin-auto-import` 按需装配 Element Plus，移除全局 `app.use(ElementPlus)`；桌面导航切换为 `router-link` 顶栏；Element Plus chunk 从 93 个降至 7 个，并保持单 chunk 预算 <500 KiB。验证：Element Plus chunk check RED→GREEN；navigation + dashboard Cypress 18 passed；frontend `type-check + build + build:check-element-plus + build:check-chunks` 通过。 |
| 已完成 | **P37 Wave 1：多标的历史/诊断组合式逻辑抽取** | 已完成 2026-06-04 | 新增 `useStatusHistorySeries` 与 `useDiagnosticsSnapshot`，统一 Dashboard/Review 的 symbol history 与 diagnostics 请求/错误/选择状态管理；页面渲染与接口行为保持不变。验证：`dashboard.cy.ts + review_runtime_history.cy.ts` 19 passed / frontend `build + build:check-element-plus + build:check-chunks` 通过。 |
| 已完成 | **P39 Wave 1：多标的诊断与历史调试导出** | 已完成 2026-06-04 | `ReviewService.export_review()` 扩展为 symbol 级 debug bundle：JSON 导出包含 `review`、`runtime_history`、过滤后的 `diagnostics`；CSV 导出包含 `review_day`、`history_point`、`history_marker`、`diagnostic_runtime`、`diagnostic_meta` 多 section 行。`/api/review/export` 会附带当前 runner diagnostics 的目标 symbol runtime 快照。验证：export focused tests 3 passed / `test_review.py + test_api.py` 70 passed / basedpyright 0/0/0。 |
| 已完成 | **P40 Wave 1：多标的全局控制审计范围落盘** | 已完成 2026-06-04 | start/stop/pause/resume/kill-switch/disable-kill-switch 现会把 `global_scope`、`primary_symbol`、`affected_symbols`、`runtime_count` 写入 `AuditLog.request_summary`，并同步生成 `CONTROL_*` `TradeEvent` 事件，补齐多标的控制可追溯性。验证：control scope RED→GREEN；`test_api.py` 64 passed / basedpyright 0/0/0。 |

### 后续迭代计划（2026-06-04 更新）

| 顺序 | 代号 | 主题 | 目标 | 验收口径 |
|------|------|------|------|----------|
| 1 | **P41** | 多标的 LLM 结果前端化 | 将 per-symbol LLM 调度状态、skip reason、last/next 分析时间接入 Dashboard/Review。 | 前端可直接识别哪个 symbol 因预算/冷却被跳过；无额外写操作。 |
| 2 | **P42** | 构建告警剩余清扫 | 针对 `@vueuse/core` pure annotation 与剩余 Element Plus circular chunk warnings 做定向清理。 | build 告警进一步减少；chunk 预算不回退；导航/仪表盘回归通过。 |
| 3 | **P43** | 多标的导出与图表联动整理 | 将 symbol-history、diagnostics、导出能力连成统一的 symbol 调试工作流。 | 不新增写操作；Review/Dashboard/导出之间 symbol 语义一致。 |
| 4 | **P44** | 多标的审计与导出联动 | 将控制审计、运行诊断与 symbol debug export 串联，形成完整事件追踪链。 | 一次运维动作可从审计追到 diagnostics/export，定位路径闭环。 |
| 5 | **P45** | 多标的运维时间线统一视图 | 把 `CONTROL_*` 审计事件、diagnostics 与 trade events 聚合到统一时间线展示。 | 运维可按 symbol/全局视角追溯一次控制动作后的系统演变。 |

> **P40 Wave 1 已交付完成。** 当前多标的全局控制动作已同时写入 audit 与 trade event，能回溯影响范围。
>
> 建议下一轮启动 **P41 多标的 LLM 结果前端化**：预算、skip reason、last/next 分析时间已在后端就绪，下一步优先把这些状态暴露到 Dashboard/Review 给运维直接查看。
>

**显式不做（与已有 YAGNI 决策保持一致）：**
- 交易所节假日历
- 审计 CSV/JSON 导出
- Webhook 模板编辑器
- 通知重发队列
- 高频交易 / 复杂择时指标
- 量化研究平台
---

## 原始规划记录（已交付部分保留作为历史）

以下章节记录最初提出的 5 个主题：**LLM智能区间调整**、**回测系统**、**实时图表与监控增强**、**日志审计与报警**、**响应式与移动端适配**。其中 LLM、回测与实时图表已经交付；当前执行优先级以本文上方 2026-05-25 更新为准，即先实施 **P4 交易执行安全与成本控制增强**。

---

## 迭代 0：LLM 智能区间调整 (LLM Intelligent Interval)

> **目标：** 通过 DeepSeek LLM 分析市场行情，自动为策略推荐买入/卖出价格区间，减少人工频繁调整的依赖。策略运行期间持续优化区间，实现动态自适应交易。
> **价值：** 核心交易决策智能化，显著降低人工调参成本，提升策略对不同市场环境的适应能力。
> **设计文档：** [docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md](docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md)

### 任务

#### 0.1 数据聚合与 LLM 顾问服务

- 新建文件：
  - `backend/app/services/data_aggregator.py`：聚合长桥历史行情、实时价格、持仓状态、最近成交记录，构建 LLM Prompt。
  - `backend/app/services/llm_advisor_service.py`：调用 DeepSeek API，解析 JSON 响应，防抖控制（30 分钟窗口）。
- 实现细节：
  - 输入数据：加权组合（7 天日 K 权重 60% + 24 小时分钟 K 权重 40%），附带 ATR、布林带指标。
  - Prompt 要求 LLM 输出 `suggested_buy_low`, `suggested_sell_high`, `confidence_score`, `analysis`。
  - 失败处理：API 超时 30s，重试一次，失败时通过 Server酱通知 "LLM 区间分析失败"。

#### 0.2 渐进式平滑过渡策略

- 新建文件：
  - `backend/app/services/interval_application_service.py`：核心规则引擎。
- 实现细节（**2026-05-25 更新：实际行为是"追价加仓"，非原"只放宽不收紧"**）：
  - **FLAT（空仓）**：LLM 建议立即生效。
  - **LONG（持多）**：sell_high 优先取 `max(old, new)`；若 `new_sell_high < old_sell_high`，强制不低于 `current_price * (1 + 波动阈值)` 防贴现价。new_buy_low 仅在 `≤ old_buy_low` 时下调（**允许 LLM 追价加仓**），不会上抬 buy_low。
  - **SHORT（持空）**：镜像 LONG —— buy_low 仅在 `≤ old_buy_low` 时下调；否则取 `min(new, current_price * (1 - 波动阈值))`，**允许 LLM 追价加空**。
  - **风控兜底**：置信度 < `llm_min_confidence`（默认 0.7）拒绝；区间宽度 > `llm_max_stripe_width_pct`（默认 8%）拒绝；区间宽度 < `min_exit_profit_pct * current_price` 或 `min_profit_amount/reference_quantity` 拒绝。

#### 0.3 定时触发与手动触发

- 新建文件：
  - `backend/app/crontabs/interval_analysis_cron.py`：APScheduler 定时任务（每 4 小时）。
- 新增 API：`POST /api/strategy/llm-interval/analyze`（手动触发），`GET /api/strategy/llm-interval/status`（状态查询）。

#### 0.4 前端 UI 集成

- 修改文件：
  - `frontend/src/views/Strategy.vue`：增加 LLM 智能区间卡片（开关、置信度显示、建议区间、上次应用原因）。
  - `frontend/src/views/Dashboard.vue`：状态栏增加 LLM 下次分析时间指示器。
- 新增 API Client：`frontend/src/api/llm_advisor.ts`。

#### 0.5 数据库迁移

- Alembic 迁移：为 `strategy_config` 表新增 `auto_interval_enabled`, `llm_suggested_buy_low`, `llm_suggested_sell_high`, `llm_confidence_score`, `llm_analysis`, `llm_last_analysis_at`, `llm_next_analysis_at`, `llm_applied_buy_low`, `llm_applied_sell_high`, `llm_applied_at`, `llm_reject_reason`。

#### 验证

- [ ] 后端 pytest：覆盖渐进式过渡规则、风控兜底、LLM 解析、防抖限制。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `strategy_llm_*.cy.ts` 和 `dashboard_llm_indicator.cy.ts`。
- [ ] Docker Compose 启动后，手动触发分析返回正确结构；30 分钟内重复触发被限制。

---

## 迭代 1：回测系统 (Backtesting)

> **目标：** 消除 README 中"暂不支持回测系统"的限制。实现策略的历史数据回测验证，帮助用户在实盘运行前验证参数合理性。
> **价值：** 显著降低盲目上线风险，是量化交易策略上线前的核心验证环节。

### 任务

#### 1.1 设计回测数据接口与数据层

- 新建文件：
  - `backend/app/core/backtest.py`：回测引擎（纯数据驱动的策略推演）。
  - `backend/app/api/backtest.py`：FastAPI 路由：`POST /api/backtest/run`。
  - `backend/app/schemas.py`：新增 `BacktestResult`, `BacktestParams`, `BacktestTradeLog`。
  - `backend/tests/test_backtest.py`：单元测试。
- 实现细节：
  - `BacktestEngine` 不依赖实时的长桥行情，而是接收历史价格序列和策略参数，逐 K 线推演状态机（flat -> long -> flat 或 flat -> short -> flat），输出每笔虚拟成交。
  - 价格序列由用户提供（CSV 上传）或长桥历史行情接口获取（二期可选；一期先用用户上传）。
  - 回测需要模拟风控逻辑（日亏损、连续亏损），但不触发真实通知和真实下单。

#### 1.2 前端回测页面

- 新建/修改文件：
  - `frontend/src/views/Backtest.vue`：策略参数选择与历史数据上传界面。
  - `frontend/src/api/backtest.ts`：`runBacktest` API 调用。
  - `frontend/src/router/index.ts`：新增 `/backtest` 路由，导航栏加入"回测"入口。
- 实现细节：
  - 支持配置回测参数（与实盘策略参数共用 `StrategyConfig`，但可选覆盖）。
  - 上传 CSV（格式：`timestamp,open,high,low,close,volume`）。
  - 提交后端生成回测结果，前端以表格和图表展示。

#### 1.3 回测结果可视化

- 前端新增 `frontend/src/components/BacktestChart.vue`：
  - 采用折线图展示标的收盘价与买卖信号点。
  - 采用柱状图展示每日盈亏（PnL）。
  - 表格展示每笔虚拟成交记录（时间、动作、价格、数量、盈亏）。
  - 汇总指标：总收益率、最大回撤、夏普比率、胜率、盈亏比。

#### 验证

- [ ] 后端 `test_backtest.py` 通过（至少覆盖 flat-long-flat 与 flat-short-flat 两条主路径）。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `backtest.cy.ts` 覆盖页面交互。
- [ ] Docker Compose 全栈启动后，`curl -X POST http://localhost:8000/api/backtest/run` 返回正确结构。

---

## 迭代 2：实时图表与监控增强 (Dashboard Visualization)

> **目标：** Dashboard 从纯文本仪表盘升级为带实时折线图、盈亏曲线的监控中心。
> **价值：** 将离散的价格/盈亏数字转化为时间序列图形，帮助用户快速判断策略当前状态和历史走势。
> **状态：** ✅ 已完成。实现采用项目内轻量 SVG 图表组件，避免引入新的前端网络依赖。

### 任务

#### 2.1 后端增加历史状态查询接口

- 新建/修改文件：
  - `backend/app/api/status.py`：新增 `GET /api/status/history?from=...&to=...`，按区间查询历史 `RuntimeState` 快照。
  - `backend/app/services/runtime_state_service.py`：补充 `query_history` 方法。
- 实现细节：
  - 基于 `orders` 表和 `runtime_state` 表的数据，按时间范围聚合返回价格/盈亏序列。

#### 2.2 Dashboard 实时价格与盈亏曲线

- 新建/修改文件：
  - `frontend/src/components/PriceChart.vue`：实时价格折线图，随 WebSocket 消息更新。
  - `frontend/src/components/PnLChart.vue`：盈亏柱状图/面积图，随 WebSocket 消息更新。
  - `frontend/src/views/Dashboard.vue`：引入图表组件，重新排布布局。
- 实现细节：
  - 图表库选择：优先使用 `vue-echarts`（轻量、与 Vue 3 生态兼容），替代重量级图表库。
  - 数据点缓存限制：前端保留最近 200 个数据点，保持内存和渲染性能。
  - 图表需要区分"历史加载"（页面打开时 REST 查询）和"实时追加"（WebSocket 推送）。

#### 2.3 交易信号标记

- 图表上标记策略触发的买入/卖出点位，以箭头和颜色区分。
- 数据来源：页面打开时加载 `GET /api/orders` 的最近成交记录，并在 WebSocket 收到新的风控通过+下单事件时实时追加标记。

#### 验证

- [x] 前端 `npm run build` 通过。
- [x] Dashboard 打开后图表能加载历史状态数据，并通过状态流追加最新价格/盈亏点。
- [x] Cypress 新增 `dashboard_charts.cy.ts` 页面校验图表与交易信号标记渲染。

---

## 迭代 3：日志审计与报警系统 (Audit Logging & Alerting)

> **目标：** 由当前的 Server酱单一通知渠道，升级为支持自定义 Webhook、Email 的多渠道报警系统；并增加操作审计日志。
> **价值：** 生产环境运维需要更灵活的报警通道（如企业微信、钉钉、Slack）；操作审计满足合规与排障需求。

### 任务

#### 3.1 操作审计日志

- 新建/修改文件：
  - `backend/app/models.py`：新表模型：`AuditLog`（`id`, `action`, `actor`, `detail`, `ip`, `created_at`）。
  - `backend/app/core/audit.py`：`AuditLogger`，封装审计日志记录逻辑。
  - `backend/app/api/*.py`：核心操作（启动/停止/暂停/Kill Switch/策略修改/凭证修改）调用 `AuditLogger.record(...)`。
  - `backend/alembic/versions/`：生成数据库迁移脚本。
- 实现细节：
  - `action` 枚举：`START`, `STOP`, `PAUSE`, `RESUME`, `KILL_SWITCH`, `STRATEGY_UPDATE`, `CREDENTIALS_UPDATE`。  
  - `actor`：从请求头中读取 `X-API-Key` 的哈希（不存明文）。
  - `ip`：读取请求 `X-Forwarded-For` 或 `client.host`。

#### 3.2 通知渠道扩展

- 新建/修改文件：
  - `backend/app/core/notify.py`：重构为策略模式：`NotifierInterface`。
  - `backend/app/core/notifiers/`：新目录，包含：
    - `serverchan.py`：现有 Server酱实现。
    - `webhook.py`：通用 Webhook POST 通知（允许用户配置任意 URL 和模板）。
- 实现细节：
  - 凭证配置中新增 `notification_channel` 字段（`serverchan` | `webhook`），后端根据配置实例化对应 Notifier。
  - 前端 Credentials 页面增加通知渠道选择表单。

#### 3.3 风控事件报警分级

- 修改：
  - `backend/app/core/risk.py`：不同级别事件调用不同的通知方法（如风控暂停 vs Kill Switch 可分别指定通知级别）。
- 实现细节：
  - `notify_risk_event` 增加 `severity` 参数（`WARNING`, `CRITICAL`）。
  - `CRITICAL` 级别同时触发所有配置的通知渠道（Server酱 + Webhook），确保不遗漏。

#### 验证

- [ ] 后端新增 `test_audit.py` 和 `test_notifiers.py`，测试覆盖日志写入与通知发送。
- [ ] Docker Compose 启动后，修改策略能触发日志写入；触发风控能收到 Webhook 消息。
- [ ] Cypress 凭证页面可切换通知渠道。

---

## 迭代 4：响应式与移动端适配 (Responsive & Mobile)

> **目标：** 让前端在移动浏览器上有可用的操作体验。
> **价值：** 用户需要随时随地查看策略状态或执行 Kill Switch，移动端是高频场景。

### 任务

#### 4.1 前端响应式布局

- 修改文件：
  - `frontend/src/App.vue`：侧边栏导航改为可折叠或底部 Tab 栏（移动端）。
  - `frontend/src/views/Dashboard.vue`：关键指标以卡片流排布，图表高度自适应，控制按钮增大触控区域。
  - `frontend/src/views/Strategy.vue`：表单增加移动端单列布局。
  - `frontend/src/views/Credentials.vue`：同策略页面。
- 实现细节：
  - 使用 CSS Media Queries 与 Element Plus 的 `el-col` 响应式断点。
  - 隐藏非必要的复杂图表（如回测图表在移动端可折叠）。

#### 4.2 PWA 基础支持

- 修改文件：
  - `frontend/index.html`：增加 `manifest.json` 链接。
  - `frontend/public/manifest.json`：Web App Manifest。
  - `frontend/vite.config.ts`：使用 `vite-plugin-pwa` 配置 Service Worker（离线缓存静态资源和基础页面）。
- 实现细节：
  - 离线时页面仍然可以打开（前端缓存），但 API 调用提示"网络不可用"。
  - 目标不是完全离线可用，而是保证页面壳体和已缓存数据的离线访问。

#### 4.3 移动端安全优化

- `nginx.conf`：增加移动设备相关的响应头优化（`X-Content-Type-Options`, `Referrer-Policy`）。
- 登录/凭证页面：移动端自动隐藏密码输入框的保存提示，防止凭证泄露到设备键盘记忆。

#### 验证

- [ ] 使用 Chrome DevTools 模拟 iPhone 14 Pro、Pixel 7 设备，页面无横向滚动、按钮可点击、文字不溢出。
- [ ] 在移动 Safari 上测试 PWA "添加到主屏幕" 后图标和启动画面正确。
- [ ] Cypress 增加移动端视口尺寸的烟雾测试（`cy.viewport('iphone-x')`）。

---

## 原始迭代交付状态摘要

| 迭代 | 主题 | 当前状态 | 备注 |
|------|------|------|------|
| 0 | LLM 智能区间调整 | ✅ 已交付并经 2026-05-25 数据真实性修正 | 保留追价加仓规则 |
| 1 | 回测系统 | ✅ 已交付 | P4 仅补跳过分类 |
| 2 | 实时图表与监控增强 | ✅ 已交付 | Dashboard 已可用 |
| 3 | 日志审计与报警 | 待后续实施 | 排在 P4 之后 |
| 4 | 响应式与移动端适配 | 待后续实施 | 排在 P4 之后 |

## P149+ 平台/量化深度迭代（自主 10 轮 × 多批）

| 批次 | 范围 | 状态 | 备注 |
|------|------|------|------|
| P149–P158 | 平台插件 SDK + 事件流 + 组合/Paper/风控基础 | ✅ 已交付 | PlatformRunner + PaperBroker + Portfolio |
| P159–P172 | 研究级分析 + 执行深度（Sizer/指标/Universe/优化器/OMS/持仓/构造/账本/调度/MC） | ✅ 已交付 | 参考 Nautilus/Lean |
| P173–P182 | 机构级编排（OMS/持仓/构造/预热/账本/调度/MC/投影/多币种） | ✅ 已交付 | 事件溯源 CQRS |
| P183–P192 | 算法执行 + 研究层 + tearsheet + 延迟 | ✅ 已交付 | TWAP/VWAP/Brinson/智能寻优 |
| P193–P202 | 分布式总线 / 订单簿 / 连续合约 / 因子研究 / 隔离 / 衰减 / TCA / SVI / PBO/DSR / 多期 Brinson | ✅ 已交付 | 纵深补齐 |
| P203–P212 | 风险科学 + 投资组合优化（LW 收缩 / Markowitz / BL / HRP / VaR / 回撤 / 比率族 / Pain / 肥尾） | ✅ 已交付 | 参考 PyPortfolioOpt/Jorion |
| P213–P222 | 风险研究 II（regime/CPCV/风格分析/换手优化/风险预算/MFE-MAE/IS/收益日历/压力报告/稳定性） | ✅ 已交付 | 参考 Nautilus/López de Prado |
| P223–P232 | 配对/微观/执行/风险 III（协整/Kelly/GARCH/VPIN/Almgren-Chriss/Hawkes/历史压力/因子风险/Sobol/EVT） | ✅ 已交付 | 参考 Engle-Granger/McNeil-Frey |
| P233–P242 | 因果/诊断 IV（Granger-PCMCI/HMM/copula/回撤预测/流动性/动量因子/组合分解/SPA/执行质量/分散度） | ✅ 已交付 | 参考 Hamilton/Sklar/Hansen-White |
| P243–P252 | 期权/随机过程/鲁棒/路由/多元相依 V（期权定价+Greeks/IV+SVI/Kalman+RTS/GBM-OU-CIR-Merton/统计套利/鲁棒统计/Bandit/LOESS/SOR/Vine Copula） | ✅ 已交付（2026-06-26） | 参考 QuantLib/filterpy/SMPyBandits/pyvinecopulib |

### P243–P252 交付详情（2026-06-26）

10 个新 `app/platform/` 模块 + 10 个新 `/api/platform/*` 端点，纯 Python、零新依赖、确定性（`random.Random(seed)`）、可选注入、默认路径零行为变更：

- **P243** `options_pricing.py` — Black-Scholes-Merton 欧式 call/put 闭式 + 全 Greeks（Δ/Γ/ν/Θ/ρ/vanna/volga）+ Merton 连续分红；`POST /api/platform/options-pricing`。
- **P244** `implied_volatility.py` — BS 隐含波动率（Brenner-Subrahmanyam 初值 + Newton-Raphson + bisection 回退 + 无套利边界）+ Gatheral raw-SVI 5 参 NLSQ 拟合（Gauss-Newton/LM + 投影约束）；`POST /api/platform/implied-volatility`（`mode=iv`/`mode=svi`）。
- **P245** `kalman_filter.py` — 线性 Kalman 滤波（Joseph 形式）+ RTS 固定区间平滑 + 静态/逐步矩阵 + 可选控制输入；`POST /api/platform/kalman-filter`（`smooth?`）。
- **P246** `stochastic_processes.py` — GBM/OU/CIR/Merton 跳跃扩散 Euler 仿真（Box-Muller + Knuth 泊松）+ 解析矩；`POST /api/platform/stochastic-processes`。
- **P247** `stat_arb_signals.py` — 距离法 spread + z-score 进出场（LONG/SHORT/FLAT + 滞回）+ 复用 P223 OU 半衰期；`POST /api/platform/stat-arb-signals`。
- **P248** `robust_statistics.py` — MAD/winsorize/trimmed mean/Theil-Sen/Huber IRLS；`POST /api/platform/robust-statistics`。
- **P249** `bandits.py` — ε-greedy/UCB1/Thompson Beta+Gaussian + regret；`POST /api/platform/bandits`。
- **P250** `loess.py` — Cleveland LOWESS（tricube + 鲁棒 bisquare 迭代）；`POST /api/platform/loess`。
- **P251** `smart_order_routing.py` — 多 venue L1 最优价贪婪 + tick 量化 + 拆单 + WAP/费用；`POST /api/platform/smart-order-routing`。
- **P252** `vine_copula.py` — C-vine/D-vine 逐对 copula（Gaussian/Gumbel/Clayton，复用 P235）+ 对数似然 + AIC/BIC；`POST /api/platform/vine-copula`。

新增共享数学 `_math_utils.py`（`norm_cdf`/`norm_inv` Acklam/`norm_pdf`，无 scipy），供 P243/P244/P246/P252 复用。全量回归 2431 passed（基线 +417 新增）；`basedpyright app/platform/` 0/0/0。下一批 P253+ 留待后续。

---

## 实施建议

1. **一次只做一条主线。** 每个迭代内可以并行开发互不影响的子任务（如前后端可并行），但不要在同一个代码基上交叉进行两个迭代的开发，避免功能碎片化。
2. **每个迭代产出物：**
   - 一个保存在 `docs/superpowers/plans/` 的实施计划文件（可参考已有格式）。
   - 完成后更新此 Roadmap，标记对应迭代完成状态。
3. **P4 交易执行安全与成本控制是当前最高优先级**，因为回测和 Dashboard 已交付，下一风险集中在真实下单的费用、LLM 替换挂单与可解释性。
4. **测试纪律：** 每个新增功能必须有对应测试（后端 unit test + 前端 Cypress E2E），覆盖率不低于现有的 80% 水平。

---

## 附录：原始设计中的非目标与当前状态对照

| 非目标（原始） | 当前状态 | 计划 |
|---|---|---|
| 多标的组合策略 | ❌ 仍不支持 | 超出当前 Roadmap，需更大架构升级 |
| 高频交易 | ❌ 仍不支持 | 设计限制，暂不改变 |
| 复杂择时指标 | ❌ 仍不支持 | 暂不改变 |
| 回测系统 | ❌ -> ✅ 计划中 | **迭代 1** 实现 |
| 量化研究平台 | ❌ 仍不支持 | 超出范围 |
| 代客理财/公开策略分发 | ❌ 仍不支持 | 法律合规限制，永久不在计划中 |
