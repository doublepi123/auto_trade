# Strategy v2 信号预注册合同（PREREGISTRATION）

> 版本：v1（2026-08-30 冻结）。本文件故意放在包内以便版本控制；仓库约定 `docs/` 下的中间文档不入库，本合同不属于中间文档。
> 配套机制：`backend/tests/test_strategy_v2_preregistration.py`（参数哈希冻结测试）。

## 0. 这是一份什么文件

这是一份治理合同，不是研究笔记。读完它，你必须能直接回答一个问题：**我现在是否被允许继续做这件事**。回答不了，就按「不允许」处理。

适用背景：当前为 PAPER / SIMULATION 账户。本合同不是紧急止损措施，而是工程纪律：纸上环境恰恰是发现「信号没有 edge」的正确场所，本合同保证这个发现是诚实的、不会被调参调没。它要防止的具体失败模式是：**悄悄调参直到证据变好看**。那只是加了额外步骤的过拟合。

## 1. 当前证据结论（冻结基线）

Strategy v2 前向影子证据（248 笔已平仓交易，2026-07-14 至 2026-08-28）：

- gross PnL −34.83，费用 103.79，net PnL −138.62，胜率 37.5%。
- 当前版本子集（232 笔 / 24 个交易日）的 day-clustered 95% CI：gross 均值 −0.57 bps，CI [−5.66, +4.53]；net 均值 −10.57 bps，CI [−15.66, −5.48]。
- 往返成本约 10 bps，因此**即使 gross 的乐观上界也覆盖不了成本**。
- first-passage 检验：34 次先触 target、79 次先触 stop，target-first 比例 30.1%，低于该版本自己的 driftless 基准 `stop/(stop+target)` = 0.45/1.25 = 36.0%。

结论：**没有大到可交易的 edge 被证实；且一个连 no-information 基准都过不去的信号，任何出场或参数重调都救不回来。** 这就是冻结 v5 作为负对照的理由。

## 2. 硬前置条件（已满足）

`signal_edge` 门（`domain/strategy_v2/signal_edge.py` + `GET /api/strategy-shadow/signal-edge`）的三个缺陷已在本次迭代修复，任何新信号工作以此为先决条件：

1. gross / net 分离，产出独立的 fee-blocked 判定（费用阻挡与信号本身错误不再混为一谈）。
2. cluster-robust 估计量改为按交易加权，取代等权日均值。
3. first-passage 二项检验加入 barrier-version 过滤，只在同一 stop/target 屏障版本内计数。

## 3. 晋级门槛（promotion 必须同时满足全部四条）

1. **净值显著为正**：net 收益的 day-clustered 双侧 95% CI 下界 **> 0**（按第 3.2 节使用 `df = D − 1`；gross 一并报告，对照固定的约 10 bps 往返成本模型）。
2. **版本专属 first-passage**：该版本实测 target-first 比例的下界，高于**该版本自己**的 driftless 基准 `stop/(stop+target)`。换屏障就换基准，不许跨版本借用。
3. **样本量**：**≥ 60 个独立交易日** 且 **约 180 个独立已结算 bracket 结果**。功效说明：在观测到的约 20 bps 日离散度下，以 80% power 检出 +5 bps 的 net edge，大约需要 **100 个独立交易日**。样本不够就是不够，结论只能是 `INSUFFICIENT_DATA`。
4. **Deflated Sharpe Ratio**（Bailey & López de Prado 2014）：按已注册的试验次数校正后，在 95% 水平上与运气可区分（`distinguishable_from_luck`）。**2026-09-06 实现纠错**：既有实现以 `z > 0`（第 50 百分位）认证，未满足本合同约定的 95% 水平；现纠正为 `Φ(z) ≥ 0.95`（`z ≥ 1.644853626951`，计算不截断临界精度）。`POST /api/backtest/sweep` 的 `multiple_testing` 块分别报告保留原值的 z-score（`deflated_sharpe`）与概率（`dsr_probability`），并保留 PSR 与未测量试验方差的失败关闭守卫。这是**不符合合同的实现之纠正，认证变得更严格**，不是放宽合同。Strategy v2 前向影子 cohort 尚无已注册试验次数与已记载的选择族，**AND #4 继续失败关闭**；本次仅纠正接线前的底层计算，不接入前向影子晋级门，不启用晋级。

四条是 AND 关系。任何一条不过，不晋级。

### 3.1 晋级门的机器执行（2026-09-05 修订）

`domain/strategy_v2/signal_edge.py` 以独立的 `promotion` 块机械计算四条 AND；
样本门固定为 `PROMOTION_MIN_DISTINCT_DAYS = 60` 与
`PROMOTION_MIN_RESOLVED_BRACKETS = 180`，调用方的查询参数不能降低它们。
分析下限（30 个已结算 bracket / 20 个交易日）只决定统计诊断何时足以报告，
与晋级下限（60 个交易日 / 180 个已结算 bracket）明确分离，二者互不替代。
原有 `PASS` / `FAIL` / `FEE_BLOCKED` / `INSUFFICIENT_DATA` 与 futility 诊断保持原义；
分析 `PASS` 不等于具备晋级资格，在线换标的门还必须要求 `promotion.eligible`。

第 4 条目前因前向影子 cohort 的 DSR **尚未计算而失败关闭**，不得伪造数值，
也不得从纯 domain 层导入 platform 实现。后续日级 DSR 必须取
**T = 独立交易日数，而不是交易笔数**（例如当前是 28 天，不是 276 笔）；
把同日相关交易当成独立样本会低估 Sharpe 标准误并制造虚假置信度
（Bailey & López de Prado 2014）。本修订不改变冻结 v5 参数与负对照运行。

### 3.2 日聚类临界值的有限样本修正（2026-09-05 修订）

CR1 标准误的渐近依据是**独立交易日簇数 D**，不是交易笔数；固定 `2.0`
不能代表所有 D 的 95% 临界值。依据 Petersen (2009), *Estimating Standard Errors
in Finance Panel Data Sets*, Review of Financial Studies 22(1): 435–480，以及
Cameron, Gelbach & Miller (2011), *Robust Inference with Multiway Clustering*,
Journal of Business & Economic Statistics 29(2): 238–249，本次将统计诊断默认值与
晋级 AND #1 明确统一为 **Student-t 双侧 95%（p=0.975），df = D − 1**。
这是推断口径修正，不声称 Student-t 能消除小簇样本 CR1 的全部偏差。

`DAY_CLUSTER_T95_BY_DF` 是预注册不可变固定表，覆盖 df=1..120（D=2..121），
保留 12 位小数；D=28 时临界值为 `2.051830516480`，不再使用偏宽松的 `2.0`。
表由 `math.lgamma`、Lentz 连分式的正则化不完全 beta 函数与 CDF 二分反演生成；
测试逐项复核，并以 quant-v6 已冻结的 29 项单侧 p=0.90 表及大 df 正态极限
交叉验证。运行时不引入 SciPy 或数值求根器。越过表范围必须硬报错，禁止静默回退；
扩表需另一次书面修订。D<2 的诊断仍报告统计量不可用，不伪造置信区间。

API、service 与 domain 的 `t_critical` 缺省均为 `None`，缺省才查表；显式数值
覆盖仅保留统计诊断的兼容行为。gross/net/clustered 披露实际 `t_critical` 与
`degrees_of_freedom`；晋级 AND #1 独立查预注册表，不读取查询参数或传入 CI。
60 天 / 180 个已结算 bracket 下限及 DSR 失败关闭不变。df≥61 时该精确临界值
略低于旧的 2.0，这是同一固定置信水平的数学结果，不是按观察结果调门槛。

**futility 完全不改。** `DEFAULT_T_CRITICAL = 2.0` 与弃置上界的固定 `2.0·SE`
继续保留；不得把弃置上界改为 df-aware，也不得让查询参数操纵它来诱导 `FUTILE`。
这是防止弃置判定被操纵的独立约束，不与晋级共用可调阈值。第 9 节的功效规则仍然
随 D 变化，本修订不改变其行为。冻结 v5 参数、参数哈希、负对照收集与 P0 边界均不变。

## 4. 证据时钟（evidence clock）

- **任何参数变更都把证据收集窗口归零**。此前的交易日与已平仓交易全部归属旧参数集，不得计入新参数集的晋级判定。
- `INSUFFICIENT_DATA` 永远不得被压制、改写或四舍五入成 PASS 或 FAIL。
- 机制保障：`backend/tests/test_strategy_v2_preregistration.py` 对冻结的 v5 参数集计算规范化 SHA-256，并与记录常量 `f6b76a03dea9ad4b2513bd6e171bc2070db18eee67bd895de0683edde19061db` 比对。**任何参数变更都会让 CI 失败**，强制一次刻意的书面决定：要么回退，要么先注册新假设、分配新 `algorithm_version` / `config_version`，再在同一提交里更新哈希与本文件。绝不允许为了消红而改哈希。
- 哈希字段范围（显式枚举，详见测试模块 docstring）：`algorithm_version`、`CAUSAL_ENTRY_FILL_OFFSET_BARS`、入场门（zscore 窗口与阈值、ADX、realized-vol、`residual_sigma_min`、`arm_ttl_bars`）、bracket 与时段出场（`stop_loss_pct` / `profit_target_pct` 的美国种子值、`max_holding_minutes`、entry cutoff、flatten window、`max_entries_per_day`、`entry_cooldown_minutes`、`settlement_grace_seconds`、`virtual_quantity`）、成本模型（`slippage_bps`、`estimated_fee_rate_us/hk`、`DEFAULT_EDGE_SAFETY_BUFFER_BPS`、`min_net_reward_risk_ratio`）。显式排除：`enabled` 等运行开关（不改变信号语义）、`symbol`（身份）、`updated_at`（易变）、challenger 与 review 阈值常量（属于别的假设与评估政策，不属于被冻结的 v5 信号）。

## 5. 负对照条款（negative control）

当前 v5 参数**原样继续运行，一行不动**。它就是负对照：如果未来某条流水线把 v5 认证为「有 edge」，**坏的是流水线，不是策略**。届时先怀疑评估代码、显著性口径与多重检验校正，再谈策略。

## 6. 一次一个假设，先注册后评分

任何新信号工作必须**在评分任何数据之前**以书面形式预注册：机制（mechanism）、标的池（universe）、入场 / 出场屏障（entry/exit barriers）、成本模型（cost model）。一次只注册一个假设。没有书面预注册就评分，等于放弃本次证据资格。

## 7. 即使通过认证之后

首次重新启用实盘也必须：最小仓位、经过强制的 pre-submit 风控边界、具备正式的交易状态（trading state）、并有人工每日签字确认。**自动晋级永久禁止**（常驻 P0 不变量），没有例外开关。

## 8. 常驻 P0 不变量与消费者

- 不做空开仓、不加仓、LLM 永不实盘下单、shadow / challenger 路径永不下单且永不自动晋级。
- 明确消费者：`AUTO_TRADE_AUTO_PRIMARY_SWITCH_REQUIRE_SIGNAL_EDGE=true` 是一个在线的 fail-closed 门，消费 `signal-edge` 判定；无法评估同样拦截。本合同的晋级口径与该门的判定口径一致：信号未被证明有 edge 时，趋势占比与 reach-rate 证据只是在给噪声排名。

## 9. 弃置门槛（kill / abandon，promotion 的对称出口）

第 3 节只规定了「什么时候可以晋级」。一台只有一个出口的证据机器，**按构造会永远运行下去**：每一次不显著的读数都被翻译成「再收一点证据」，于是一个已经死掉的信号可以无限期消耗工程时间。本节补上对称的另一个出口。它和晋级门一样是**客观可判定的**，不是感觉。

### 9.1 当前状态的诚实陈述（不得夸大）

统计状态：**两个方向都没有被证明**。

- first-passage：n = 132 已结算 bracket，target-first 44 / stop-first 88，p = 0.2583，**不显著**。
- day-clustered gross：t ≈ −0.6，**不显著**。

因此**不允许**把当前结论写成「已证明为负」。可以证明的只有一件事，见 9.2：**已证明「大到能覆盖成本」的 edge 不存在**。这是一个关于**幅度**的结论，不是关于**符号**的结论，两者不可混用。

### 9.2 弃置判据：幅度不足（futility），不是「p 还大于 0.05」

必须理解当前的约束是什么：

- 费用 114.32，|gross| 20.22，**比值 5.7×**。
- gross/trade = −0.0717，fee/trade = +0.4054。要让 net 转正，gross 必须从 −0.07/trade 摆动到 +0.48/trade，总量约 **+135**。
- 也就是说：**晋级不是被证据量卡住，是被效应幅度卡住（not evidence-limited, magnitude-limited）**。再收十倍的数据，也不会把一个 −0.07 的 gross 变成 +0.48。

据此，弃置判据必须锚在幅度上，而不是「p 值还没变小」：

> **KILL 条件（满足即触发弃置决定）**：在已达成的证据预算内，gross 收益的 day-clustered **单侧 95% CI 上界**低于同口径的往返成本地板（约 10 bps / 约 0.41 per trade），且该判断在**样本已具备检出所需幅度的功效**时作出。

换句话说：我们不是「还没看见 edge」，而是**已经有足够功效去看见那个必须存在的 edge，并且看不见它**。

### 9.3 最小可检出效应与停止规则（MDE / stopping rule）

功效口径（与第 3 节第 3 条同一套参数）：日离散度约 20 bps，单侧 α = 0.05，power = 80%，
`MDE ≈ (z_{0.95} + z_{0.80}) · σ_day / sqrt(D) = 2.486 · 20 / sqrt(D)` bps。

| 独立交易日 D | MDE（bps） |
|---|---|
| 24 | 10.15 |
| 31（当前） | 8.93 |
| 60（晋级门要求） | 6.42 |
| 100 | 4.97 |

需要被检出的效应量：net 均值从 −10.57 bps 抬到 > 0，即约 **+10.6 bps**。

**在 D = 31 时 MDE ≈ 8.9 bps < 所需的 10.6 bps** —— 当前样本**已经**具备检出「足以转正的 edge」的功效。而实测 gross 的 day-clustered 95% CI 为 [−5.66, +4.53]，**上界 4.53 bps 低于约 10 bps 的往返成本地板**。

> **2026-09-04 更正：上面这段手算结论是错的，机器计算推翻了它。**
>
> §9.3 的规则被实现为 `assess_futility` 后，第一次对真实 cohort 执行就返回
> `INSUFFICIENT_DATA` 而**不是** `FUTILE`：
>
> | 量 | 手算（本节原文） | 机器计算（真实 cohort） |
> |---|---|---|
> | D | 31 | **28** |
> | MDE | 8.93 bps | **9.40 bps** |
> | 所需效应量 | 10.57 bps | **8.25 bps** |
> | gross 上界 | 4.53 bps | **6.32 bps** |
> | 判定 | KILL 成立 | **INSUFFICIENT_DATA** |
>
> 两端同时朝不利方向移动：cohort 经 barrier 版本与 provenance 过滤后 D 是 28 而非 31，
> 故 MDE 上升；同时 gross 均值略有改善，故所需效应量下降。规则 2 要求
> `MDE ≤ 所需效应量`，而 9.40 > 8.25，**不成立**，于是规则 3 触发。
>
> 这正是三分支规则存在的理由：上界 6.32 < 地板 10.00，**看上去**可以弃置，但样本并不
> 具备检出「必须存在的那个效应」的功效。在此宣告徒劳，就等于犯下本节开宗明义禁止的
> 「p 还没显著就放弃」。**因此当前状态不是徒劳，而是证据不足——不得据此弃置。**
>
> 教训记入 doctrine：**弃置判据必须由代码计算，不得手算断言。** 手算漏掉了 cohort 过滤
> 对 D 的影响，而 D 同时出现在 MDE 的分母上，是最容易算错、也最容易朝「想要的结论」
> 偏移的一项。本节保留原始错误文字与本更正，作为该教训的证据。

结论：9.2 的 KILL 条件**当前并不成立**（见上方更正框）。可以确认的是 gross 上界低于成本地板，但在 D = 28 时样本尚不具备检出所需效应量的功效，故判定为 `INSUFFICIENT_DATA`，继续收集证据。

停止规则（此后不再逐次重议）：
1. gross 单侧 95% CI 上界 ≥ 成本地板 → 证据仍有活路，继续收集，按第 3 节判定。
2. 上界 < 成本地板 **且** 当前 D 的 MDE ≤ 所需效应量 → **判定徒劳，触发 9.5 的弃置流程**。
3. 上界 < 成本地板 **但** MDE > 所需效应量 → 仍是 `INSUFFICIENT_DATA`，**不得**据此弃置（这正是本规则与「p 还没显著就放弃」的区别）。

以上三条由 `futility.py` 的 `assess_futility` 机械计算，固定采用
`PREREGISTERED_COST_FLOOR_BPS = 10.0` 与
`PREREGISTERED_SIGMA_DAY_BPS = 20.0`。gross 上界固定按 `mean + 2.0·SE`
计算，比名义单侧 95% 更保守；计算还必须先通过第 3 节 verdict 的证据下限，并以
`/api/strategy-shadow/signal-edge` 的 `futility` 字段只读披露。实测 σ 与实测成本仅供人工
交叉核对，不进入判定。

### 9.4 regime 条款：「等一个合适的市场状态」在本数据上没有依据（写入 doctrine）

被弃置的假设常见的最后一条退路是「策略没错，只是 regime 不对，等波动率回来就好」。本数据**直接否证**了这条退路，故记为 doctrine：

按已实现波动率中位数对半切分样本，gross/trade：

| 子样本 | gross/trade |
|---|---|
| 低波动率半 | −0.0306 |
| 高波动率半 | −0.0330 |

**两半都为负，且在压力端（高波动）边际更差。**

这与短期反转类收益的既有理论预测方向相反。Nagel (2012), *Evaporating Liquidity*, **Review of Financial Studies** 25(7): 2005–2039 指出，短期反转收益本质是**流动性提供的补偿**，因而应当**集中出现在市场压力时段**（价差扩大、做市能力收缩时收益最高）。本数据呈现的是相反形态：压力端并没有出现应有的补偿，反而更差。这说明该信号捕捉到的不是流动性提供溢价。

同时参照 Lesmond, Schill & Zhou (2004), *The Illusory Nature of Momentum Profits*, **Journal of Financial Economics** 71(2): 349–380：账面上的毛收益若无法在真实交易成本下存活，就不是可实现的收益。本假设的 5.7× 费用/毛利比正是该论文所描述的形态。

**因此：「等合适的 regime」在本假设上不是一个可用理由，不得作为拒绝弃置的论据。** 若未来要重新主张 regime 依赖，必须按第 6 节以新假设**重新预注册**，并说明为何 Nagel 的预测方向在此不适用。

### 9.5 弃置之后会发生什么（关键：收集不停）

弃置**不等于**关停数据收集。两者必须分开：

1. **收集继续，一行不动。** 交易日计数是唯一无法事后重建的资产：错过的日子永远补不回来，而重新开一个前向窗口要从零开始。v5 继续按原参数运行、继续落库。
2. **重分类为负对照。** 被弃置的假设与第 5 节的冻结 v5 合流，共同承担负对照职责：任何未来的流水线若把它们中的任何一个认证为「有 edge」，**坏的是流水线**。这使弃置产生正向价值，而不仅是止损。
3. **停止消耗工程于其统计量。** 不再为该假设做参数探索、出场重调、门槛微调或专项分析。既有只读端点照常提供数据；新增的分析工作需要按第 6 节注册**新**假设才能获得预算。
4. **写下来。** 弃置必须以书面决定记录（日期、触发的判据、当时的 D / CI / MDE 三个数），与预注册同等要求。口头结论不算。
5. **不得反悔式复活。** 已弃置的假设不能靠「再看看新数据」悄悄回到晋级流程；要复活，走第 6 节的新预注册，并分配新的 `algorithm_version` / `config_version`，证据时钟归零。

### 9.6 与其它条款的关系

- 本节**不修改**第 3 节的四条晋级门，也**不修改** `signal_edge` 的任何统计口径、阈值或判定标签。弃置是一个**治理决定**，其输入是既有统计量的读数。
- 本节引入两个预注册代码常量与一个只读计算字段，但不引入运行期开关，也不是自动化的 kill switch。自动晋级永久禁止（第 7 节），自动弃置同样不存在；弃置仍必须完成第 9.5.4 条的书面决定。
- `INSUFFICIENT_DATA` 的地位不变（第 4 节）：9.3 的第 3 条明确禁止把证据不足读成弃置理由。
