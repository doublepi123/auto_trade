# 开盘动量冻结规则历史迁移验证合同（OPENING_MOMENTUM_HISTORICAL_REPLAY）

> 版本：v1（2026-09-27 冻结，先于任何结果 commit）。本文件与 `backend/tests/test_opening_momentum_historical_replay_preregistration.py` 配套。
> `analysis_id = opening-momentum-top10-pit-historical-v1`。实施 CLI：`backend/app/cli/opening_momentum_historical_replay.py`（子命令 `plan | fetch | seal | evaluate`）。
>
> **注册状态：只读历史研究。本合同不授权任何订单，不授权任何 shadow→live 晋级，也不修改前向合同（`OPENING_MOMENTUM_PREREGISTRATION.md`）的任何内容。**

## 0. 这是一份什么文件

这是对**已冻结前向规则** `INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER`（config_version `44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9`，下文缩写 `44e3c377…def9`）的一次**注册式历史迁移验证（retrospective validation）**的治理合同。它把窗口、universe、统计口径、完整性门槛、判定词汇与无偷看协议在**任何结果产生之前**冻结，使「先看历史再决定要不要看历史」在机制上不可能。

它回答的唯一问题是：**这条规则迁移到一个更早、从未参与其选择的市场区间时，是否与「成本后仍有正期望」相容？** 它不回答「前向认证是否通过」——那是前向合同第 5 节专属的问题。

## 1. 定位与命名

- 这是**冻结信号规则的历史迁移验证**，不是前向 cohort 的提前认证（early certification）。
- 判定词汇与本验证绑定：`CORROBORATES / DOES_NOT_CORROBORATE / INCONCLUSIVE`。这三个词**永不**写为 `PASS / NOT_CERTIFIED / INSUFFICIENT_DATA`——它们属于前向合同。词汇混写即合同违约。
- 本验证的结果**永远不能**给前向 cohort 增加任何一笔交易；前向样本只来自 E=2026-09-28 起的前向 shadow。

## 2. 独立性与已知重叠（诚实清单）

1. **文献重叠。** Zarattini & Barbon（2023）与 Aziz（2023）的 ORB / Stocks-in-Play 研究样本覆盖约 2016–2023 年（含 2023 年 9–12 月）。因此本窗口的尾部（2023-09..12）**不是完全未研究区间**。已知重叠登记在此，不得事后剪裁窗口来「恢复」纯净度。
2. **上游 universe 研究的长历史。** `universe_selection_service.py`（约 L1748–1798）的目录评估确实拉取过长历史日线；但本验证的变体只用其中的**有限正 ADV** 事实（`avg_dollar_volume` 为有限正值），**不用** selection 分数、rank 或 `selected`。上游研究看过这些数据不等于本规则见过：本规则的结构（TOP10 活跃度 + 5 分钟 ORB + 区间低点止损）从未在该窗口上被评分。
3. **无法移除的一般市场知识。** 「2023–2026 美股大盘上行」「AI/半导体强势」属于公共知识，注册者无法遗忘。处置办法不是假装中立，而是**登记 + 禁令**：禁止依据该知识调整窗口边界、symbol 集合或分段；描述性切片永不参与判定（第 7 节）。

## 3. 结果可以改变什么、不可以改变什么

**可以：**

- 研究优先级（是否继续投入这条线）；
- 是否提交一份**书面弃置决定**（对应前向合同的 futility 流程之外的历史佐证）；
- 是否值得为同类规则继续付基础设施成本。

**不可以：**

- 改动前向 E（2026-09-28）、125 笔 / 252 交易日 / 26 周这些数字、30 bps 成本、规则本身或任何 gate；
- 把本窗口的交易并入前向 cohort 或前向统计；
- 授权任何订单、任何实盘切换、任何 shadow→live 晋级；
- 以本结果触发前向合同的 futility 停止（前向合同 ~L104–119 的 futility 只由第 60/90 笔的 `U < +30 bps` 触发）。**负的历史结果不是前向 futility 触发器。** 若要中止前向测试，需要一份书面「因外部证据的行政终止」决定：保留已收集样本与其原始统计状态，不得回溯改写。
- 若要改前向规则，唯一路径是前向合同第 11 节（新版本、新 E、同 commit 更新哈希）。

## 4. 保真度对照表（逐项偏差方向）

| # | 项目 | 保真度处理 | 偏差方向 |
|---|---|---|---|
| 1 | 幸存者偏差（今日目录 vs PIT） | universe 用 `membership_history.is_active` 的半开 `[start, end)` 区间重建 PIT 成员，映射 `INDEX_CANDIDATE_CATALOG ∪ HISTORICAL_INDEX_CANDIDATE_CATALOG` | 消除大部分；残留见 #5 |
| 2 | PIT 迁移改变冻结 universe | 前向 universe 来自「当日开盘前最近一次 COMPLETE selection run 的全部有限正 ADV 候选」；历史上不存在 selection run，故由日线重建 ADV。**universe 定义变了，因此本验证单独分配 `analysis_id`**，其结果按定义不可比前向 cohort 逐日对账 | 双向（重建 ADV 与历史 run 的差异不可观测） |
| 3 | selection run 历史上不存在 | ADV 由截至 D−1 的**已完成日线**重建（20 根 ADV 窗、≥21 根有效日线、`turnover` 优先缺失回退 `close × volume`，全部走冻结的 `selector._dollar_volume`）；**不建模** refresh 失败 | 双向 |
| 4 | 复权口径 | 日线用 `ForwardAdjust`，分钟用 `NoAdjust`（与前向一致）。风险：① `close × volume` 回退时的 `adjusted close × unadjusted volume` 伪成交额；② ForwardAdjust 历史里包含**未来**公司行动（拆股/分红在窗口结束日之后发生也会改写窗口内价格） | 回退情形高估/低估不定；未来公司行动使分钟入场价与日线 ADV 的价格基准错位，方向不定 |
| 5 | 供应商修订与已摘牌数据缺口 | 已摘牌名（ATVI、SGEN、SPLK、WBA 等）供应商可能回 301600 invalid symbol；处置见第 5 节「缺数据名」 | 缺口使有效 universe 缩小；绝不从分母静默剔除 |
| 6 | 09:36 分钟开盘价入场 | 忽略处理与下单延迟（处理 + 撮合至少再 1 根 bar） | **乐观**（成本被低估）。已注册此偏差，**不**把入场移到 09:37（那会变成另一条规则） |
| 7 | 出场路径完整性 | 先证明分钟路径完整（冻结 `_minute_path_complete`）再调冻结 `_exit_outcome`；不完整 → 该交易 UNRESOLVED，绝不猜测 | **双向**：实盘 live 路径存在「回填后不复检」缺陷（前向合同 §6.3），本验证**不**复现该缺陷，故相对实盘可能少算 FIXED_HOLD 多算 STOP，反之亦然 |
| 8 | DATA_INCOMPLETE 跳过频率 | 历史供应商缺 bar 的模式与实时 WS 不同，coverage 0.95 门槛触发的会话比例不可比 | 双向 |

## 5. 冻结分析计划

- **窗口**：2023-09-01..2026-04-30（含端点），前置 **≥21 个已完成日线会话**的 warm-up（约自 2023-08-02 起），warm-up 会话**不计分**。
- **每会话 D 的 universe**：D 日 NASDAQ_100 ∪ DJIA 的 PIT 成员（`membership_history.is_active`，半开 `[start, end)`），经 `INDEX_CANDIDATE_CATALOG ∪ HISTORICAL_INDEX_CANDIDATE_CATALOG` 映射。
  - **公司级去重**：保留 GOOGL、剔除 GOOG（沿 `catalog.py` ~L43-47 的既定做法）；
  - **有限正 ADV**：用冻结 selector 助手重建（D 前最近 **20 根已完成日线**的美元成交额均值，`turnover` 优先，≥21 根有效日线）；无有效 ADV 的名**不入 universe**；
  - **不用** `selected` 筛选、行业上限、$5 亿门槛（这些属于 selection run，历史上不存在）。
- **供应商缺数据（如 301600 invalid symbol 的已收购名 ATVI/SGEN/SPLK/WBA）**：
  - 绝不替换为收购方数据；
  - 绝不从分母静默剔除缺数据名；
  - 该名按「PIT 成员但无数据」处理，与实盘 `minimum_data_coverage=0.95` 逻辑一致（拉低当日 coverage，可能触发 DATA_INCOMPLETE）；
  - 在结果中逐名报告。
- **活跃度、TOP10、突破、09:36 入场、止损、60 分钟持有、30 bps 成本**：全部调用冻结实现（domain `evaluate_stocks_in_play_opening_range_breakout` + `OpeningMomentumConfig`、服务静态助手 `_opening_range_stop_loss_pct` / `_exit_outcome` / `_coerce_candles` / `_signal_turnover`、`shadow_round_trip_return_bps`）。
- **出场路径**：先证完整（`_minute_path_complete`），后调 `_exit_outcome`；不完整即 UNRESOLVED。
- **样本**：窗口内全部交易。无 best-125、无 discovery/holdout 切分。
- **估计量与统计量**：30 bps 成本下交易加权平均 net bps；50 bps 压力列 = 30 列再减 20 bps。周聚类（ISO 周），沿用前向合同公式：`S_w = Σ_week (r_i − mean)`，`SE = sqrt(W/(W−1) · Σ S_w² / n²)`，`L = mean − t(0.95, W−1) · SE`（单侧 95%，df=W−1）。最低样本 **n ≥ 125 且 W ≥ 26**。
- **完整性门（可度量，全部通过才允许非 INCONCLUSIVE 判定）**：
  - (a) ≥95% 的预期会话具备完整可审计的决策输入（universe、分钟 bar、ADV 三者齐备；缺数据的永久失败名计为可审计缺口，不阻断该会话的可审计性——与实盘 coverage 语义一致）；阈值理由：与规则自身的 `minimum_data_coverage=0.95` 同源，规则既然以此 coverage 交易，验证也以此 coverage 审计；
  - (b) 无系统性成员遗漏：PIT 成员-日中无供应商数据的占比 **≤2%**，且**仍上市名**（window_end 仍为活跃成员）的无数据成员-日 **= 0**；阈值理由：已摘牌名的 301600 是可预期的历史缺口，2% 容纳其总量；仍上市名缺数据说明采集缺陷而非历史事实，必须为零；
  - (c) 无未决（unresolved）的已选交易出场路径。
- **判定**：设 L30/U30 为 30 bps 列的单侧界，L50 = L30 − 20、U50 = U30 − 20：
  - 门全过且 **L50 > 0** → `CORROBORATES`；
  - 门全过且 **U50 < 0** → `DOES_NOT_CORROBORATE`（同时报告 U30 < 0 与否）；
  - 其余（含样本不足 n<125 或 W<26、门未过、界跨零）→ `INCONCLUSIVE`。
  - 明示：**这不是前向 PASS**；名义 p 不是严格独立错误率（文献重叠 + 规则选择本身消耗了统计资格）。
- **描述性切片（永不判定、永不作为丢弃区间的理由）**：日历半年（首尾标注 partial）、月度交易数、等名义月度净 bps 和、最差月、回撤、收益集中度。明示：**正的每笔均值不是稳定月收益**。
- **交易日历**：一次性从 LongPort `QuoteContext.trading_days`（US 市场）拉取，窗口超限时分块。该列表**封入输入 manifest**。与本地 holiday calendar（2024-01-01 起）重叠区间交叉核对，mismatch 逐日报告；本地日历无 2023 数据，2023 会话标注为未核对。

## 6. 无偷看协议（顺序不可换）

1. 计划、代码与 pin **在任何结果之前** commit 并 push；
2. `fetch` 阶段只输出日期、覆盖率、错误、配额与 ETA，**永不输出 PnL**（该阶段根本不计算收益）；
3. `seal` 封输入 manifest 哈希（文件清单 + sha256、交易日、universe）；
4. `evaluate` 单次跑完，不流式输出中间结果；
5. commit 一份小完成回执：plan commit、代码/输入/输出哈希、版本、判定；
6. 缺陷修复保留原失败产物与原因；**结果看过之后任何重跑都不再是第一次 OOS**——重跑必须带 `--rerun-reason`，原输出保留为 superseded 文件。

## 7. 配额与连接安全

- **只用 QuoteContext，永不 TradeContext**。不使用 `BrokerGateway`（它同时构建 TradeContext，`broker.py` ~L1290–1305）；
- 单 worker，默认 **0.5 req/s**；
- 默认暂停窗：**UTC 周一至周五 13:00–22:00 不抓取**（覆盖 US RTH 与每日研究 cron）；
- **301607（配额）或 301604/任何权限错误 → 立即全局停止**，落 `status.json` 的 `global_stop` 标记，无重试；后续 fetch/seal 见标记即拒绝。瞬时错误有界重试（默认 3 次）；**301600/invalid symbol 是按 symbol 持久化的永久失败，不无限重试**；
- fetch symbol 清单由 `plan` 预先声明，fetch 不越界；
- 月界重查配额记账（monthly checkpoint 落盘）；
- 缓存目录 `backend/data/research/opening_momentum_historical_replay_v1/`（git-ignored）：原子写（tmp + `os.replace`）、逐文件 sha256、`status.json` 状态清单、可断点续传、永久失败持久化；
- **无任何 DB 访问或写入**；不使用 `import_opening_activity`（它写 DB）。

## 8. 判定的授权范围（重申）

`CORROBORATES` 授权的唯一动作是：把「值得继续前向确认」写进研究优先级记录。`DOES_NOT_CORROBORATE` 授权的唯一动作是：考虑提交书面弃置决定的材料之一。两者都不改动前向合同任何数字，都不授权订单，都不构成稳定月收益的证据。

## 9. Code manifest（CLI 适配层 pin 清单）

以下清单与 `backend/tests/test_opening_momentum_historical_replay_preregistration.py` 的 `_MANIFEST` 逐项一致（doc-agreement 断言双向同步）。哈希为 `ast.dump`（无属性）SHA-256，冻结于 2026-09-27。适配层只提供实盘 `_observe_variants` 为**这一个**变体提供的胶水：信号 bar、活跃度比值、区间高低点；**永不**调用 `tick`、`_observe_variants`、`_close_if_due`。

```
app.cli.opening_momentum_historical_replay.build_plan_payload = b01f0c75c03437f5dd05b3df664aacce9f8a4dda242c3f162321f10807e2a684
app.cli.opening_momentum_historical_replay.build_session_observation = 7de3a869d20df85a9e014eaa7dd3347ea035d21443102e6f398a7e0716e06cf2
app.cli.opening_momentum_historical_replay.classify_provider_error = 3e6fb15f63704bb236f03127bcdcc74bbdbffd66d55d7c568c9aacdb08d5b876
app.cli.opening_momentum_historical_replay.company_dedupe = f4937543bcf9e73f4ca0261c33788080ce0365e6d84824b86830554ab858b3ed
app.cli.opening_momentum_historical_replay.compute_descriptives = a6bc2d950b0a4d07fa05e55a8e4302db402a6e0b1a7d49a73bebabbff228ce7f
app.cli.opening_momentum_historical_replay.cross_check_trading_days = 373b2035be134ae15db1dce15857adb80ad887b6846fda18fc1bc318f8602199
app.cli.opening_momentum_historical_replay.decide_verdict = dd8809ae61a4ff3703dbf972f5b5f5a98e0bc5df4269efbd46a72e3a6a7e84fa
app.cli.opening_momentum_historical_replay.evaluate_session_decision = 3603499a2414be4eae5588cd389d1b2aac66d1edb75d98c961d97c56c647c86e
app.cli.opening_momentum_historical_replay.frozen_config_version = 5faafab72903ab4c5efddcc18f06fd740c3dbc0cdd27162de7d03348ab73e8f7
app.cli.opening_momentum_historical_replay.frozen_decision_config = 052adc8214416f6b37093f7f386b9300a07e5904d1a2c2e7228260b9a5cec1af
app.cli.opening_momentum_historical_replay.is_fetch_window_open = e4d1a12f4eb0ef90e6559b8ab6ce6440363fed2d4f9b7c4f68335e958eae0230
app.cli.opening_momentum_historical_replay.one_sided_t95 = d2b55ab0623aaf8d4b5e08f78c40882220ec4d26d6d0cbbb3207a062b2b818ce
app.cli.opening_momentum_historical_replay.pit_universe_for_session = 5f0fbe0a81638a39e8b5ef5413b0869a0da16f4fe7088619ec69d69dd1707733
app.cli.opening_momentum_historical_replay.rebuild_session_adv = 884e4e37261722b12a26f1d1ba5186def491e0aa05720b53e2df6d7f2d283c29
app.cli.opening_momentum_historical_replay.run_evaluate = 1f6fe70189d79c046870887e6b0b7fec6cd42eca86f49cb29f572b0b0f9c5e5c
app.cli.opening_momentum_historical_replay.run_fetch = d4b0f1e005a55604b34cfbe7b13f157ee06d29954c2d0c2c9187f6555cd94bf5
app.cli.opening_momentum_historical_replay.run_seal = b460e6cce851da157ae0272de3510ef08132cf40925f58af18db3e532326c282
app.cli.opening_momentum_historical_replay.settle_session_exit = 61b9f07a802016b756e653dca28dedd4c5a0385a27c3ce526b47e0c2177c1d41
app.cli.opening_momentum_historical_replay.week_clustered_statistic = 88bc68155fa9b44429f0e02fc5b4fe96a349725cdfbfe8f97391e9c30239a683
app.cli.opening_momentum_historical_replay:ADV_LOOKBACK_BARS = f3ce26c20bd6b921e7619a32503d5b3781316ca759fd1fb94df25c9dae9a98d2
app.cli.opening_momentum_historical_replay:ANALYSIS_ID = 716a57ada1a45190521f61a0b7c3d9edb911344e3d82d3f3b4d2c3c9d1f23c7f
app.cli.opening_momentum_historical_replay:ENTRY_OFFSET = ae44f7a0814040a53d6d354ddd42d5c292605a3586c09374a4fbcddccb1b0749
app.cli.opening_momentum_historical_replay:EXIT_OFFSET = 48cc21900d6eff480e321fa95788dd9bb8cb9de485538531df57cc2fe7ac7c68
app.cli.opening_momentum_historical_replay:FROZEN_CONFIG_VERSION = 5da1a89f78d2a7d610c8f649687910df3afc4043a9e83e9e090da381a8405d04
app.cli.opening_momentum_historical_replay:GATE_MEMBER_DATA_MISSING_MAX_SHARE = e0868b58698c24f5964b9bf46335229725f44c62a7bd98e0f4e33b8a29afdb14
app.cli.opening_momentum_historical_replay:GATE_SESSION_INPUT_COVERAGE = abcef86b08d6c6d4815e17e4dba4b8308d3c18a879994f576c388b08358555a3
app.cli.opening_momentum_historical_replay:GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS = 06617bb67e43e8172050f8e8f955d0529fbcfb4a25f8b2fec28fa7ed6a1b2cd1
app.cli.opening_momentum_historical_replay:HOLDING_MINUTES = 026ecacc9efc0d43f4b78d8541aa966239059cdc3434f69e1406ca1ed9705b72
app.cli.opening_momentum_historical_replay:MIN_COMPLETED_BARS = 73c3e55d4778d30c3fb80ae849fc82ca5aef47f9d49417a1e349b65736335f61
app.cli.opening_momentum_historical_replay:MIN_TRADES = 29bda80ca6fa505d5c22b30167e8a3ac692bd8483f5f2de86eb7311f8b536d57
app.cli.opening_momentum_historical_replay:MIN_WEEKS = 3ad9503950ca350068c0fc4023bd03cbfcc5b3558abd689929d75aeb44a0dfb0
app.cli.opening_momentum_historical_replay:ONE_SIDED_T95_BY_DF = 0d6e9d6aa4a9679d8042405cc08c21a4ad0d863460958a3292b44b9dde91d079
app.cli.opening_momentum_historical_replay:OPENING_ACTIVITY_TOP_N = db9872c349cc3ab2ec899dc49d6e52124e35817119a84ac6ff5fe72cdfea9fe1
app.cli.opening_momentum_historical_replay:STOP_LOSS_CAP_PCT = 14dc2e3f7fd0c11829bf0f554940bdde989435a778a869645ae25e2ca819d877
app.cli.opening_momentum_historical_replay:STRESS_COST_BPS = 7cbc3dc478820865c754d63d85b3bdf55cabf21b4a7d8a6349e32ff9f793e733
app.cli.opening_momentum_historical_replay:WARMUP_SESSIONS = 1b37c1c530d30746a4936c6314adeaed2f0ef1f757378020adab54189c05a18a
app.cli.opening_momentum_historical_replay:WINDOW_END = b763b1f0f35d4a280dd2decc877bb949c28969767b038f6bf49969d6a4d052b7
app.cli.opening_momentum_historical_replay:WINDOW_START = bf7271237f87d8bf4d9925ed9006efc41d0d9c3a4a21c8147a2c5f568cb15a66
combined = b2e9eef58025ef4d4259bdb9d2e26a1ffe1e49c76418ecf4c56ff90e09a408aa
```

**禁止为消红而改哈希。** 任何 pin 失配都要求：先在本文档记录书面决定（动机、处置、对已完成结果的影响），再在**同一提交**里更新哈希与本文档；在结果已存在之后改哈希使既有结果作废，且新的运行**不得**称为第一次 OOS。

## 10. 变更流程（唯一合法路径）

1. 在本文档记录书面决定（动机、新 `analysis_id` 后缀、对既有产物的处置）；
2. 分配新的 `analysis_id` 并同步 CLI 常量与 pin；
3. **同一提交**更新测试 pin 常量、本哈希清单与决定记录；
4. 旧输入与旧输出按第 6.6 条保留，不删除、不改写。

绝不允许：先改代码后补文档、只改哈希不写决定、看过结果后调整窗口/universe/统计口径再跑、或把重跑称为第一次 OOS。
