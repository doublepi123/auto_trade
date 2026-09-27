# 开盘动量冻结规则历史迁移验证合同（OPENING_MOMENTUM_HISTORICAL_REPLAY）

> 版本：v1（2026-09-27 冻结，先于任何结果 commit）。本文件与 `backend/tests/test_opening_momentum_historical_replay_preregistration.py` 配套。
> `analysis_id = opening-momentum-top10-pit-historical-v3`（v1/v2 见 §8.5/§8.6：已注册、未执行；v2 原始输入经兼容性检查后复用）。实施 CLI：`backend/app/cli/opening_momentum_historical_replay.py`（子命令 `plan | fetch | seal | evaluate`）。
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
- **每会话 D 的 universe 与三分维度（变更决定 2 后的现行规则）**：D 日 NASDAQ_100 ∪ DJIA 的 PIT 成员（`membership_history.is_active`，半开 `[start, end)`），经 `INDEX_CANDIDATE_CATALOG ∪ HISTORICAL_INDEX_CANDIDATE_CATALOG` 映射。
  - **公司级去重**：保留 GOOGL、剔除 GOOG（沿 `catalog.py` ~L43-47 的既定做法）；
  - **ADV 决定池成员资格**（与实盘 `index_catalog_symbols` 一致），逐 (成员, 会话) 分为四类：`ELIGIBLE(adv)`（在**每个**所需封存会话（含 warm-up 日期表）上都有有效日线、完整 21 根窗口、经冻结 selector 计算出有限正 ADV、最新 bar 恰为前一封存会话）；`KNOWN_INELIGIBLE`（完整有效窗口的 ADV 非有限正）；`UNVERIFIABLE`（不足 21 根且**无独立上市日期证明**——缓存边界不是上市证明，NEW_LISTING 路径已删除、所需封存会话缺日线（含中间缺口；停牌名因此保守 UNVERIFIABLE）、最后 21 根窗口内有无效 bar、None/非有限/负 turnover 输入异常、流动性代理不可用导致的 `DATA_INVALID_SPREAD_PROXY`、新鲜度缺口、或日线抓取 COMPLETE 且非永久却无任何日线数据）；`PERMANENT_GAP`（供应商永久拒绝）。
  - **决策池 = ELIGIBLE 成员**，连同其现有的分钟 bar（经冻结 `_coerce_candles`）传入，**绝不**按分钟数据质量过滤；缺信号 bar 只减少 observations，缺 activity ratio 破坏完整性——两者都与实盘一致；
  - **coverage 分母 = ELIGIBLE ∪ coverage_only**（UNVERIFIABLE 与 PERMANENT_GAP 只进分母，永不建 observation、永不获得虚构 ADV）。**把永久缺口保留在分母里是本合同的保守加项，不是对实盘同一性的声明**；
  - `KNOWN_INELIGIBLE` 成员被排除，与实盘相同；
  - **不用** `selected` 筛选、行业上限、$5 亿门槛（这些属于 selection run，历史上不存在）。
- **供应商缺数据（如 301600 invalid symbol 的已收购名 ATVI/SGEN/SPLK/WBA）**：绝不替换为收购方数据；绝不从分母静默剔除；按 PERMANENT_GAP 处理并逐名报告。
- **活跃度、TOP10、突破、09:36 入场、止损、60 分钟持有、30 bps 成本**：全部调用冻结实现（domain `evaluate_stocks_in_play_opening_range_breakout` + `OpeningMomentumConfig`、服务静态助手 `_opening_range_stop_loss_pct` / `_exit_outcome` / `_coerce_candles` / `_signal_turnover`、`shadow_round_trip_return_bps`）。前五分钟 turnover 采用实盘语义：接受单分钟为零、不容 None、要求有限正**总和**（`_signal_turnover`）。
- **ADV 重建（最终修正后）**：日线日期行**绝不丢弃**；先按日期取 D 前最后 21 根已完成 bar；缺失 volume 使该 bar 无效（窗口随之无效）；**turnover 为 None 是输入异常**（实盘归一化器 `float(getattr(item, "turnover", 0))` 对缺失属性得 0、对显式 None 转换失败并丢弃该 bar），故 None/非有限/负 turnover 同样使 bar 无效——只有合法的 0.0 走冻结 `_dollar_volume` 回退；窗口整体验证后**直接调用冻结 `selector._candidate_metrics`**，spread 输入与实盘服务逐形一致（`liquidity_spread_proxy_bps` 不可用时记 `DATA_INVALID_SPREAD_PROXY` 并置空 metrics——**绝不**虚构报价），任何 DATA_ 缺陷 → UNVERIFIABLE；非 DATA 的流动性排除理由不剥夺有限正 ADV 的池资格（与实盘读存储指标一致）；坏 bar 绝不由更旧的 bar 顶替。
- **出场路径**：先证完整（`_minute_path_complete`），后调 `_exit_outcome`；不完整即 UNRESOLVED。选中交易的止损路径 bar 原始 OHLC 无效者亦 UNRESOLVED（修复过的 low 不是原始证据）。
- **样本**：窗口内全部交易。无 best-125、无 discovery/holdout 切分。
- **估计量与统计量**：30 bps 成本下交易加权平均 net bps；50 bps 压力列 = 30 列再减 20 bps。周聚类（ISO 周），沿用前向合同公式：`S_w = Σ_week (r_i − mean)`，`SE = sqrt(W/(W−1) · Σ S_w² / n²)`，`L = mean − t(0.95, W−1) · SE`（单侧 95%，df=W−1）。最低样本 **n ≥ 125 且 W ≥ 26**。
- **完整性门（可度量，全部通过才允许非 INCONCLUSIVE 判定；决策资格、数据存在与可审计性是三个独立维度，绝不由单一状态决定）**：
  - (a) **可审计会话** ≥95%（研究层面，与(会话内)observation coverage 的 0.95 是两个不同的 95%）：当且仅当凭封存证据可为每个成员确定成员资格与 ADV 资格（**无 UNVERIFIABLE 成员**；`PERMANENT_GAP` 成员**永不**阻断可审计性——它由 (b) 计数），且冻结规则随后复现唯一的 OPEN 或 SKIP 及其 reason，该会话可审计。**确认缺失**的 bar/turnover 是可审计输入——它们合法复现 DATA_INCOMPLETE 或 ENTRY_BAR_MISSING。OPEN 会话还需可证明的出场路径：OPEN 且出场不可证 → 既不可审计也计入 (c)。**选中交易的原始止损路径（入场至出场 bar 的每一分钟）在结算前验证**：任一分钟缺失或原始 OHLC 无效 → 该交易 UNRESOLVED（计入 (c)、不写 CLOSED、不入统计——修复过的 low 不是原始证据）；任何决策成员决策窗内 bar 的原始 OHLC 异常使会话不可审计，成员**绝不**因此被删除或重选。不完整的 fetch 永不到达此处（preflight 拒绝）；缺失前驱是配置错误（preflight 拒绝）。
  - (b) **成员-日缺失仅计两类**：`NO_MINUTE_DATA`（分钟抓取 COMPLETE 但该会话保留窗口 09:30..10:40 ET 内**没有任何**原始分钟 bar——这**不是**「全日无交易」的断言）与 `PERMANENT_PROVIDER_GAP`。缺失单根 bar、缺失 turnover、无效 OHLC、KNOWN_INELIGIBLE 与 UNVERIFIABLE ADV 单独记录、**不计入** (b)。阈值不变：无供应商数据成员-日占比 **≤2%**，**仍上市名**（window_end 仍为活跃成员，取 seal 时封存集合）**= 0**，均以成员-日粒度。
  - (c) 无未决（unresolved）的已选交易出场路径。
- **判定**：设 L30/U30 为 30 bps 列的单侧界，L50 = L30 − 20、U50 = U30 − 20：
  - 门全过且 **L50 > 0** → `CORROBORATES`；
  - 门全过且 **U50 < 0** → `DOES_NOT_CORROBORATE`（同时报告 U30 < 0 与否）；
  - 其余（含样本不足 n<125 或 W<26、门未过、界跨零、退化样本零周方差）→ `INCONCLUSIVE`。
  - 明示：**这不是前向 PASS**；名义 p 不是严格独立错误率（文献重叠 + 规则选择本身消耗了统计资格）。
- **描述性切片（永不判定、永不作为丢弃区间的理由）**：日历半年（首尾按注册窗口标注 partial）、月度交易数（含零交易月）、等名义月度净 bps 和、最差月、回撤、收益集中度（仅正贡献）。明示：**正的每笔均值不是稳定月收益**。
- **交易日历（变更决定 1+2 后的现行来源）**：以基准 ETF 日线交集**盲推**——QQQ.US 与 DIA.US 每个有常规时段日线 bar 的日期取交集（`history_candlesticks_by_offset`，DAY，前向翻页，NoAdjust——只有日期起作用）。仅一侧有 bar 的日期作为 mismatch 报告并**从预期会话中剔除**（原因记录）。与本地 holiday calendar（2024-01-01 起）重叠区间交叉核对：本地标记休市但双 ETF 有 bar、或反之的日期逐日列出，**不作为门槛**。半日（early close）在可知处记录；本地半日表 2024 年起才覆盖，2023 半日按惯例日程记录并标注 unverified（12-22 不是半日，已剔除）；09:36 入场与 60 分钟持有均在 13:00 前结束，**半日不改变规则**。可选的 provider `trading_days`（≤28 天分块、仅最近一年）作为**额外**交叉核对，其失败永不中止 fetch。
- **warm-up 日历（最终修正后的证据义务）**：运行中的 v2 fetch 产出的 `trading_days.json` 自 2023-09-01 起、未持久化基准 bar 且丢弃了 warm-up 日期。`fetch-calendar-warmup` 子命令（仅 QuoteContext、同一 throttle/暂停窗；**存活 fetch 检查与持久化 global_stop 检查先于 QuoteContext 构造**；quota/permission 拒绝按 `run_fetch` 的规则写持久化 global_stop）抓取 QQQ/DIA 2023-08-01..2023-08-31 的日线（NoAdjust），**只保存日期**到 `trading_days_warmup.json`（交集、单边日期、来源、fetched_at、请求数；不存储不打印任何价格；原子写；已有文件无 `--reason` 拒绝覆盖）。seal 绑定**原始** `trading_days.json`（含哈希）与 warm-up 文件。preflight 的证据义务是**具体的**：对注册窗口（首个计分日 2023-09-01），warm-up 必须包含注册前驱 **2023-08-31**，并覆盖首个计分日 21-session ADV 窗口所需的**全部**封存会话 **2023-08-03..2023-08-31**（恰 21 个 NYSE 会话）；日期必须唯一、在注册范围内、且严格早于首个计分日；任一不满足即拒绝（先于任何 attempt STARTED）。**warm-up 日期只用于 ADV/新鲜度与缺口检查，永不成为计分会话：计分日期数组恒为 2023-09-01..2026-04-30。**

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

## 8.5 变更决定 1（2026-09-27，任何结果之前）

按第 10 节流程记录的书面决定：

1. **发生了什么。** v1（`analysis_id = opening-momentum-top10-pit-historical-v1`）**已注册、未执行**。首次真实 `fetch` 在拉取交易日历时立即失败，早于任何行情数据的抓取，未创建缓存目录，不存在任何结果。原始错误：

   ```
   _fetch_trading_days → provider.trading_days(begin, end) with _TRADING_DAYS_CHUNK_DAYS = 400
   longport.OpenApiException: (code=301600) too many query days
   ```

   SDK 对 `QuoteContext.trading_days` 的说明是「区间必须小于一个月，且仅支持最近一年」。因此 v1 注册的交易日来源（provider `trading_days` 覆盖 2023-09..2026-04）**不可行**，属于注册时的请求形状错误，不是数据问题。

2. **无偷看状态。** 因任何结果都未产生（fetch 在交易日一步即失败），本次修正后的运行**仍是第一次 OOS**；此事实在此如实记录，不作为后续任何重跑的先例。

3. **新交易日来源**（盲收益，只看日期不看价格，见第 5 节更新后的表述）：QQQ.US ∩ DIA.US 日线 bar 交集；单边日期剔除并报告；本地日历交叉核对不设门槛；2023 半日 unverified；半日不改规则；可选 provider `trading_days` 仅作额外核对且失败不中止。

4. **错误分类修正。** `code=301600` 不再单独构成 per-symbol 永久失败：同一代码也用于「too many query days」这类请求形状拒绝。现分类为 `REQUEST_SHAPE`（不重试、不落 symbol 永久失败、fetch 干净失败并写 `status.json` 错误条目，非配额/权限不写 global_stop）；只有明确的 invalid/unknown symbol 语义仍是 `PERMANENT_SYMBOL`。

5. **新 analysis_id。** `opening-momentum-top10-pit-historical-v2`。其余一切冻结元素（窗口、universe 规则、门、判定、统计量、安全规则、无 DB 规则）**不变**。

6. **同一提交。** 本决定、更新的 pin 与代码在同一提交内（第 10 节第 3 步）。

## 8.6 执行记录（非规则变更）

- **尝试 1（v1，2026-09-27 约 10:29Z）**：第一个请求 `trading_days`（400 天分块）即被拒绝：`code=301600 too many query days`。未取得任何行情数据，无任何结果。处置见第 8.5 节变更决定 1。
- **尝试 2（v2，2026-09-27 11:36Z，commit `39a06340`）**：基准 ETF 日线拉取在进入 SDK 之前就失败了。适配器向 SDK 请求 `Period.DAY`，而 SDK 的枚举成员名是 `Period.Day` / `Period.Min_1`（`AttributeError: type object 'builtins.Period' has no attribute 'DAY'`；被归为 TRANSIENT，重试 3 次后干净失败；`status.json` 保留该错误条目，未写 global_stop）。**没有任何请求到达 provider，未取得任何行情数据，无任何结果。**
- **修复**：只改动未被 pin 的 SDK 适配层 `_LongportQuoteProvider._period`，新增私有映射 `_SDK_PERIOD_NAMES = {"DAY": "Day", "MIN_1": "Min_1"}`，未映射的周期在发出请求之前即被拒绝；并补充按真实 SDK 枚举名驱动适配器的测试。第 9 节 manifest、全部冻结常量与分析语义都没有改变，因此**不分配新的 analysis_id**。由于任何结果都尚未产生，修复后的运行仍是第一次 OOS。

## 8.7 变更决定 2（2026-09-27，任何结果之前）

结果前独立评审对 `evaluate` 给出 NO-GO，共 6 项 MUST-FIX。按第 10 节流程记录：

1. **状态**：v3 注册时**没有任何结果**；对运行中 v2 fetch 的缓存**未打开任何价格文件**（仅读取 `status.json` 与 `trading_days.json` 以核对格式）。v2 抓取的原始输入在兼容性检查后**复用**（文件布局、status schema、逐文件 sha256 均由现行 `run_fetch` 产出并被 v3 的 seal/evaluate 原样接受；seal 时缺失的元数据——如逐文件校验和——由 **seal 时计算并记录**，不要求 fetch 补写）。窗口、universe 规则、估计量、统计量与判定映射**不变**，仅含下列修正。

2. **修正清单**：
   - **未完成的 fetch**（MUST-FIX 1）：seal 与 evaluate 共用同一 preflight——计划内每个 symbol 的两种数据（daily/minute）必须处于终态（COMPLETE，或有错误证据支撑的 PERMANENT_FAILURE）；缺失状态、PENDING、FAILED_TRANSIENT 一律拒绝；COMPLETE 条目必须有文件在盘；计划全文与哈希随缓存绑定；seal 在有存活 fetch 进程（`/proc` 扫描 argv 匹配本模块 + `fetch`，或 `fetch.lock`）时拒绝。
   - **成员-日计数**（MUST-FIX 2）：Gate (b) 改为逐 (symbol, session) 状态分类（NO_MINUTE_DATA / DECISION_WINDOW_INCOMPLETE / ADV_INPUT_MISSING / ADV_NOT_FINITE_POSITIVE / ADV_INPUT_GAP / PERMANENT_PROVIDER_GAP / INVALID_OHLC），永久缺口只影响 Gate (a) 的可审计判定，**永不**抵扣 Gate (b)；决策输入完整性检查覆盖活跃度比值所需的**前五分钟 turnover**。
   - **决策 universe 的有限正 ADV 过滤**（MUST-FIX 3）：三集合分离——(i) PIT 成员（成员-日审计分母）、(ii) 有限正 ADV 成员（进入决策，与实盘 `index_catalog_symbols` 一致）、(iii) 供应商无数据成员（留在 coverage 分母，永不改善 coverage）；「ADV 已知但不合格」与「无数据」是两个不同结局。
   - **ADV 重建等价**（MUST-FIX 4）：严格取 D 前日线、按日排序并校验日期唯一；取**最后 21 根**并整窗校验后经冻结 `_dollar_volume`（turnover 优先、close×volume 回退）平均最后 20 根；坏 bar **绝不**由更旧的 bar 顶替；缺失 volume/turnover **绝不**折算为 0；新鲜度：D 前最后一根日线必须等于封存交易日表上 D 的前一会话，否则记输入缺口（ADV_INPUT_GAP）。
   - **封闭输入集**（MUST-FIX 5）：evaluate 只读 manifest 列出的文件且逐一即时校验 sha256，未列出文件即便在盘也拒绝；永久缺失显式记录；窗口末成员集在 seal 时封存并供 Gate (b) 使用（不再运行时重算）；plan、membership JSON、两个目录与两份源 manifest 的哈希在 seal 绑定、evaluate 复核；seal 回执不可静默覆盖（再 seal 需理由且保留旧回执）；校验日历日期唯一、universe 覆盖 plan、文件 symbol/period/adjustment 元数据一致。
   - **只运行一次**（MUST-FIX 6）：cache 级原子 attempt 回执；全部预检（输入、代码哈希、输出可写、输出不在输入集内）先于任何计算完成；STARTED 先记录且失败也保留；后续任何尝试需非空 `--rerun-reason` 并记录前次尝试及其输出哈希；**绝不**改名或删除更早的输出，重跑写新版本文件并仅在成功后链接 supersede；换 `--output` 路径不能绕过以上任何一条。

3. **analysis_id → `opening-momentum-top10-pit-historical-v3`**；第 9 节 manifest 同步更新，并把本次依赖的装载器、输入检查与回执助手纳入 pin。

4. **结果前裁决补充修正（2026-09-27 第二次评审，仍在任何结果之前；未看过任何结果、未打开任何价格文件；沿用同一 analysis_id v3）**。核心原则：**决策资格、数据存在与可审计性是三个独立维度**，绝不由单一 `status == "OK"` 决定——每个成员-日记录多条事实（`MemberDayFacts`），分类交给纯函数（`assemble_member_day_audit`）：
   - **决策池与 coverage 分母（裁决 1，改判定）**：修正此前两处错误过滤——ADV 四分类（ELIGIBLE / KNOWN_INELIGIBLE(新上市或完整有效窗口 ADV 非有限正) / UNVERIFIABLE(窗口内封存会话缺日线、最后 21 根含无效 bar、新鲜度缺口、或日线 COMPLETE 而非永久却无数据) / PERMANENT_GAP）；决策池 = ELIGIBLE（连同其现有分钟 bar 经冻结 `_coerce_candles` 传入，**绝不**按分钟质量过滤——缺信号 bar 只减 observations，缺 ratio 破坏完整性，均与实盘一致）；`coverage_only_symbols`（UNVERIFIABLE ∪ PERMANENT_GAP）只进 `required = max(8, ceil(0.95 × (eligible ∪ coverage_only)))` 的分母，永不建 observation、永不获虚构 ADV；KNOWN_INELIGIBLE 被排除（与实盘一致）。**把永久缺口留在分母是本合同的保守加项，不是对实盘同一性的声明。**
   - **首个计分会话 warm-up（裁决 2，改判定）**：v2 fetch 的 `trading_days.json` 自 2023-09-01 起、丢弃 warm-up 日期，D=2023-09-01 全员 ADV_INPUT_GAP。新增 `fetch-calendar-warmup` 子命令（仅 QuoteContext、同 throttle/暂停窗/全局停止；fetch 存活时拒绝；QQQ/DIA 2023-08-01..08-31 日线 NoAdjust；**只存日期**到 `trading_days_warmup.json`，含交集/单边/来源/fetched_at/请求数；原子写；已有文件无 `--reason` 拒绝）。seal 绑定原始 `trading_days.json`（含哈希）+ warm-up 文件；preflight 在 warm-up 缺失或无法证明首个计分会话前驱时**先于任何 attempt STARTED** 拒绝；warm-up 只用于 ADV/新鲜度/缺口检查，**计分日期数组恒为 2023-09-01..2026-04-30**。
   - **Gate (b) 收窄（裁决 3，改判定）**：仅计两类——`NO_MINUTE_DATA`（分钟抓取 COMPLETE 但该会话保留窗口内无任何原始 bar；**非**「全日无交易」断言）与 `PERMANENT_PROVIDER_GAP`；缺单根 bar、缺 turnover、无效 OHLC、KNOWN_INELIGIBLE、UNVERIFIABLE 单独记录且**不计入**。阈值不变：≤2%、窗口末成员 = 0（seal 时封存的成员集），均成员-日粒度。
   - **Gate (a) = 可审计会话（裁决 4，改判定）**：当且仅当凭封存证据可为每个成员确定成员资格与 ADV 资格（无 UNVERIFIABLE），且冻结规则复现唯一 OPEN/SKIP 及 reason。确认缺失的 bar/turnover 是可审计输入（合法复现 DATA_INCOMPLETE / ENTRY_BAR_MISSING）；OPEN 需可证明出场路径（不可证 → 不可审计且计 (c)）；不完整 fetch 到不了这里（preflight 拒）；缺前驱是配置错误（preflight 拒）；触及决策或选中交易止损路径的无效原始 OHLC 列为完整性问题（选中交易止损路径无效 → UNRESOLVED——修复过的 low 不是原始证据）。可审计计数**在决策与出场分类之后**累计（此前在之前累计是错的）。两个 95% 保持区分：会话内 observation coverage 与研究层面可审计会话占比。
   - **日线适配器不再丢行（裁决 A）**：日期行**绝不**丢弃；先按日期取 D 前最后 21 根；缺失 volume 使 bar（与窗口）无效；缺失 turnover 原样传给冻结 `_dollar_volume`（回退生效）；窗口验证后**直接调用冻结 `selector._candidate_metrics`**——等价性由构造保证。
   - **分钟 turnover（裁决 B）**：改用冻结语义——接受单分钟为零、不容 None、要求有限正总和（`_signal_turnover`）；此前「每分钟都必须 > 0」比实盘严。
   - **执行阻断项（裁决 C/D）**：注册的 v2→v3 计划兼容导入（原计划字节与哈希不改写、analysis_id 不重写；除 analysis_id/cli_version 外逐字段等于 v3 `build_plan_payload` 输出；两者都记录）；`--output` 改为**必填且须在缓存外**；evaluate 在输出旁写小型回执 JSON（plan/seal manifest/git HEAD/源哈希/输出 sha256/analysis_id/verdict/门布尔/n/W，无逐笔数据）；evaluate 校验 seal 回执的 manifest 哈希、重算并比对 plan digest；**源出处**——evaluate 在 `git status --porcelain -- backend/app backend/tests` 非空时拒绝，记录 `git rev-parse HEAD` 与 CLI、`opening_momentum_shadow_service.py`、`opening_momentum.py`/`_universe.py`/`_policy.py`、`selector.py`/`catalog.py`/`membership_history.py`、`index_membership_history.json` 的 sha256（取代 markdown-only「source manifest hash」）；attempt 声明改为 `attempts/NNNN.claim` 的 `O_CREAT|O_EXCL` 原子独占创建（两个并发 evaluate 不可能同时进行），NNNN > 1 需 `--rerun-reason`。
   - **阈值无一放宽**：≤2%、=0、两个 95%、n≥125、W≥26、stress 20 全部保持。

5. **最终结果前检查（2026-09-28，仍在任何结果之前；未看过任何结果、未打开任何价格文件；沿用同一 analysis_id v3）**，5 项改判 MUST-FIX + 执行安全：
   - **Gate (a) 误把 PERMANENT_GAP 当 UNVERIFIABLE**（最终 1）：`MemberDayAudit` 携带 `adv_kind`；可审计性仅由 **UNVERIFIABLE** 阻断，`PERMANENT_GAP` 永不阻断（由 (b) 计数）。E2E 证明：同一确定性 SKIP 会话，含 PERMANENT_GAP 成员时可审计、含 UNVERIFIABLE 成员时不可审计，且 (b) 计入永久缺口。
   - **无效原始止损路径从未变 UNRESOLVED**（最终 2）：`stop_path_ohlc_invalid` 事实未入审计、交易先 append 后检查。修正——选中成员的**原始止损路径**（入场至出场 bar 的每一分钟）在结算前验证（`raw_stop_path_is_valid`）；无效 → gate (c) +1、不写 CLOSED、不入统计；决策成员决策窗内原始 OHLC 异常 → 会话不可审计，成员**绝不**删除或重选。E2E：选中股 offset 6 与 offset 30 的坏 high 各自 unresolved=1 且无 CLOSED。
   - **ADV 适配器仍非实盘等价**（最终 3）：3a——turnover 为 None 是输入异常（bar 无效、窗口无效、成员 UNVERIFIABLE；日期行绝不丢弃；只有合法 0.0 走回退），删除「等价于 BrokerCandle 默认」的错误表述；3b——删除「中性报价重试」：spread 代理不可用时按实盘形状记 `DATA_INVALID_SPREAD_PROXY` 并置空 metrics，绝不虚构 ADV。E2E：最后 20 根中一根零美元成交额 bar、其余为正 → 代理不可用 → UNVERIFIABLE（非 ELIGIBLE）。
   - **NEW_LISTING 是未证推断 + 中间缺口未查**（最终 4）：NEW_LISTING 路径**整体删除**——「缓存里不足 21 根」不是上市证明（缓存边界即抓取边界），无独立封存上市日期证据，一律 UNVERIFIABLE（coverage-only，入分母）。历史足够时：成员须在**每个**所需封存会话（含 warm-up 日期）上有有效日线；任一缺失（含中间缺口）→ UNVERIFIABLE；停牌名保守 UNVERIFIABLE。2023-08-03..08-31 恰 21 个 NYSE 会话、计划 `daily_start`=2023-08-03，故 D=2023-09-01 在数据完整时恰有 21 根前置 bar——不重新抓取、不丢弃首日。E2E：08-03..08-31 → 首日 ELIGIBLE；缓存自 08-04 起 → UNVERIFIABLE（非 NEW_LISTING）；21 根但中间缺一个所需会话 → UNVERIFIABLE。
   - **warm-up preflight 只证明「存在更早日期」**（最终 5）：改为具体证据义务——注册前驱 2023-08-31 必须在文件中，且覆盖 2023-08-03..08-31 全部 21 个会话；日期唯一、在范围内、严格早于首个计分日；先于任何 attempt STARTED 拒绝。测试：仅 08-01 拒；缺 08-31 拒；缺 08-15 拒；正确文件过。
   - **执行安全**：`fetch-calendar-warmup` 的存活 fetch 与持久化 global_stop 检查移到 QuoteContext 构造**之前**（工厂延迟构造）；quota/permission 拒绝复用 `run_fetch` 的持久化 global_stop 写入。测试：伪造存活进程时 provider 工厂从不被调用。

## 9. Code manifest（CLI 适配层 pin 清单）

以下清单与 `backend/tests/test_opening_momentum_historical_replay_preregistration.py` 的 `_MANIFEST` 逐项一致（doc-agreement 断言双向同步）。哈希为 `ast.dump`（无属性）SHA-256，冻结于 2026-09-27。适配层只提供实盘 `_observe_variants` 为**这一个**变体提供的胶水：信号 bar、活跃度比值、区间高低点；**永不**调用 `tick`、`_observe_variants`、`_close_if_due`。

```
app.cli.opening_momentum_historical_replay._attempt_receipt_path = f294205c2c5e5f5ce204d6c2ff9b3e414b8fdca9950c2fdb49785be7a91decc2
app.cli.opening_momentum_historical_replay._bar_is_valid = 406b80c061d7cb51d3035bd2e53f1d64579cba044f1b3ba01a8ec19b7417c32a
app.cli.opening_momentum_historical_replay._cache_preflight = bad579a65227d1776706483f20945709423c0ad2fe23b0401df3a631d70b8112
app.cli.opening_momentum_historical_replay._claim_next_attempt = 707d90dfe08cac6677ddec0bdf2a07c7c16598ce3c5dc83f6db457ddc0c25458
app.cli.opening_momentum_historical_replay._collect_member_day_facts = 16579427f921be54831ce2d56b1e79837a3e6443bfebd0368537488b796b4858
app.cli.opening_momentum_historical_replay._compute_descriptives_window = 0d77f61b17536b631a4a89443a736d85a6b7b1c77ce60e9d86d2aa6e5842b5c2
app.cli.opening_momentum_historical_replay._daily_bar_rows = cc0ff6ec1ed69654f7adc056bac956778e7f63d15caac5d3e25ce18db24efa59
app.cli.opening_momentum_historical_replay._evaluate_computation = c2e83a0cae89921545ccd0476feff5acff8288b55dabb9175c79a56f3dabbdd2
app.cli.opening_momentum_historical_replay._fetch_process_alive = ffd60155c75ef53e814b3d775648e171aeda71762556888c1faa39f9e71b58a5
app.cli.opening_momentum_historical_replay._file_sha256 = eed6a789bd63a388d4f67d12debcdc57488c0ed004776434e9b031d075078ec1
app.cli.opening_momentum_historical_replay._frozen_selector_avg_dollar_volume = 1ad99f84a9bbe8fcf7f50f3ccaf324540cc201316add0771ff56ebedd9f30135
app.cli.opening_momentum_historical_replay._last_n_sealed_sessions_before = 9e99d8d3f63f80e9203c0926446602d92434a28d518c413ef3c1eb4fd82c4424
app.cli.opening_momentum_historical_replay._load_attempt_receipt = 1f9dfcc23805f171e2f94c38f3e0df7a77a0734db57ed3fb86af25c2fb3816a8
app.cli.opening_momentum_historical_replay._load_daily_bars = bebcb5948c1f992d17eec2cb1b2f172b87c69b48697fe93cdf85c26297823fab
app.cli.opening_momentum_historical_replay._load_minute_bars = 4e6242dd9d4cbda9d604b71d06f9979b2660d8f3600b5ad7f70e38bc4dd3a897
app.cli.opening_momentum_historical_replay._load_status = 071d8701ec84485438a4bd4781220e2c2ba918d87a1a6ff6efedc401dca410c5
app.cli.opening_momentum_historical_replay._proc_cmdline = e98c6f5532539ac1294990bffbacf8ed32d2e901ad5fd54361757d4351290070
app.cli.opening_momentum_historical_replay._raw_bars_valid_ohlc = 0b313081c228914b5e3325455efe488f10f19840891f9caa15664d4d45f2c750
app.cli.opening_momentum_historical_replay._read_gzip_json = f84acc22e418aa898226eb69bb5a9fdfbef540d5819e86bf8d5f45b56d166ba7
app.cli.opening_momentum_historical_replay._require_clean_worktree = 5da3430504e29b2dbfc94eec77c698a1921cb8939d8e23fd7cd105ec8532c66a
app.cli.opening_momentum_historical_replay._safe_spread_proxy = f9d02b9d17f67c40fb1fb6b32d03fde11a001285a42316d4afef4f391dd412ce
app.cli.opening_momentum_historical_replay._sealed_plan_digest = b2b22f662297f92fd0693bcce37e32e6ade58001381e57dd920c9ea2a90b1620
app.cli.opening_momentum_historical_replay._source_provenance_hashes = 89a69dc54f38ecf790786575f04883a94b88ec6658f2dc76715a9a745cc7e670
app.cli.opening_momentum_historical_replay._verify_sealed_file = 0279cd97964b01c52888e8864b3b3a14033488840b5ec48f0f9e5fb2b44a81bc
app.cli.opening_momentum_historical_replay._write_attempt_receipt = 448ca4e3b9712231f4ff745d534d1db96d50e4e0d3830a3774ec02e867b13421
app.cli.opening_momentum_historical_replay.assemble_member_day_audit = 49b863d068877f4755cca9e9f682e2f05d04f821b5c9db24761b01a5f2c0aba9
app.cli.opening_momentum_historical_replay.build_plan_payload = b01f0c75c03437f5dd05b3df664aacce9f8a4dda242c3f162321f10807e2a684
app.cli.opening_momentum_historical_replay.build_session_observation = 7de3a869d20df85a9e014eaa7dd3347ea035d21443102e6f398a7e0716e06cf2
app.cli.opening_momentum_historical_replay.classify_provider_error = daed59f8bc79923398f1215527daf8ee66aec667d453b19da27fccab084962db
app.cli.opening_momentum_historical_replay.classify_session_adv = 2cb31db44dbde57da516f55836695298a4fd4d1b74db1cc86ff0f034dea948a8
app.cli.opening_momentum_historical_replay.company_dedupe = f4937543bcf9e73f4ca0261c33788080ce0365e6d84824b86830554ab858b3ed
app.cli.opening_momentum_historical_replay.compute_descriptives = 004b4232b527f0b1895ace555670f82d6450f91f123e12f8f85b40d9803d0034
app.cli.opening_momentum_historical_replay.cross_check_trading_days = 373b2035be134ae15db1dce15857adb80ad887b6846fda18fc1bc318f8602199
app.cli.opening_momentum_historical_replay.decide_verdict = 9fc31c7fa01c4e26e6b0f476a1d9df324a49666f90bdf8ed07e78bba35087b09
app.cli.opening_momentum_historical_replay.derive_trading_days_from_benchmarks = 26c06804f8292a859ec23ad3e0f2086b50b8774ae7e6d658650cb5323bd77eb8
app.cli.opening_momentum_historical_replay.evaluate_session_decision = 8b68b413c0fa961fd2ae67b52ff5710063c62bb7b4e97af0069ce167dbf20b14
app.cli.opening_momentum_historical_replay.frozen_config_version = 5faafab72903ab4c5efddcc18f06fd740c3dbc0cdd27162de7d03348ab73e8f7
app.cli.opening_momentum_historical_replay.frozen_decision_config = 052adc8214416f6b37093f7f386b9300a07e5904d1a2c2e7228260b9a5cec1af
app.cli.opening_momentum_historical_replay.import_v2_plan = 79e026d3eb34d5878b181ccc9bc6c45e530feba26bba3b36ac5932c14a611fad
app.cli.opening_momentum_historical_replay.is_fetch_window_open = e4d1a12f4eb0ef90e6559b8ab6ce6440363fed2d4f9b7c4f68335e958eae0230
app.cli.opening_momentum_historical_replay.one_sided_t95 = d2b55ab0623aaf8d4b5e08f78c40882220ec4d26d6d0cbbb3207a062b2b818ce
app.cli.opening_momentum_historical_replay.pit_universe_for_session = 5f0fbe0a81638a39e8b5ef5413b0869a0da16f4fe7088619ec69d69dd1707733
app.cli.opening_momentum_historical_replay.raw_stop_path_is_valid = bb0bf82eab905bc8223cd85d8f2c2f38583d460b8161974ce0f830f7ea54bcd3
app.cli.opening_momentum_historical_replay.rebuild_session_adv = d13b0ad02afb8b0d12f97dc68d19a4a34c9b2c0ddc241ebb2608bfc025fbd5f9
app.cli.opening_momentum_historical_replay.run_evaluate = f0c6aaa77c1d6b40616ec02361cbad4879a5fe74ad2ba17691217b76b2efa34e
app.cli.opening_momentum_historical_replay.run_fetch = 17bbb2afb9e4ea05ef752bef59344875b8154ece9fcc3dd73efe9c13d2e57f48
app.cli.opening_momentum_historical_replay.run_fetch_calendar_warmup = b6b4bb8303fa3fa8ab11b0227a0c3b40a11959dbad4f1b239cf727951689f3b0
app.cli.opening_momentum_historical_replay.run_seal = e7ad382f3cffcf609b4c794259f3f8d6b50ff252b86d9fe724915e83de750a00
app.cli.opening_momentum_historical_replay.session_is_auditable = 632a3f23fd367a1584b600e71f900fd490d399495dc7e0bd170f95509f3ed586
app.cli.opening_momentum_historical_replay.settle_session_exit = 61b9f07a802016b756e653dca28dedd4c5a0385a27c3ce526b47e0c2177c1d41
app.cli.opening_momentum_historical_replay.week_clustered_statistic = 8010f555025ad07985ab8b75c12697695ef4134e620fec9a059460fa851b66c3
app.cli.opening_momentum_historical_replay:ADV_LOOKBACK_BARS = f3ce26c20bd6b921e7619a32503d5b3781316ca759fd1fb94df25c9dae9a98d2
app.cli.opening_momentum_historical_replay:ANALYSIS_ID = f47bf9d5d3c5db49b8f67a2e7cf406ff7e36365015bfbcbcc5a95a99a5164dcd
app.cli.opening_momentum_historical_replay:BENCHMARK_ETFS = 5d3f013aa602c3e9699762d78cf2d0bf91044f9a20eb1bd4043e102de90c9a41
app.cli.opening_momentum_historical_replay:ENTRY_OFFSET = ae44f7a0814040a53d6d354ddd42d5c292605a3586c09374a4fbcddccb1b0749
app.cli.opening_momentum_historical_replay:EXIT_OFFSET = 48cc21900d6eff480e321fa95788dd9bb8cb9de485538531df57cc2fe7ac7c68
app.cli.opening_momentum_historical_replay:FROZEN_CONFIG_VERSION = 5da1a89f78d2a7d610c8f649687910df3afc4043a9e83e9e090da381a8405d04
app.cli.opening_momentum_historical_replay:GATE_B_MISSING_KINDS = 8d87ed60925d668ccaeebf349d5dd59933a874221da2fa70561bc46f7313f735
app.cli.opening_momentum_historical_replay:GATE_MEMBER_DATA_MISSING_MAX_SHARE = e0868b58698c24f5964b9bf46335229725f44c62a7bd98e0f4e33b8a29afdb14
app.cli.opening_momentum_historical_replay:GATE_SESSION_INPUT_COVERAGE = abcef86b08d6c6d4815e17e4dba4b8308d3c18a879994f576c388b08358555a3
app.cli.opening_momentum_historical_replay:GATE_STILL_LISTED_MISSING_MAX_MEMBER_DAYS = 06617bb67e43e8172050f8e8f955d0529fbcfb4a25f8b2fec28fa7ed6a1b2cd1
app.cli.opening_momentum_historical_replay:HOLDING_MINUTES = 026ecacc9efc0d43f4b78d8541aa966239059cdc3434f69e1406ca1ed9705b72
app.cli.opening_momentum_historical_replay:KNOWN_INELIGIBLE_ADV = bf2d4f015ccdcd64eeb1a215772fd624b999c98e2fe9eed754c65a32ee8ca432
app.cli.opening_momentum_historical_replay:MIN_COMPLETED_BARS = 73c3e55d4778d30c3fb80ae849fc82ca5aef47f9d49417a1e349b65736335f61
app.cli.opening_momentum_historical_replay:MIN_TRADES = 29bda80ca6fa505d5c22b30167e8a3ac692bd8483f5f2de86eb7311f8b536d57
app.cli.opening_momentum_historical_replay:MIN_WEEKS = 3ad9503950ca350068c0fc4023bd03cbfcc5b3558abd689929d75aeb44a0dfb0
app.cli.opening_momentum_historical_replay:ONE_SIDED_T95_BY_DF = 0d6e9d6aa4a9679d8042405cc08c21a4ad0d863460958a3292b44b9dde91d079
app.cli.opening_momentum_historical_replay:OPENING_ACTIVITY_TOP_N = db9872c349cc3ab2ec899dc49d6e52124e35817119a84ac6ff5fe72cdfea9fe1
app.cli.opening_momentum_historical_replay:STOP_LOSS_CAP_PCT = 14dc2e3f7fd0c11829bf0f554940bdde989435a778a869645ae25e2ca819d877
app.cli.opening_momentum_historical_replay:STRESS_COST_BPS = 7cbc3dc478820865c754d63d85b3bdf55cabf21b4a7d8a6349e32ff9f793e733
app.cli.opening_momentum_historical_replay:UNVERIFIABLE_DAILY_GAP = b2c9f5b0e6b8517971ff88c9599b6df24952c821d9432153959c73c0f73cfd75
app.cli.opening_momentum_historical_replay:UNVERIFIABLE_FRESHNESS = a767ee4a06a8eb8e44f31dd81c1a7dea18b52739a7b8b78b31dbaa1bab4e4ec2
app.cli.opening_momentum_historical_replay:UNVERIFIABLE_INSUFFICIENT_WINDOW = eb1ec87baae0946ca306dd9d626563f0ed2a7a6558d98bd72853b6c1383fb4f8
app.cli.opening_momentum_historical_replay:UNVERIFIABLE_INVALID_BAR = 3dfe0a6a50ba59c11c62da73158f9fd0e5315019391e3b005069731fcf80f268
app.cli.opening_momentum_historical_replay:UNVERIFIABLE_NO_DAILY = 9509058bf27297b06663586772ee534764621f8cfe0d844db01e4d680015e57b
app.cli.opening_momentum_historical_replay:WARMUP_END = 855daf504bc710e592b114b8e53d41b40a49cf8de48cb463db461f9ee4d40c6f
app.cli.opening_momentum_historical_replay:WARMUP_SESSIONS = 1b37c1c530d30746a4936c6314adeaed2f0ef1f757378020adab54189c05a18a
app.cli.opening_momentum_historical_replay:WARMUP_START = f547041b51aa4326aa57cec5fe159d1822a50d497e6e4fad65df1122f8b2355c
app.cli.opening_momentum_historical_replay:WINDOW_END = b763b1f0f35d4a280dd2decc877bb949c28969767b038f6bf49969d6a4d052b7
app.cli.opening_momentum_historical_replay:WINDOW_START = bf7271237f87d8bf4d9925ed9006efc41d0d9c3a4a21c8147a2c5f568cb15a66
combined = a4672c6c2c12203a487614f9ef4a30190b35de92717cd286062443bc694e8e34
```

**禁止为消红而改哈希。** 任何 pin 失配都要求：先在本文档记录书面决定（动机、处置、对已完成结果的影响），再在**同一提交**里更新哈希与本文档；在结果已存在之后改哈希使既有结果作废，且新的运行**不得**称为第一次 OOS。

## 10. 变更流程（唯一合法路径）

1. 在本文档记录书面决定（动机、新 `analysis_id` 后缀、对既有产物的处置）；
2. 分配新的 `analysis_id` 并同步 CLI 常量与 pin；
3. **同一提交**更新测试 pin 常量、本哈希清单与决定记录；
4. 旧输入与旧输出按第 6.6 条保留，不删除、不改写。

绝不允许：先改代码后补文档、只改哈希不写决定、看过结果后调整窗口/universe/统计口径再跑、或把重跑称为第一次 OOS。
