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

1. **净值显著为正**：net 收益的 day-clustered 单侧 95% CI 下界 **> 0**（gross 一并报告，对照固定的约 10 bps 往返成本模型）。
2. **版本专属 first-passage**：该版本实测 target-first 比例的下界，高于**该版本自己**的 driftless 基准 `stop/(stop+target)`。换屏障就换基准，不许跨版本借用。
3. **样本量**：**≥ 60 个独立交易日** 且 **约 180 个独立已结算 bracket 结果**。功效说明：在观测到的约 20 bps 日离散度下，以 80% power 检出 +5 bps 的 net edge，大约需要 **100 个独立交易日**。样本不够就是不够，结论只能是 `INSUFFICIENT_DATA`。
4. **Deflated Sharpe Ratio**（Bailey & López de Prado 2014）：按已注册的试验次数校正后，在 95% 水平上与运气可区分（`distinguishable_from_luck`）。注意：仓库**已实现**该计算，`POST /api/backtest/sweep` 返回的 `multiple_testing` 块包含 deflated Sharpe 与 `distinguishable_from_luck`。本条要求是把既有实现**应用为门**，不是重新造一个。

四条是 AND 关系。任何一条不过，不晋级。

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
