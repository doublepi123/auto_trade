# P243–P252：期权定价 + 随机过程 + 鲁棒统计 + 路由 + 多元相依（10 轮深度迭代）

> 承接 P149–P242（100 个平台模块，事件流 / 插件 SDK / 回测 / 执行抽象 / 组合 / 风控 / 归因 / 分析 / 研究层 / 机构级编排 / 风险科学 I–IV）。本批 10 轮填补 Roadmap 显式 YAGNI 与其他开源量化核心：**期权定价与 Greeks、隐含波动率与 SVI 波动率面、Kalman 滤波、随机过程 / SDE、统计套利信号、鲁棒统计、Bandit 策略选择、LOESS 局部回归、智能订单路由、Vine Copula 多元相依**。每轮一个聚焦、可测、可独立合入的后端特性。

## 开源参考映射

| 能力域 | 参考开源项目 | 对应迭代 |
|--------|-------------|----------|
| 期权定价 + Greeks（BSM 闭式） | QuantLib `BlackScholesProcess`、py_vollib | P243 |
| 隐含波动率 + SVI 波动率面 | QuantLib SVI、Let's Be Rational、Gatheral 2004 | P244 |
| Kalman 滤波 + 状态空间 + RTS 平滑 | filterpy、pykalman | P245 |
| 随机过程 / SDE（GBM/OU/Merton-JD/CIR） | QuantLib `StochasticProcess`、sdeint | P246 |
| 统计套利信号（距离法 + z-score + OU 半衰期） | Avellaneda-Lee 2008、ArbitrageLab | P247 |
| 鲁棒统计（MAD/winsorize/trimmed/Theil-Sen/Huber） | statsmodels.robust | P248 |
| Bandit 策略选择（ε-greedy/UCB1/Thompson） | SMPyBandits | P249 |
| LOESS/LOWESS 局部回归（Cleveland tricube + 鲁棒迭代） | statsmodels.nonparametric.lowess | P250 |
| 智能订单路由（SOR：多 venue 最优盘路由 + 拆单） | Nautilus `OrderRouting`、FIX | P251 |
| Vine Copula 多元相依（C-vine / D-vine） | pyvinecopulib、VineCopula | P252 |

## 10 轮清单

| 代号 | 主题 | 一句话 | 参考 |
|------|------|--------|------|
| **P243** | 期权定价 + Greeks | Black-Scholes-Merton 闭式（call/put + Δ/Γ/ν/Θ/ρ/vanna/volga），欧式，连续与离散分红 | QuantLib `BlackScholesProcess`、py_vollib |
| **P244** | 隐含波动率 + SVI 波动率面 | BS 隐含波动率（Brenner-Subrahmanyam 初值 + Newton-Raphson，无 scipy）+ Gatheral SVI 5 参原始参数化拟合（NLSQ via Gauss-Newton） | QuantLib SVI、Let's Be Rational |
| **P245** | Kalman 滤波 + 状态空间 | 线性 Kalman predict/update + RTS 固定区间平滑 + 可选 EM 一步参数估计 | filterpy、pykalman |
| **P246** | 随机过程 / SDE | GBM / OU / Merton 跳跃扩散 / CIR 解析矩与 Euler-Maruyama 仿真（注入 `random.Random(seed)`） | QuantLib `StochasticProcess`、sdeint |
| **P247** | 统计套利信号 | 距离法协整 spread + z-score 进出场阈值 + 复用 P223 OU 半衰期 + Avellaneda-Lee 信号 | Avellaneda-Lee 2008、ArbitrageLab |
| **P248** | 鲁棒统计 | MAD / winsorize / trimmed mean / Theil-Sen 斜率 / Huber M-estimator IRLS | statsmodels.robust |
| **P249** | Bandit 策略选择 | ε-greedy / UCB1 / Thompson sampling（Beta + Gaussian 臂）+ regret 界 + 注入 seed | SMPyBandits |
| **P250** | LOESS/LOWESS 局部回归 | Cleveland tricube 核加权局部线性回归 + 鲁棒迭代重加权 + 置信带 | statsmodels.nonparametric.lowess |
| **P251** | 智能订单路由 (SOR) | 多 venue 最优买卖盘聚合 + tick 规则 + venue 成本/费率 + 最小拆单计划 | Nautilus `OrderRouting`、FIX |
| **P252** | Vine Copula 多元相依 | C-vine / D-vine 逐对 copula 构造（复用 P235 Gumbel/Clayton/Gaussian）+ 对数似然 + AIC/BIC | pyvinecopulib、VineCopula |

## 设计原则

1. **参考而不照抄**：借鉴开源抽象形态，实现贴合本仓事件流与既有原语（`PlatformRunner`/`PaperBroker`/`Portfolio`/`EventBus`/`cointegration`/`copula`），零新依赖。
2. **加法不破坏**：所有新模块均为可选注入；默认路径零行为变更。
3. **纯 Python 零新依赖**：numpy 仍不引入；RNG 一律 `random.Random(seed)`；无 scipy（正态 CDF/逆 CDF 复用 `risk_metrics` 的 Acklam 逆正态与 erf 有理逼近或本仓既有实现）。
4. **事件先行 / 离线可测**：每轮纯后端可独立离线使用，与 runner 解耦。
5. **`_ensure_*` 同步**：本批默认**无新表**（均为无状态计算函数）；若某轮需持久化，同步迁移。
6. **YAGNI**：不做前端 UI、不做真实 ML 训练、不做 L2 暗池撮合、不做美式期权 PDE/二叉树（本轮仅欧式闭式）、不做 calibration 到真实期权链。

## 每轮交付物

- `backend/app/platform/<module>.py`：纯 Python 模块，含 dataclass 结果对象 + `to_dict()` + 自由函数。
- `backend/app/platform/api.py`：新增 `POST /api/platform/<endpoint>`，`require_api_key` + 422 校验。
- `backend/tests/platform/test_<module>.py`：单元测试（数值正确性 + 边界 + 422）。
- `backend/tests/platform/test_api_risk_portfolio.py`：追加端点 200/422 测试（沿用既有 harness）。
- `CLAUDE.md` / `README.md` / `docs/Roadmap.md`：末轮统一同步。

## 验收口径

每轮交付后必须满足：

- `cd backend && python3 -m pytest tests/platform/test_<module>.py tests/platform/test_api_risk_portfolio.py -v` 全绿。
- `cd backend && python3 -m basedpyright app/platform/` 0 真实错误 / 0 warnings / 0 notes。
- 不引入 numpy / scipy / pandas / sklearn 等新依赖。
- 末轮：`pytest tests/ -q` 不低于基线（2014 passed）+ `basedpyright` 0/0/0 + 文档同步 + 单 commit 合入。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 无 scipy 的特殊函数（erf / Φ / Φ⁻¹） | 复用 `risk_metrics` 的 Acklam 逆正态 CDF；erf 用 Abramowitz-Stegun 7.1.26 有理逼近（已在 `fat_tail`/`overfitting` 等模块验证可用） |
| SVI NLSQ 收敛性 | 用 Gauss-Newton + Levenberg-Marquardt 阻尼，参数边界约束（`a>0, b≥0, ρ∈[-1,1], m∈ℝ, σ>0`），迭代上限 + 残差单调性回退 |
| Kalman 数值稳定 | 协方差对称化 + Joseph 形式更新 + Cholesky-free（纯标量/小矩阵） |
| Bandit 确定性 | 全程 `random.Random(seed)`；Thompson Beta/Gaussian 共轭闭式采样 |
| Vine copula 组合爆炸 | 仅 C-vine / D-vine 两类树结构；逐对 copula 复用 P235；n≤8 限定 |
| SOR 路由语义 | 纯计算：聚合多 venue L1 盘口 → 最优价 + 拆单计划，不接真实 broker |

## 验收命令

```bash
# 每轮末
cd backend && python3 -m pytest tests/platform/test_<module>.py tests/platform/test_api_risk_portfolio.py -v
cd backend && python3 -m basedpyright app/platform/<module>.py app/platform/api.py

# 末轮全量
cd backend && python3 -m pytest tests/ -q      # 期望 ≥ 2114 passed
cd backend && python3 -m basedpyright            # 0/0/0
```

## 非目标

- 不做前端 UI / Cypress。
- 不做真实期权链 calibration / 美式期权 / 波动率模型校准到市场数据。
- 不做真实 broker SOR 接线（纯计算层）。
- 不重启 P2（API 鉴权）。
- 不开 P253+（留作下批 10 轮）。

## 备注

- 本批延续 P203–P242 的"风险科学 / 研究层"主线，进入**期权 / 随机过程 / 鲁棒统计 / 多元相依**四个新能力域。
- 模块命名沿用既有约定（`options_pricing.py`、`implied_volatility.py`、`kalman_filter.py`、`stochastic_processes.py`、`stat_arb_signals.py`、`robust_statistics.py`、`bandits.py`、`loess.py`、`smart_order_routing.py`、`vine_copula.py`）。
- 端点命名：`/options-pricing`、`/implied-volatility`、`/kalman-filter`、`/stochastic-processes`、`/stat-arb-signals`、`/robust-statistics`、`/bandits`、`/loess`、`/smart-order-routing`、`/vine-copula`。