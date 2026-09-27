# 开盘动量 shadow 确认性预注册合同（OPENING_MOMENTUM_PREREGISTRATION）

> 版本：v1（2026-09-27 冻结）。本文件与 `backend/tests/test_opening_momentum_preregistration.py` 配套，构成一次 **N=1 确认性（confirmatory）前向预注册**。
> 仓库约定 `docs/` 下的中间文档不入库，本合同不属于中间文档，故放在 `domain/` 下（与 `strategy_v2/PREREGISTRATION.md` 同一做法）。
>
> **注册状态：仅授权独立前向 shadow 取证。本合同不授权任何订单。** `opening_momentum_execution_enabled` 被 `config.py`（L1133–1143）在 `merge_longbridge_credentials()` 校验器中强制写回 `False`；即使部署配置打开它，也只会得到一条 warning 然后被忽略。本合同不触碰该边界。

## 0. 这是一份什么文件

这是一份治理合同，不是研究笔记。它注册**恰好一个**开盘动量 shadow 变体作为确认性假设，冻结其规则、统计口径与停止规则，使「悄悄调参直到证据变好看」在机制上不可能。

它与 DESIGN（设计/探索）工作的区别是核心：**E 之前看到过的一切数据只允许用于选择规则，不允许用于认证规则。** 本文第 4 节记录设计快照并声明其认证无效性；第 5 节冻结确认性检验；此后在确认窗口内出现的任何规则改动都触发第 9 节的证据时钟归零。

本合同独立于 `strategy_v2/PREREGISTRATION.md`（v5 负对照），不修改其任何参数、哈希或弃置决定。

## 1. 冻结的假设（一个变体，N=1）

- **变体名**：`INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER`
- **algorithm_version**：
  `cross-sectional-opening-momentum-v3-preopen-frozen-universe+forward-only-5m-orb-stocks-in-play-top10-index-catalog-valid-adv-opening5-turnover-to-prior20d-adv-proxy-next-minute-open-range-low-stop-cap4-hold60-cost30-precommitted-20260728-v1`
- **universe_source**：`OPENING_INDEX_CATALOG_FIVE_MINUTE_ORB_STOCKS_IN_PLAY_TOP10`
- **config_version（全量 SHA-256）**：
  `44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9`

变体定义位于 `backend/app/services/opening_momentum_shadow_service.py`：spec 类 `_IndexCatalogStocksInPlayOrbSpec` 在 **L669–711**（类 L669 起；TOP10 spec 元组 `_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SPECS` 在 **L692–711**），变体 identity 构造在 **L3246–3270**。

`config_version` 的哈希输入（每一项都已在冻结当日核对）：

- `OpeningMomentumConfig.version_hash()`（`app/domain/opening_momentum.py` **L95–110**）：对本变体的 5 分钟 ORB 参数覆盖做规范化 JSON SHA-256；
- 服务端 spec 版本串与 top_n（service **L3250–3253**：`f"{five_minute_orb_config.version_hash()}:{spec.version}:{spec.top_n}"`）；
- `opening_momentum_evidence_config_version()`（`app/domain/opening_momentum_universe.py` **L101–117**），再混入 `UNIVERSE_ALGORITHM_VERSION` 与 `CATALOG_SOURCE_VERSION`；
- 五分钟 ORB 参数覆盖构造在 service **L2884–2894**（`_variant_identities()` 内）。

以上四项串起来即冻结哈希 `44e3c377…def9`；配套测试用两条独立路径（服务变体表 + 纯 domain 重算）复核。

## 2. 实际规则（逐条对照代码核实）

以下每一条都是代码的实际行为，不是意图描述。行号以 2026-09-27 的 HEAD 为准。

**标的池（universe）。** 取开盘前（`completed_before=session_open`）最近一次 `status == "COMPLETE"` 且 `as_of_date < session_date` 的 universe selection run（service **L2372–2396** `_latest_universe_selection_run`）。取该 run 的**全部 US 候选**里 `metrics_json.avg_dollar_volume` 为有限正值的 symbol（**L2485–2496** `index_catalog_symbols`；`_optional_metric` **L5395–5403** 拒绝非有限值）。**`selected` 不是必要条件**——候选行不筛 `selected`。变体的实际 symbol 列表绑定 `selection_run_id`（**L2771–2777**）。上游目录为 `INDEX_CANDIDATE_CATALOG`（`app/domain/universe_selection/catalog.py` **L50 起**，123 个条目）。

**ADV（日均成交额基线）。** `avg_dollar_volume` 由 universe selection 的 `selector.py` 计算：取最近 20 根**已完成日线**的美元成交额均值（**L265–266**）；单 bar 美元成交额优先取 `turnover`，缺失或非正时回退 `close × volume`（`_dollar_volume` **L206–210**）；进入候选至少需要 21 根有效日线（`min_completed_bars`，**L58**，指标计算窗 **L242–247**）。注意这是**日频美元 ADV 代理**，不是同窗口相对成交量。

**活跃度排名（activity）。** 开盘前五分钟（09:30–09:34 共 5 根 bar）的分钟 turnover 之和（`_signal_turnover` service **L5061–5069**，任一分钟 turnover 缺失则为 None）除以 ADV（**L1817–1833**）。按比值**降序**取 TOP10，并列按 symbol 升序（`opening_momentum.py` **L361–374**；服务端排除逻辑 **L1912–1941**）。**没有任何最低比值门槛**（`minimum_opening_activity_ratio` 为 None）。比值缺失的 symbol 记 `OPENING_ACTIVITY_DATA_MISSING` 且使当日 `data_complete = False`（对 `DAILY_ADV_PROXY` 基线要求全部 symbol 都有比值，service **L1896–1900**）。

**突破（breakout）。** 开盘前五分钟的 high/low 被冻结为开盘区间（service **L1800–1805**）。第 6 分钟（09:35）收盘价**严格高于**区间 high 才算确认突破（`opening_momentum.py` **L434–443**）。在 TOP10 中按突破深度（`signal_close/range_high − 1`）降序选一，深度并列按开盘收益，再按 symbol（`candidate_key` **L492–508**）。**没有最低突破深度门槛**（`minimum_breakout_depth_bps` 为 None）。

**入场（entry）。** 入场价为 09:36 ET 分钟 bar 的 open（`_variant_entry_at` service **L1604–1616**：`signal_minutes(5) + execution_delay_minutes(1)`；观测构造 **L1734–1783**，entry bar 缺失记 `ENTRY_BAR_MISSING` 不入场）。最小 universe 为 `minimum_universe_size = 8` 且观测覆盖率达到 `minimum_data_coverage = 0.95`（`_EARLY_BROAD_MINIMUM_COVERAGE`，service **L107**、**L1868–1874**、**L2088–2095**）。

**出场（exit）。** 止损价 = `max(range_low, entry × 0.96)`：开盘区间低点与 4% 硬上限取较近者（`_opening_range_stop_loss_pct` service **L5079–5099**，`_OPENING_RANGE_STOP_MAX_PCT = 4.0` 在 **L289**）。算出的止损无效（区间低点高于等于入场价等）则当日不入场，记 `OPENING_RANGE_STOP_INVALID`（**L2164–2182**）。**4% 不是硬亏损上限**：它只是止损距离的封顶，实际止损距离由区间低点决定。结算时，开盘价直接跳穿止损按 bar open 成交，否则按止损价成交（`_exit_outcome` **L3988–4021**）；无止损触发则持有满 60 分钟后按当根 bar open 出场（`FIXED_HOLD_EXIT`，**L4024–4036**；入场 bar 开盘价入场，**L2300–2308** 出场期限）。

**成本（cost）。** 单边 fee 5 bps（`one_side_fee_rate = 0.0005`）加单边滑点 10 bps（`one_side_slippage_bps = 10.0`），往返 30 bps（`round_trip_cost_bps`，`opening_momentum.py` **L88–93**）。落库时 `estimated_cost_bps = 30.0`（service **L2309**），`net_return_bps = gross_return_bps − estimated_cost_bps`（**L3928–3940**）。

**与研究文献的关系（明示）。** 本变体**不是** Zarattini, Barbon & Aziz 的「Stocks in Play」ORB 复制。文献用同窗口相对成交量（RVOL）定义 in-play；本变体用的是**日频美元 ADV 代理**（前 20 个完整交易日的日均美元成交额）作分母。文献只提供机制先验（开盘活跃异动延续），不提供本规则的参数依据。

## 3. 冻结哈希的三层 pin（测试实现）

配套测试 `backend/tests/test_opening_momentum_preregistration.py` 钉三层，全部从权威源在测试时重建，绝不使用硬拷贝：

1. **config_version**：从服务变体表重建目标变体，断言全量哈希等于 `44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9`；同时从纯 domain 函数独立重算同值。并断言未入哈希但影响语义的字段：top_n 10、`DAILY_ADV_PROXY` 基线、20 日回看、coverage 0.95、hold 60、止损封顶 4.0、成本 30 bps（fee 5 + 滑点 10 单边）、`opening_range_stop=True`。
2. **目录内容**：`INDEX_CANDIDATE_CATALOG` 的规范化 JSON（symbol/alias/sector/memberships）SHA-256 = `3678a732e5e1d527633912424cacfeadf8bc6339899c1b6fec1f8f1c3353b921`（123 条），另钉 `CATALOG_SOURCE_VERSION = nasdaq-100-2026-07-24_djia-2026-06-29_historical-pit-v9`。
3. **源实现**：对下方「code manifest」显式清单里的每个函数/常量做 AST 规范化（`ast.dump`，不含属性，注释与格式变化不触发）SHA-256，再合成 combined pin =
   `1a9e1515ba5f4070e3023d7218f0286ccd4f37d68adf565c10c67a94a1c6cecb`。
   采用函数级而非整文件哈希，避免巨型 service 文件里的无关改动误触发。

**禁止为消红而改哈希。** 任何 pin 失配都要求：先在本文档记录书面决定，分配新版本与新 E，再在**同一提交**里更新哈希与本文档。任何改动都使证据时钟归零（第 9 节）。

## 4. 设计快照（DESIGN，认证无效）

E 之前的所有前向 shadow 数据（2026-07-28 至 2026-09-25，47 个变体）都是 DESIGN。本变体被选中时的设计快照：

- TOP10 cohort：**n=25**，净均值 **+90.3 bps**，中位 **+46.0**，t 1.69，未校正单侧 p 0.052，剔除最优交易后均值 **+60.7**，22 个不同 symbol；
- 47 个变体中 12 个为正；对 47 做 Bonferroni 校正后 **p ≈ 1**；
- 系统配对比较（对共同日基线）**INCONCLUSIVE**，CI [−49.9, +140.3]。

**这些数字不能认证任何东西。** 它们被用于——且仅被用于——在 47 个候选中**选择**这一个规则。选择行为本身消耗了统计资格：单假设名下的「确认性 N=1」只有在全新窗口、只评这一个冻结规则、不中途改规则、不与同窗口其它变体比较时成立。若在确认窗口内重新评分旧候选后择优、比较本假设的参数版本、或按中期收益修改规则，则 N=1 资格失效，必须重新注册试验族并补全多重检验校正（对照 v5 合同 §10.8 的试验族条款）。

## 5. 确认性检验（冻结，不再逐次重议）

### 5.1 E 与注册时效

- **E = 2026-09-28**（US/Eastern 交易日）。
- 本注册**仅当在 2026-09-28 13:30 UTC 之前 commit 并 push 到 main 才计数**。错过该时限，E 顺延到**事先公告**的下一个交易日；结果已被看过的交易日永远不能作为 E。

### 5.2 主估计量与假设

- **主估计量**：30 bps 成本下的交易加权平均 `net_return_bps`。
- **H0：μ_net ≤ 0**，单侧 **α = 0.05**，**恰好一次**终局成功检验。

### 5.3 样本与终局点

- **样本**：按入场 `session_date`（≥ E）排序的**前 125 笔**交易，且其中至少覆盖 **26 个不同日历周**（按 `session_date` 的 ISO 周计）。
- **终局点（terminal）**：第 125 笔交易，或自 E 起计的第 **252 个 US 交易日**，先到者为准。**窗口绝不因未达显著而延长。**

### 5.4 统计量（周聚类）

设 W 为有交易的日历周数（ISO 周），n 为样本量：

- `S_w = Σ_week (r_i − mean)`（每周离差和）
- `SE = sqrt( W/(W−1) · Σ S_w² / n² )`
- `L = mean − t(0.95, W−1) · SE`（单侧 95% 下界，df = W−1；此处 `−` 为数学减号）
- **`stress_L = L - 20`**（50 bps 总成本的压力列；即从下界再扣 20 bps）

### 5.5 判定

- **PASS**：完整性门（第 6 节）通过、样本下限（125 笔 + 26 周）满足，且 **`stress_L > 0`**。三者 AND。
- **NOT_CERTIFIED**：样本充足但门槛未达，或 futility 停止触发。**这不是负期望的证明**（对照 v5 §9.1 的禁止事项：不显著 ≠ 已证明为负）。
- **INSUFFICIENT_DATA**：终局点未到、样本不足、存在未结清交易、或完整性无法核实。预算（252 交易日）到期即终局，**绝不自动延期**。

**诊断量（永不参与判定）**：普通 t、中位数、剔除最优交易后的均值。

### 5.6 Futility（仅限 60 与 90 笔，永不提前 PASS）

只在第 **60** 笔与第 **90** 笔各检查一次：

- `U = mean + 2·max(SE_week, 267/√n)`
- 若 **U < +30 bps** → 停止，判 `NOT_CERTIFIED`。

它停止的是「追求经济目标」；它**不是** μ ≤ 0 的证明。futility 判定同样需要书面记录（对照 v5 §9.5.4）。

### 5.7 现实时效（诚实陈述）

- 观测频率约 **0.6 笔/交易日**：125 笔 ≈ 208 个交易日 ≈ **10 个月**，在约一年的预算内可达；
- +90 bps 基准效应（压力列 +70）约需 **90 笔**即可检出；
- **+60 bps 基准效应（压力列 +40）约需 276 笔 ≈ 22 个月，超出预算**——若真实效应只有这个强度，本设计可能在预算内无法认证它。这是注册时已知的功效缺口，不是事后借口；
- 周聚类可能进一步降低功效。

## 6. 完整性门（阻断 PASS；不阻断注册）

1. **会话对账**：E 起每个 US 交易日必须能归类为：正常无信号 / 数据失败 / 漏采集 / OPEN / CLOSED 之一。未知缺口**永不**用零填补。前 125 笔按会话顺序取，不按结算顺序取。存在未结清（unresolved）交易则阻断 PASS。
2. **选择 run 核对**：检验前核对实际 selection run 的版本、cutoff、universe 与成本，与本合同一致。
3. **已知审计风险（已核实并引用）**：`_close_if_due()`（service **L3821–3911**）先检查分钟路径完整性（`_minute_path_complete` **L3950–3966**），不完整时尝试历史回填（**L3871–3903**），但**回填之后不再复查完整性**；且 `_exit_outcome()`（**L3968–4036**）只迭代**已存在的 bar**（**L3988–3992** 按时间戳过滤）。因此缺失分钟内部的止损可能被漏掉，而该交易已按 `FIXED_HOLD_EXIT` 关闭。**没有可证明完整止损路径的证据不得 PASS。** 若未来修复改变了出场语义，则该修复本身要求新版本与新 E，且旧样本不得混入新样本。
4. **外部数据不可 pin**：供应商数据修订、SDK 版本或复权口径变化（日线研究用 `ForwardAdjust`、分钟用 `NoAdjust`；`universe_selection_service.py` **L196–212** 与 `broker.py` **L1309–1339**）无法被哈希捕获。运行留痕必须记录供应商与 SDK 版本，并保留 run 溯源（selection_run_id → run 的原始输入快照）。

## 7. PASS 授权什么

**仅人工复核。** 不授权实盘切换、加仓、或任何「稳定月收益」声明。任何后续实盘试验都需要单独的显式审批，内容至少覆盖：

- 最小可交易规模；
- 固定的资金、总亏损与时限预算；
- 对真实费用与成交偏差的接受（小额交易的最小佣金可能远超 30/50 bps）；
- 每笔订单必须走既有的唯一 pre-submit 风控边界（`TradeExecutionService.pre_submit_risk_check()`）。

**禁止 shadow → live 自动晋级**（常驻 P0：仓库 AGENTS.md「no short entries / no add-ons / LLM never places live orders / shadow paths never submit or auto-promote」；`config.py` L1133–1143 强制 `opening_momentum_execution_enabled = False`）。

## 8. 评估器与 `forward_evidence_start_date`（不得编辑）

- **评估器**：纯 domain 评估器**另行实现**（本合同只冻结其规则）。它数据进、数值出，不访问 DB 与 settings（domain 纯度契约）；过滤 `config_version == 44e3c377…def9 AND E ≤ session_date ≤ terminal`，并执行第 6 节完整性检查。
- **`forward_evidence_start_date` 不是确认性边界，不得编辑**：本变体该项为 `date(2026, 7, 28)`（service **L228**、identity **L3267–3269**），它不进入 `config_version` 哈希；改动它会静默改变既有 API 的证据区间（tick 过滤 service **L1102–1113**；`_variant_responses` 证据过滤 **L4088–4097**），等于篡改已收集证据的口径而不触发任何哈希。确认性边界**只有**本合同的 E = 2026-09-28 与终局点。

## 9. 证据时钟与无重选

- **任何**注册规则的改动（哈希任一层、目录内容、清单内函数语义）都把确认性证据窗口归零：此前所有交易日与交易归属旧规则。
- **无重选**：若 TOP10 失败，TOP5、TOP20 或同窗口任何其它变体**不得**援引本 PASS。它们是不同假设，须各自注册。
- 禁止在确认窗口内做参数探索、出场重调、门槛微调或专项分析后把结果归入本 cohort；新增分析须另行注册（对照 v5 §6/§9.5.3）。
- 判定标签三分（PASS / NOT_CERTIFIED / INSUFFICIENT_DATA）不得混写；`INSUFFICIENT_DATA` 永不被压制或四舍五入。

## 10. Code manifest（源 pin 清单）

以下清单与测试中的 `_MANIFEST` 逐项一致（测试的 doc-agreement 断言双向同步）。哈希为 `ast.dump`（无属性）SHA-256，冻结于 2026-09-27：

```
app.services.opening_momentum_shadow_service:_EARLY_BROAD_MINIMUM_COVERAGE = 8f4a1235479b3997db4714f6604cc73fd995d2b5dd634e76c70fb2bff736d042
app.services.opening_momentum_shadow_service:_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SPECS = e9cb7c49b54e72c85bbef0039522103771ce14fa044b572734f905c2718d0c53
app.services.opening_momentum_shadow_service:_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_SOURCE = 68d03491d3f6be948db127e5a9329db840199c9706cfdf9822437380e6724d83
app.services.opening_momentum_shadow_service:_INDEX_CATALOG_STOCKS_IN_PLAY_ORB_VERSION_SUFFIX = d622c68f1c7648778f752af203efc00aa3bcd94f044d0ed0a2d3239077bafa01
app.services.opening_momentum_shadow_service:_OPENING_RANGE_STOP_MAX_PCT = c81497319e168125208ea72246a2ef3c0d2f5693d59969bea083b314d7856ea5
app.services.opening_momentum_shadow_service:_POST_20260727_FORWARD_EVIDENCE_START_DATE = 183d4201bf15897c3a25896502f8eb0be930c22490ad036d93ef8262e91ff0eb
app.services.opening_momentum_shadow_service._IndexCatalogStocksInPlayOrbSpec = a9a7723735aa76584dc06f8497acabbdd29d6e993819bb29dd756248adce8886
app.services.opening_momentum_shadow_service._candidate_avg_dollar_volume_by_run = 4e98180002e0796b8e76c4e10c7d56dec2a5325c43f49c1d3d73d9ea54ade5c4
app.services.opening_momentum_shadow_service._close_if_due = a1026d2a387d11766916aee6702b1292963adf24ca4e775f3273a0b3581dff76
app.services.opening_momentum_shadow_service._coerce_candles = 77372a43fb90cd54b590188109ddc583c849382d0f4fec44201c116ff2024502
app.services.opening_momentum_shadow_service._evaluate_variant_decision = 1a22870b6e662c34d4aff3c2cde0d907bb0f59d33ca2701f32df45bde78e28c9
app.services.opening_momentum_shadow_service._evidence_config_version = cfedc14472245c0a53f7dc5c95bae4cb5081d5500bbe02778d9d1d67cf005986
app.services.opening_momentum_shadow_service._exit_outcome = b3a32ffd9639a7e1a961d9d4e6ab3d407fc8571622deb39fe3187cca4794ac12
app.services.opening_momentum_shadow_service._historical_candles_before = 38c6be6cacbe32446a05d8226006945e2f8c41fcca0e75f79833079ee8a978c4
app.services.opening_momentum_shadow_service._latest_universe_selection_run = cb2e2c913688651977fb86c5f4f53b1f0ae3be91b8f072c64f6f145dcdd2e27a
app.services.opening_momentum_shadow_service._minute_path_complete = 1bdf3ab4cd1fbb93388e6fbc086ef308e5cbb60fc741a7b3b049274febac5468
app.services.opening_momentum_shadow_service._observe_variants = 0d681dc2a375d66035d95aa2b4d33621c47fad3054cbdf15af081f0027c066d8
app.services.opening_momentum_shadow_service._opening_range_stop_loss_pct = 26a5e45c65ce7a5bc9433982d56faae896b4943f440827657ac9ea67b32fa107
app.services.opening_momentum_shadow_service._optional_metric = 51f45762f16aa5f7a37476c2ae325d41112bf615937096592779bf67ec607eef
app.services.opening_momentum_shadow_service._signal_turnover = 861a702125c57c06afc27432b0628a4ec05069e620a79da63c3c4e664cdba48e
app.services.opening_momentum_shadow_service._universe_variants = 744e56c0e834ee3230f2b2bb996bac9885377d5a2d6c76544754f6a98d5635c2
app.services.opening_momentum_shadow_service._variant_decision_due = dc1cba2c46dfed4539bb7ed78d2a2767dc1024d65aada8b7cd4c5d4b83d3f118
app.services.opening_momentum_shadow_service._variant_entry_at = bc8946ab27a9d79ba5a2799629e7b91201505a87926a96e57a4019bb8a50b6f8
app.services.opening_momentum_shadow_service._variant_identities = cb21cab7a83768336e93346adc0dc8f8415bc813da26a448bf57f3598d162af1
app.services.opening_momentum_shadow_service._variant_signal_at = 33755082816a0a6bfa47ad48ad1348ef210e78cf59e0b85f641beb398bbee630
app.domain.opening_momentum.OpeningMomentumConfig = 5276331bec5fecb3aed023174b6f4d2a5347b867376c4fae172d014654922324
app.domain.opening_momentum._evaluate_opening_range_breakout = 66894cb8b08bf9f10a6482ae8ef94f83932b821b6d5282277519656c69f923db
app.domain.opening_momentum._rank_opening_observations = d1cd4414bd2974ed04dc63e1b210bde24cf958fbf28fe1f27240625bd3d7cbfb
app.domain.opening_momentum.evaluate_stocks_in_play_opening_range_breakout = db687d98d6cc5d1f93fa25fa02c404b72ee73ee45ce0dab258965d541edb32a7
app.domain.opening_momentum.shadow_round_trip_return_bps = 7c7c1a82909ca675e624bc478ba1e0a9c3d17251af5a71714df0abd8cd4f6f0c
app.domain.opening_momentum_policy.opening_execution_config = 4eba43ea83ecdc05b86ffa9f1cd5b2ea87c3ad99ade7f07675c453be32b847d4
app.domain.opening_momentum_universe.opening_momentum_evidence_config_version = 1fa531c66d0f3ce2ef27f602492203d3f3e29ec1b5e005fb64d1f0435b337022
app.domain.universe_selection.catalog:CATALOG_SOURCE_VERSION = c832a386b9803e91f735c5385ec1a56ddee45398b7c57701b49405235d7301ea
app.domain.universe_selection.selector.UniverseSelectionConfig = 20ade6861b05b1d9954ef9ad993f005f0a71c383a19c25f71a865e2f7eb3f01e
app.domain.universe_selection.selector._candidate_metrics = 3f351ae1c30c187b5512511e2b023ac88cca9edb69137014d904563afd98d80c
app.domain.universe_selection.selector._dollar_volume = 70682ded849177ec609de48db0afe8ceb62916389c4e3b66d437377492d38e0b
app.services.universe_selection_service._research_candlesticks = be5bd9e060a5eef443bb5e5c8930ad93b87302f53fff0736d4a531e3cc01c9ae
app.core.broker._get_candlesticks_inner = 258a1b7ca2baf4f3c0e86ee8cfb0d9f3cab81dd54ccee4df8d45f4b624f9f466
app.core.broker.get_candlesticks = 6d23e2b9d26599bc3d7eb031fa45d2d6f08e624f699d95e0cd57f094554cf8f9
app.core.broker.get_forward_adjusted_candlesticks = 1f059155b2f7dd7380c9a22194137bd4c2fab19c59fc5fa7f96ce60daad146e9
combined = 1a9e1515ba5f4070e3023d7218f0286ccd4f37d68adf565c10c67a94a1c6cecb
```

清单覆盖：universe 选择（`_latest_universe_selection_run`、`_universe_variants`、`_candidate_avg_dollar_volume_by_run`）、ADV（`selector._dollar_volume`、`_candidate_metrics`、`UniverseSelectionConfig`）、活跃度排名（`_signal_turnover`、`evaluate_stocks_in_play_opening_range_breakout`）、突破选择与入场（`_evaluate_opening_range_breakout`、`_rank_opening_observations`、`_evaluate_variant_decision`、`_variant_entry_at`）、止损与出场（`_opening_range_stop_loss_pct`、`_minute_path_complete`、`_exit_outcome`、`_close_if_due`）、成本（`OpeningMomentumConfig`、`shadow_round_trip_return_bps`、`opening_execution_config`）、版本绑定（`opening_momentum_evidence_config_version`、spec 常量与类）、以及可达的券商 K 线转换路径（`_research_candlesticks`、`BrokerGateway.get_candlesticks` / `get_forward_adjusted_candlesticks` / `_get_candlesticks_inner`）。

## 11. 变更流程（唯一合法路径）

1. 在本文档记录书面决定（动机、新版本号、新 E、对旧样本的处置）；
2. 分配新的 `algorithm_version` 尾串与新的 `config_version` 哈希；
3. 在**同一提交**里更新测试的 pin 常量与本文档的哈希清单；
4. 证据时钟归零，旧样本不混入新 cohort。

绝不允许：先改代码后补文档、只改哈希不写决定、或在确认窗口内「微调」后继续用旧样本认证。
