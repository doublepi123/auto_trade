# 月度趋势冻结规则预注册合同（SPY_MONTHLY_SMA10_CASH_V1）

> 版本：v1（2026-09-28 冻结，先于任何结果 commit）。本文件与 `backend/tests/test_spy_monthly_sma10_preregistration.py` 配套。
> `analysis_id = spy-monthly-sma10-cash-v2`（v1 见 §14.7：第 1–4 项 MUST-FIX 改变分析语义后升级；v1 已注册、未执行、无任何结果）。实施 CLI：`backend/app/cli/spy_monthly_sma10_replay.py`（子命令 `plan | fetch | import-corporate-actions | seal | evaluate`）；纯计算实现：`backend/app/domain/monthly_trend/sma10.py`。
>
> **注册状态：只读历史研究。本合同不授权任何订单、任何实盘切换、任何 shadow→live 晋级。所有者已暂停实盘入场；本合同是暂停期间的一条新研究线，不是恢复交易的依据。**

## 0. 这是一份什么文件

这是对一条**全新假设规则** `SPY_MONTHLY_SMA10_CASH_V1`（SPY 月度 SMA10 现金择时，Faber 2007 风格）的**预注册式历史验证治理合同**。它在**任何结果产生之前**冻结标的、信号、执行、成本、窗口、估计量、统计量、样本门槛与判定词汇，使「先看历史再决定要不要看历史」在机制上不可能。

它回答的唯一问题是：**这条规则在 2012-01..2021-12（120 个完整月）上，相对 SPY 买入持有，是否以可接受的收益让渡证明了对下行风险的降低？** 它不回答「是否应该实盘」——那是完全独立的产品决定，需要独立的前向证据。

## 1. 定位与命名

- 这是**冻结规则的历史验证**，不是前向认证。判定词汇与本验证绑定：`CORROBORATES_RISK_MANAGEMENT_VALUE / DOES_NOT_CORROBORATE / INCONCLUSIVE / INSUFFICIENT_DATA / DATA_BLOCKED`。这些词**永不**写为 `PASS / NOT_CERTIFIED`——它们属于前向合同。词汇混写即合同违约。
- 判定结果是**一次性 OOS**：evaluate 只跑一次；看过结果之后的任何重跑不再是第一次 OOS，必须带 `--rerun-reason`，原输出保留为 superseded 文件。
- 本验证**永不**给任何前向 cohort 增加交易；**永不**触发任何订单路径。

## 2. 独立性与已知重叠（诚实清单）

1. **注册诚实声明（orchestrator 审计，2026-09-28）**：本地研究工件与数据库**没有**任何覆盖 2012–2021 的 SPY/QQQ 数据——`data/research` 0 命中、2022 年之前 0 DB 行、也没有任何既有 SMA10/Faber 代码或文档。因此本机内部不存在对这些年份的先验偷看。
2. **但这不是「没人看过」的证明。** Faber（2007）在更早的数据上发表了同型规则；2012–2021 相对该论文是**发表后 OOS**，不是发现性样本。任何把本窗口当作「未研究区间」的表述都是错的，按本条纠正。
3. **数据下界。** LongPort US 日线自 2010-06 起，因此 **2000/2008 无法测试**。任何暗示本验证覆盖金融危机熊市的表述都是错的。
4. **无法移除的一般市场知识。**「2012–2021 美股长牛」属于公共知识，注册者无法遗忘。处置办法不是假装中立，而是**登记 + 禁令**：禁止依据该知识调整窗口边界或分段；描述性切片永不参与判定（第 7 节）。

## 3. 冻结规则

- **标的与基准。** 唯一可交易标的为 `SPY.US`。`QQQ.US` 仅为次级披露基准，**永不**替代 SPY。
- **信号。** 第 m 月最后一个完整交易日收盘后：`I_m = 1{T_m > mean(T_{m-9..m})}`，T 为仅用该日已知信息构建的全收益指数（价格 + 现金股息再投资进**指数**——仅用于信号，见股息注记）。相等即现金。
- **执行。** 0→1：下一交易日开盘买入一次；1→0：下一交易日开盘全部卖出。信号不变不交易。不加仓、无 DRIP、无做空、无杠杆、无日内止损、无时间退出。
- **研究账户（sleeve）。** 初始现金 5,000 USD。入场买入满足可用现金+费用、≤100 股、**执行名义（开盘价加滑点）≤5,000 USD** 约束下的最大整数股（决定 14.7 第 4 条）。无外部现金流，持有期间不再平衡。**股息权利在除息日开盘前按当时持股固定**；未派部分为 NAV 内应收款（不可买入），派息日转现金（未知 pay date 时用除息日，§4.3），**不再投资**（决定 14.7 第 1 条）。闲置现金收益为 0。市值涨超上限不减持。**期末清算按时间顺序执行，窗口结束后不开新仓**（决定 14.7 第 2 条）。
- **基准。** SPY 买入持有，同初始现金与入场上限：第一个计分月（2012-01）**首个交易日开盘**买入，股息记现金，期末清算计入清算成本。QQQ 买入持有与零收益现金仅作披露——且 QQQ 为 **price-only**（注册股息源无 QQQ 行，见 §14.5：不贷记股息、永不进 claim）。
- **每单成本。** 佣金 `1.568 + 0.0000641 × 成交名义` USD，常量**引用自 `backend/app/core/accounting_fees.py`**（`SEC98_FIXED_USD` / `SEC98_NOTIONAL_RATE`，CLI 直接 import；纯模块以 Decimal 字面量重复声明并由 pin 测试断言数值相等，漂移不可能静默发生）。基础滑点每边 5 bps；压力情景每边 15 bps。**最终清算在两种成本情景下都计费。** 收益为费用后、个人税前。另披露固定股息预扣敏感性（30%，仅披露）。
- **窗口。** 主验证 2012-01..2021-12：120 个完整月。2010-06..2011-12 仅 warm-up（SMA 需要 10 个月末，2011-03 月末起信号首次可用）。

### 股息注记（信号 vs 账户，两条不同的通道）

- **信号通道**：T 指数在**除息日**把股息再投资进指数（除息日收盘价已含除息下跌，该日再投资是标准 point-in-time 全收益口径）。拆股对 T 中性（日收益比乘以拆股比率抵消价格跳变）。**月末日之后**除息的股息不可能进入当月 T_m——这是「无偷看」的构造保证（由测试钉死）。
- **账户通道**：股息在**派息日**（pay date；来源未知时用除息日，**本合同注册的回退决定**）记为现金，不再投资，也不进信号。
- 两条通道**永不**混用：股息永不进入账户持股，也永不从账户口径倒灌进信号指数。

## 4. 冻结分析计划

### 4.1 月度配对序列

rS = sleeve 月末权益收益率，rB = SPY 买入持有月收益率，**包含现金月**（sleeve 持币月 rS = 现金收益 = 0，除非当月有交易成本冲击）。首个计分月（2012-01）的收益率相对初始 5,000 计量。**边界（2011-12 信号，决定 14.7 第 3 条）**：若 2011-12 信号非平仓，sleeve 在 2012 年首个会话开盘即建仓，rS(2012-01) 因此反映当月持仓；若为平仓信号，2012-01 无交易。任一情形都不是「恒为 0」——由边界信号决定，缺失即 DATA_BLOCKED。

### 4.2 四条 claim（全部必须过，各以单侧 95% 下界判定）

1. `E[rS] > 0`（长期期望为正）。
2. `E[rS − rB] > −0.001`（平均月度让渡至多 10 bps）。
3. `E[0.8·min(rB,0)² − min(rS,0)²] > 0`（下行二阶矩至少降低 20%）。
4. 压力成本（每边 15 bps）下净月均下界 > 0。

### 4.3 已注册决定（规格未明处，先于结果冻结）

- **股息入账日**：优先 pay date；来源未给出 pay date 时回退 ex-date。已在 §3 股息注记登记。
- **派息日早于除息日**的脏数据：按 DATA_BLOCKED 处理（§6.4），绝不猜测排序。
- **基准买入时点**：第一个计分月的首个封存交易日开盘（“第一个合格月的开盘”的字面执行；sleeve 与基准在同一窗口、同一初始现金下配对）。
- **清算日**：最后一个计分月（2021-12）月末的**下一个**封存交易日开盘；sleeve 与基准同日清算，清算成本（佣金+滑点）在两种成本情景下都计。
- **月度收益率首月基准**：相对初始现金（5,000），非相对前一月末。
- **信号相等（T_m == SMA10）**：现金（I_m = 0），严格大于才持有。
- **warm-up 期间永不交易**：进入计分窗口时仓位恒为 FLAT。
- **封存日历的月末** = 该日历月最后一个封存交易日（SPY ∩ QQQ 日线交集）。

## 5. 统计量（冻结）

- **自助法**：配对月序列的**循环移动块 bootstrap**（circular moving-block bootstrap），块长 12（block length 12），重采样 10,000 次，固定种子 **20260928**，单侧 95% 百分位下界（5th / 95th 百分位）。
- **配对保持**：每条 claim 序列按月对齐，同一 BootstrapConfig（同种子、同块长）独立自助——种子相同 ⇒ 各序列的块起点索引完全一致，配对月结构在重采样中保持（由测试钉死：同种子下两条序列的块起点序列相同）。
- **最低样本**：120 个连续月、≥12 现金月、≥60 持仓月。

### 5.1 退化样本政策（已注册）

常数序列（如全 0）的自助均值恒等于该常数，下界 = 上界 = 均值。政策：**判 INCONCLUSIVE**。理由：常数序列既不能证实方向（claim 1 下界 = 0，不 > 0），也不能证伪（上界 = 0，不 ≤ 0 的严格证伪条件依 claim 而定）。**永不报错、永不判过。**

### 5.2 功效诚实声明

月效应 0.5%、月波动 3% 时，80% 功效约需 **223 个月**。本窗口 120 个月**很可能 INCONCLUSIVE**——这不是失败，是样本量的数学事实，注册在先以防事后解读为「规则无效」。

## 6. 判定映射（冻结）

- `CORROBORATES_RISK_MANAGEMENT_VALUE`：数据与样本门全过，且 4 条 claim 各自的单侧 95% 下界全部过阈。
- `DOES_NOT_CORROBORATE`：样本充分，且**至少一条** claim 的单侧 95% **上**界 ≤ 其阈值。
- `INCONCLUSIVE`：其余（界跨阈、退化样本、样本不足但数据完整且不想判 INSUFFICIENT_DATA 的边界由 §6.5 处理）。
- `INSUFFICIENT_DATA`：数据完整但样本量不足（<120 月、<12 现金月或 <60 持仓月）——与 INCONCLUSIVE 严格区分：前者是「样本太小」，后者是「样本够但证据不分向」。
- `DATA_BLOCKED`：缺月末、缺次开、缺或不确定的股息/拆股数据。**绝不填补、绝不丢月。**

### 6.5 词汇纪律

`INSUFFICIENT_DATA` 与 `INCONCLUSIVE` 是两个不同判决；`DATA_BLOCKED` 是输入缺陷不是统计结论。任何把 DATA_BLOCKED 月「修好再跑」的行为都需要新的书面决定与新的 analysis_id。

## 7. 描述性切片（永不判定）

月胜率、最差月、最大回撤、最长连亏、滚动 12 月收益、敞口占比、QQQ 差异。明示：

- **“稳定”的含义是长期正期望加可接受回撤，不是每月盈利**；
- 量级感：5,000 USD 上每年 8% 的假想收益约合**每月 33 USD**；
- 功效弱：0.5%/月、3% 月波动的效应在 80% 功效下需约 223 个月，120 个月很可能 INCONCLUSIVE。

## 8. 数据契约（关键，不可通融）

- 封存 SPY.US 与 QQQ.US **2010-06-01..2022-01-31** 的 RAW 日线 OHLC（NoAdjust），另加**独立封存**的股息/拆股文件。
- **绝不**把 `ForwardAdjust` 价格当作可执行全收益账户。股数、名义与固定费用必须用 RAW 价格。（本验证只用 NoAdjust，前复权数据永不进入账户路径。）
- 股息/拆股来源**已注册并强制执行**（§14.5/§14.6）：SSGA `spdr-etf-historical-distributions.xlsx`（完整 sha256 `51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad46877c54`，SPY 2010–2021 恰 48 条季度记录；URL 见 §14.6）。CLI 输入：上述 xlsx（stdlib `zipfile+xml` 解析，**sha256 逐一强制**）；封存后的缓存文件为 canonical JSON（`corporate_actions.json`，含来源与哈希）。**仅限合成测试**的原始 JSON 导入路径不携带注册格式，真实运行 seal 一律拒绝。`fetch` 只抓 LongPort RAW 日线；`import-corporate-actions --file X --source-url U` 子命令哈希并存储。**seal 在该文件缺失、来源格式非注册 xlsx、源哈希不符或 48 季事实不成立时拒绝（DATA_BLOCKED）。** 拆股由 RAW 价格屏幕证明"无"（§14.5 第 3 条），不由源的沉默证明。
- **交易日历** = SPY ∩ QQQ 封存日线交集（沿 ORB replay 的基准交集做法）。本地日历不覆盖 2012–2021，**永不**使用。
- 月末 = 每个日历月最后一个封存交易日；「次开」= 下一个封存交易日的开盘价。

## 9. 保真度与已知偏差（逐项方向）

| # | 项目 | 处理 | 偏差方向 |
|---|---|---|---|
| 1 | 开盘成交假设 | 下一交易日开盘价 ± 滑点，无排队/部分成交模型 | 乐观（真实开盘流动性成本更高） |
| 2 | 月频再平衡忽略日内路径 | 只用月末快照 + 次开执行 | 中性 |
| 3 | 闲置现金 0 收益 | 不建模货币基金/利息 | 保守（低估 sleeve 现金月收益） |
| 4 | 月度标记用 RAW 收盘 | 与执行同口径 | 中性 |
| 5 | 股息入账日粒度 | pay date（或 ex-date 回退）封存日历日 | 中性偏保守（入账不早于实际） |
| 6 | 压力滑点 15 bps | 线性放大两边 | 保守 |
| 7 | 佣金固定项对小额单敏感 | SEC98 常量按名义线性+固定 | 与实盘一致 |
| 8 | 2012–2021 单一 regime | 窗口本身即限制（§2.3） | 不可消除，仅登记 |

## 10. 无偷看协议（顺序不可换）

1. 计划、代码与 pin 在**任何结果之前** commit 并 push；
2. `fetch` 只输出覆盖率/错误/配额/ETA，**永不输出 PnL**（该阶段不计算收益）；
3. `import-corporate-actions` 哈希并封存公司行动文件（来源 URL + sha256）；
4. `seal` 封输入 manifest 哈希（文件清单 + sha256、日历、计划哈希、源哈希）；公司行动文件缺失即拒绝；
5. `evaluate` 单次跑完，不流式输出中间结果；`--output` 必填且必须在缓存外；
6. commit 一份小完成回执：plan commit、代码/输入/输出哈希、版本、判定；
7. 缺陷修复保留原失败产物与原因；**结果看过之后任何重跑都不再是第一次 OOS**——重跑必须带 `--rerun-reason`，原输出保留为 superseded 文件。

## 11. 配额与连接安全

- **只用 QuoteContext，永不 TradeContext**；不使用 `BrokerGateway`（它会同时构建 TradeContext）；
- 单 worker，默认 **0.5 req/s**；默认暂停窗 **UTC 周一至周五 13:00–22:00 不抓取**（复用 ORB replay 的 `is_fetch_window_open` / `_Throttle` / 暂停窗口逻辑，按 import 复用而非复制）；
- **301607（配额）或 301604/权限 → 立即全局停止**，落 `status.json` 的 `global_stop` 标记，无重试；后续 fetch/seal 见标记即拒绝。瞬时错误有界重试（默认 3 次）；请求形状拒绝（如 301600 too many query days）干净失败不落 symbol 永久失败；明确的 invalid-symbol 语义按 symbol 持久化永久失败；
- fetch symbol 清单由 `plan` 预先声明（仅 SPY.US、QQQ.US），fetch 不越界；
- 缓存目录 `backend/data/research/spy_monthly_sma10_v1/`（git-ignored）：原子写（tmp + `os.replace`）、逐文件 sha256、`status.json` 状态清单、可断点续传；
- **无任何 DB 访问或写入**。

## 12. 复用声明（import，不复制）

按 ORB replay（`opening_momentum_historical_replay.py`）的既有硬化模式，通过 **import** 复用：`_Throttle`（限速+暂停窗+月界配额记账）、`is_fetch_window_open`、`classify_provider_error`（错误分类含 301600 双语义）、`_RetryableProvider`（有界重试+全局停止传播）、`_page_forward_daily`（日线前向分页）、`_atomic_write_json` / `_atomic_write_bytes` / `_canonical_json_bytes`（原子写）、`_file_sha256`、`_read_gzip_json`、`_require_clean_worktree`（干净树检查）、`_claim_next_attempt` / `_write_attempt_receipt` / `_load_attempt_receipt`（独占 attempt 回执）、`_sealed_plan_digest`（计划摘要）、`_verify_sealed_file`（封存校验）、`_proc_cmdline`。

**新写小助手**（ORB 语义私有、不可复用处，逐项说明）：`_LongportQuoteProvider`（本 replay 仅 DAY/NoAdjust，且不携带 ORB 的分钟周期映射——ORB 适配器钉在其自身 pin 里，跨模块实例化其私有类会混淆两个 pin 的归属）；`_fetch_process_alive`（扫描本模块名）；`_load_status` / `_save_status` / `_symbol_state`（本 replay 的 status schema 只有 daily 一种数据）；`_derive_trading_days`（SPY∩QQQ 两标的交集，ORB 是 QQQ∩DIA）；公司行动装载/校验/封存。**绝不修改 ORB 文件。**

## 13. 判定的授权范围（重申）

`CORROBORATES_RISK_MANAGEMENT_VALUE` 授权的唯一动作是：把「值得继续研究」写进研究优先级记录。`DOES_NOT_CORROBORATE` 授权的唯一动作是：考虑提交书面弃置决定的材料之一。两者都不改动任何前向合同数字、不授权订单、不构成稳定月收益的证据、不触发任何自动晋级。

## 14. 变更流程（唯一合法路径）

1. 在本文档记录书面决定（动机、处置、对已完成结果的影响）；
2. 分配新的 `analysis_id` 后缀并同步 CLI 常量与 pin；
3. **同一提交**更新测试 pin 常量、本哈希清单与决定记录；
4. 旧输入与旧输出保留，不删除、不改写。

绝不允许：先改代码后补文档、只改哈希不写决定、看过结果后调整窗口/统计口径再跑、或把重跑称为第一次 OOS。

### 14.5 结果前决定：注册股息源与 QQQ 降级为 price-only（2026-09-28，任何结果之前）

数据源已由另一条研究线核实并封存：**SSGA `spdr-etf-historical-distributions.xlsx`（完整 sha256 见 §14.6）**。本决定在任何结果产生之前记录，沿用同一 `analysis_id`（不改变任何冻结的分析元素，只落实数据契约 §8 中"来源由另一条研究线决定"的悬置项）：

1. **SPY 股息来源注册。** 该工作簿含 SPY 在 2010–2021 恰好 **48 条**记录（每季一条，各含 ex-date、record date、payable date 与数值化每股金额）。`import-corporate-actions` 现在接受该 xlsx 原始文件：解析仅用 **stdlib `zipfile` + `xml.etree.ElementTree`**（`openpyxl` 不是本仓库依赖，不引入）；表头按**列名**定位（FUND / EX-DATE / RECORD DATE / PAYABLE DATE / AMOUNT per share），不按列位置；日期接受 Excel 序列值（1900 系统）或 ISO 字符串；金额必须数值化且有限为正。存储格式仍是 canonical JSON，完整性锚点是**盘上文件**的 sha256（源文件哈希留作溯源）。
2. **QQQ 降级为 price-only 披露基准。** 该工作簿**没有 QQQ 行**（QQQ 是 Invesco 产品）。决定：QQQ 腿**不获得任何公司行动、不贷记股息**，其收益序列只进描述性 `qqq_differences` 切片，**永不进入任何 claim**。SPY 买入持有仍是唯一的 claim-2/claim-3 基准（§3 不变）。QQQ 日线的第二职责——与 SPY 取交集构成封存交易日历——不变。
3. **拆股完整性屏幕（新增 DATA_BLOCKED 路径）。** 注册源**没有拆股列**，因此"源里没有拆股行"**不是**"窗口内没有拆股"的证据。evaluate 现在用 RAW 相邻收盘价推导隐含因子：`factor = prev_close / close`，任何 `factor ≥ 1.5` 或 `≤ 2/3` 且当日**无**已注册拆股行动的不连续点 → `DATA_BLOCKED`（绝不填补、绝不丢日）。阈值余量：窗口内 SPY 最差单日约 −12%（因子 ~1.14），1.5 留有 >4 倍余量，同时仍能捕获 2:1（2.0）与 3:2（1.5）拆股。该屏幕在统计计算之前运行；触发即返回 DATA_BLOCKED 载荷并列出前 5 个违规点。
4. **无 claim、无窗口、无统计口径改变。** 本决定只落实数据输入路径与一个更严格的 DATA_BLOCKED 屏幕；四个 claim、阈值、bootstrap、样本门、判定映射全部不变，因此**不分配新的 `analysis_id`**。pin 清单新增拆股屏幕常量与函数、xlsx 解析器与 `import-corporate-actions` 的新分支，与本决定同一提交。

### 14.6 结果前决定：注册源的 URL 纠正与完整哈希强制执行（2026-09-28，orchestrator 评审，仍无任何结果）

评审发现两处缺陷（注册 URL 错误；注册哈希只记录不强制）。修复仍发生在任何结果之前（未 fetch、无任何输出），沿用同一 `analysis_id`，仅收紧数据输入：

1. **URL 纠正。** 注册 URL 为 orchestrator 实际验证过的地址（HTTP 200，577,780 字节）：`https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx`。此前注册的 `/us/en/intermediary/etfs/library-content/products/library/` 路径是错的，从代码、本文档（§8 与本节）与 post-commit 命令序列中全部更正。
2. **完整哈希强制执行。** 前/后缀常量删除，替换为**完整** sha256：`51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad46877c54`。xlsx 路径的 `import-corporate-actions` **拒绝任何哈希不符的文件**，错误信息明确指出：SSGA 侧这是一份**活文件**，未来的修订版需要一次 registered change decision（书面决定 + 哈希更新 + pin 同一提交），绝不静默替换。
3. **JSON 路径降级为仅限合成测试。** 决定：保留 JSON 输入路径但标记 `source_format = json-synthetic-test-only`，**seal 对该格式一律拒绝**——真实运行只能用注册 xlsx + 注册哈希。这使封存记录的形状与 seal 复核仍可被合成测试覆盖，同时真实运行在机制上不可能使用未注册的 JSON。
4. **导入时强制注册内容事实。** SPY 必须恰有 **48 条**除息日在 2010-01-01..2021-12-31 的股息事件：**每季恰好一条**（每个日历季度 2010Q1..2021Q4 共 48 季，无重复、无缺失）、每条有 pay date、金额数值化且 > 0（解析器已拒绝非正/非数值金额）。任何偏差（47/49 条、重复季度、窗口外事件、缺 pay date）→ 拒绝，映射为 DATA_BLOCKED。拒绝信息只含**计数与季度**，**永不打印金额**。5. **seal 复核。** seal 对已存储的公司行动记录重新验证三件事：`source_format` 是注册 xlsx、`source_file_sha256` 等于注册完整哈希、48 季内容事实对**存储记录**成立。任一不成立 → DATA_BLOCKED 拒绝。
6. **无分析元素改变。** claim、阈值、bootstrap、样本门、判定映射、窗口全部不变；本决定只收紧输入验证，**不分配新的 `analysis_id`**。pin 清单同步（删 2 个前/后缀常量，增 5 个：URL、完整哈希、窗口起止、48 事件数）。


### 14.7 结果前决定：独立评审 12 项 MUST-FIX（2026-09-28，仍无任何结果）

独立结果前评审对整个包提出 12 项 MUST-FIX。修复发生在任何 fetch、任何 seal、任何 evaluate 之前（未读 .env、未触碰 ORB 文件）。**其中第 1–4 项改变分析语义**（股息权利、清算会计、暖机边界、名义上限口径），因此 **`analysis_id` 升级为 `spy-monthly-sma10-cash-v2`**；其余各项收紧输入验证与执行安全，不改变估计量。逐项登记：

1. **股息权利（改语义）。** 权利在**除息日开盘前**按当时持股固定（除息日开盘买入者无权利；除息日开盘卖出者保留全部权利）。未派股息形成**应收款**：计入 NAV、**不可用于买入**；派息日无论是否仍持仓，应收款转为现金。月末 NAV 含应收款。
2. **最终清算（改语义）。** 清算按时间顺序在其自身会话执行；**窗口结束后不得开新仓**（清算会话上的 BUY 事件被忽略，最后一个月不可能出现 BUY→SELL 往返）。最后一个计分月的月度收益**包含**注册的次开清算：该月月末权益即清算后净现金。**恒等式**：`初始 × ∏(1+r_月) == 最终净清算权益`，对 sleeve 基础/压力与基准逐腿强制，任何失配 → DATA_BLOCKED。
3. **暖机边界（改语义+改文本）。** 2011-12 的信号是**进入第一个计分会话的持仓状态**——非平仓时，第一个计分会话本身在 2011-12 月末的次开执行目标交易。**必需信号缺失**（不足 10 个连续月末）→ DATA_BLOCKED，**绝不静默平仓**。§4.1 中「进入窗口时仓位恒为 FLAT 是规则结构的必然」的旧表述已删除——那是错误的：边界由 2011-12 信号决定。
4. **名义上限（改语义）。** 5,000 上限比较 `股数 × 可执行价`（开盘价加滑点）；佣金是**独立的现金约束**，不占用名义余量。
5. **部分缓存与日历（选 (a)）。** 单边 SPY/QQQ 日期在必需区间内 → DATA_BLOCKED；seal 校验抓取回执的逐 symbol bar 哈希与计数；月末与次开对照**独立封存日历**验证（双 symbol 同时缺的日期由此捕获）。**日历来源选 (a)**：本环境无离线 exchange-calendar 包，注册 `nyse_calendar.py`——按 NYSE 公开规则推导（周末+标准假日+周六前移/周日后移+Meeus 复活节算 Good Friday），外加登记为数据的三次一次性休市（2012-10-29/30 飓风 Sandy、2018-12-05 老布什国葬）。该推导已对 2010–2021 逐年 spot-check（251–253 会话/年，2012 因 Sandy 少 2 天）。"每月仅 1 根 bar"的缓存会被拒。
6. **attempt 前输入验证。** RAW OHLC 有限为正且 `low ≤ min(o,c) ≤ max(o,c) ≤ high`；无重复日期；`pay_date ≥ ex_date`。xlsx 解析器容忍稀疏单元格（None 值）且表头与数据行**都按单元格引用列字母**定位（不再 enumerate）。所有失败在 `_claim_next_attempt` 之前产出显式 DATA_BLOCKED 或拒绝。
7. **拆股（选 splits.json 封存证据）。** 价格屏幕降级为**纯异常探测器**（3:2 拆股+1% 波动即可逃脱，永不作为「无拆股」证明）。注册封存 `data/splits.json`：SPY 与 QQQ 在 2010-01-01..2021-12-31 **均无拆股**（SPY 最后一次 2:1 于 2005-03-11，QQQ 于 2022-08-17，均在窗口外；来源 URL 载于文件）。seal 将其哈希入 manifest；**文件缺失或哈希不符 → DATA_BLOCKED**。
8. **源哈希与 pin（改 pin 方案）。** evaluate 将**当前**源哈希与**封存**哈希逐一比对，失配在 attempt 声明前拒绝；哈希范围含复用的 ORB 模块与 `accounting_fees.py`；evaluate 使用**封存的 status 快照**而非重读 status.json。pin 方案从函数清单改为**全文件 AST 哈希**（纯模块、CLI、日历模块、splits.json、accounting_fees、ORB 模块）。
9. **独占执行。** 全程持有的 `O_CREAT|O_EXCL` `run.lock`（带存活 pid 与 attempt 状态；前任非终态或存活 → 拒绝重跑；陈旧锁需按注册理由处置）；输出发布**绝不覆盖**（碰撞落到版本化兄弟文件）；supersedes 链保留旧输出链接。
10. **fetch 绑定冻结计划。** 首个请求前完整 canonical 计划须等于冻结计划，否则拒绝；绑定计划写入缓存；**续传须呈交同一计划**（digest 相等）；永久失败持久化到 status。新增真实的 plan→fake fetch→seal 连续性测试。期间发现并修复一个真实缺陷：ORB 的 `_page_forward_daily` 游标推进（latest+1 天）在本 replay 的严格 after 语义下**每页边界丢 1 个会话**（复现：2014-05-21、2018-05-11）——本地新写 `_page_forward_daily_local`（游标=latest），**ORB 文件不动**。
11. **逐 claim 退化政策。** 退化检查按**各 claim 自己的输入序列**（rS、rS−rB、下行项、压力 rS）：任一常数 → 该 claim 不能通过，判决 INCONCLUSIVE；`degenerate_series` 记录是哪些 claim。
12. **披露与测试。** 最大回撤改用**复利**权益曲线（两个 −10% 月 → 19%）；30% 预扣敏感性**选择：实际计算**（同 sleeve 在 30% 预扣下的完整重放，仅披露）；`/proc` 抓取检查在测试中以本地 fixture 中和；三个旧测试改为断言真实函数数值输出（仓位与权益），不再做 `random.Random` 相等性检查。


### 14.8 结果前决定：封存日历与拆股证据的两处准确性修正（2026-09-28，仍无任何结果）

对所有者复核发现的两个 pin 文件缺陷的修复。**两处均在 SMA10 窗口（2010-01..2022-01）之外，窗口内推导逐日不变（2010–2021 仍为 3021 个会话），`analysis_id` 维持 v2。**

1. **`nyse_calendar.py` 补 Juneteenth 与卡特国葬。** 复核者用 ORB replay 封存的**真实供应商日历**（2023-09..2026-04）核对本推导，发现两处遗漏：
   - **Juneteenth**（6 月 19 日，Sat→Fri / Sun→Mon 顺延）自 **2022 年**起为 NYSE 假日（首次观测 2022-06-20，周一）。此前版本完全缺失——2024-06-19 与 2025-06-19 被错误推导为开市日。已加 `JUNETEENTH_FIRST_YEAR = 2022` 与规则分支。
   - **2025-01-09**（卡特总统全国哀悼日，NYSE 全市场休市）缺失。已加入 `ONE_OFF_CLOSURES`，并补注来源。
   - 同时审计 2010–2026 全部一次性全市场休市：仅 Sandy（2012-10-29/30）、老布什（2018-12-05）、卡特（2025-01-09）三次（9/11 在 2001；里根 2004-06-11 与福特 2007-01-02 的有限交易时段不在此范围且在 2010 前）。来源：NYSE 官网 hours-calendars/新闻稿 + 封存 ORB 供应商日历交叉验证。
   - 新测试：`expected_nyse_sessions(2023-09-01, 2026-04-30)` 对照**硬编码在测试内**的供应商已验证休市清单（含 2024/2025 Juneteenth 与 2025-01-09，不读宿主文件）；2022-06-20（首个观测 Juneteenth）专项测试。
2. **`splits.json` 来源重标。** `source_urls` 误列了 SSGA distributions 工作簿的**旧错误 URL**；且该工作簿**没有拆股列**，根本不是拆股证据。已改为：
   - SSGA 工作簿（**正确 URL** `https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx`）仅在 `dividend_source_url` 键下作为**股息来源**登记，并显式注明"无拆股列、非拆股证据"；
   - `source_urls` 只保留真正的拆股历史来源（stockanalysis.com 的 SPY/QQQ splits 页与 Invesco 官网）；
   - 全仓 `grep -rn "etfs/library-content/products/library" backend/` 复核：仅剩两处合法出现——本文档 §14.6 的**历史纠正记录**（记录旧路径是错的这一事实本身）与测试中的**标记字符串**。
3. **pin 同步。** 两文件均在全文件 pin 清单内，哈希与组合值随本决定同一提交更新。窗口内语义零变化。


### 14.9 结果前决定：最终结果前评审 6 项 MUST-FIX（2026-09-29，仍无任何结果）

最终结果前评审 NO-GO 的 6 项修复。**第 1、3、4 项改变窗口内计算路径（窗口选择、期末应收、日历校验范围），但估计量定义、claim、阈值、统计口径与判定映射零变化，`analysis_id` 维持 v2。** 全程合成数据；未 fetch、未 commit、未读 .env、ORB 文件零改动。

1. **真实工作簿被拒 → 改为选择。** 原实现对窗口外事件直接拒绝，而真实 SSGA 工作簿含数十年 SPY 行。修正顺序：先验完整 sha256 → 解析 → **选择**除息日 ∈ 2010-01-01..2021-12-31（恰 48 条，**含 2010–2011 暖机股息，不是只留 2012+**）→ 季度完整性检查。存储记录携带 `raw_spy_row_count` 与 `selection_range`；报告只含计数，永不打印金额。**首根封存 bar 之前的股息不更新 T 指数**（否则会伪造一次水位跳变）。
2. **源哈希真正强制。** `manifest["source_hashes"]` 改为**必需 dict**（不再摊平到顶层——摊平导致 evaluate 的 `manifest.get("source_hashes", {})` 永远为空、零次比较运行）。evaluate 在 attempt 声明前要求**键集与值完全一致**，缺失即拒。删除 evaluate 前对现行 `status.json` 的重读，一切以**封存快照**为准（新 `_sealed_preflight`）。
3. **期末应收。** 原期末权益只取 `state.cash`，丢弃未结应收，恒等式只比现金。修正：期末权益 = 清算现金 + **仍在途的应收**（pay date 落在封存数据之后），两者**分别披露**；恒等式应用于 base、stress、基准 **与预扣腿**（四腿全量）。
4. **日历与校验。** 独立日历比对改为**完整注册区间 `DATA_START..DATA_END` 精确比对**（不再裁剪到缓存自身 min/max）；头部/尾部截断阻断。暖机要求从**首个独立日历会话**起（非 `min(sealed)`）；末端为最后计分月末的**下一个封存会话**（非"月末+10 天"）。seal 与 evaluate 双时点对 **SPY 与 QQQ 双标的**校验 OHLC/重复日期/结构（错误信息含标的名），公司行动日期一并校验。逐 symbol bar 哈希与计数为**必需**（缺失即拒，无 `is not None` 守卫）。
5. **attempt 链。** (a) 发布或回执写入失败 → attempt 记为 FAILED（含 `OSError` 等一切异常）；(b) 重跑前检查**持久化的非终态 attempt**（STARTED 阻断）；(c) `supersedes` 取自**已保存的 `previous_attempt`**（原先读新写的 STARTED 回执，恒为 null）；(d) **reseal 归档旧 manifest**（`manifest_archive/`，原先只归档回执）。发布与回执写入改为**严格 no-clobber 原子操作**（temp + `os.link`，目标已存在即失败，再删 temp）——输出碰撞现在**直接拒绝**（不再静默落版本化兄弟文件）。死 PID 锁策略不变：人工按注册理由处置，绝不自动删除。
6. **主机无关性。** 缺公司行动文件的 seal 拒绝测试改为经 `_no_live_fetch` 本地 fixture 中和真实 `/proc` 扫描；不再硬编码 PID 999999 假死进程。

**Pin 口径说明（评审要求）：** pin 哈希必须用项目 Python **3.11**（`.venv/bin/python`，3.11.15，与 CI 和 Docker 一致）计算。宿主 `python3` 是 3.14，`ast.dump` 输出不同，会给出不同的 AST 哈希。


### 14.10 结果前决定：delta 评审 3 项 MUST-FIX（2026-09-29，仍无任何结果）

对 14.9 六项修复的 delta 评审确认大部分，留下 3 项。**全部为执行安全与验证时序修复，估计量定义、claim、阈值、统计口径与判定映射零变化，`analysis_id` 维持 v2。**

1. **回执写入失败未记 FAILED。** 14.9 的保护只覆盖结果发布；哈希计算、回执发布与完成记账在保护之外。修正：**整个发布/回执阶段**包入保护——任一点失败（结果发布、哈希、回执发布、记账）都将 attempt 记为 FAILED，**连同已存在的结果路径与哈希**（部分发布仍有可审计痕迹），再传播异常；run lock 由外层 finally 释放。测试：结果发布成功、回执发布抛错 → attempt FAILED 且记录了结果路径/哈希、无残留锁。
2. **attempt 前数据再验证缺位。** seal 校验双标的，但 evaluate 在 claim 之后才解析校验、且只查 SPY。修正：**双标的**（SPY 与 QQQ）的结构、OHLC、重复日期与公司行动日期检查移入 `_sealed_preflight`，全部在 claim 之前运行；结构性错误（含畸形行，如非日期首列）成为**显式拒绝且零 attempt 声明**（无 `attempts/` 条目）。seal 与 evaluate 各自独立证明——即便两者间的字节被（测试中模拟的）连同 manifest 一起篡改，evaluate 仍拒绝。测试：篡改已封存 QQQ 文件且哈希同步重写（坏 OHLC、畸形行）、SPY 重复日期 → 拒绝且无 attempts/。
3. **no-clobber 发布使用固定临时名。** `.name.publish.tmp` 是共享路径：并发写者可截断一个已 hard-link 到已发布结果的 inode。修正：`tempfile.mkstemp(dir=目标目录, prefix=…)`（**独占创建**、唯一名）→ 写入 → `fsync` → `os.link` 到目标（已存在即失败，绝不改写）→ 删除临时文件；结果与回执两条路径同用此法。测试：两次发布同一目标，第二次被拒且**首次结果字节不变**；两并发发布用**不同**临时路径且无残留；源码检查钉死 `mkstemp`/`fsync` 且固定名模式不回归。

**Pin 口径**（沿 §14.9）：全部用 `.venv/bin/python`（3.11.15，与 CI/Docker 一致）计算；宿主 `python3` 为 3.14，`ast.dump` 输出不同。

## 15. Code manifest（pin 清单）

以下清单与 `backend/tests/test_spy_monthly_sma10_preregistration.py` 的 `_MANIFEST` 逐项一致（doc-agreement 断言双向同步）。哈希为 `ast.dump`（无属性）SHA-256，冻结于 2026-09-28。

```
app.cli.spy_monthly_sma10_replay = bbd2b2e06d20740c65061212be2eea7d216fdde22664f235a7affb097830cdd1
app.domain.monthly_trend.data.splits.json = 6e0de8ae5173afb22634e2577b90151974a4a1b4136b2ebde347abe733fb165d
app.domain.monthly_trend.nyse_calendar = 828dda83588b0731c07afcbd7f380d1abe7cce024bd467b7ce19c8057bb977f5
app.domain.monthly_trend.sma10 = c9127d6015e11422c319b3bac93969e356b30706f70f6c09a9aef35e829f5073
dep.app.cli.opening_momentum_historical_replay = ea90fb33573c2aacdd62be0601730bfed9fe4849a4947bf45ead946f4ddb6581
dep.app.core.accounting_fees = 2f33e6d3ddf3f6c657a14befd361c1bc2e07c73db829db52db1f8f0bc0503a1b
combined = f56194f783b8ffeaea7bda811db3d5eaed06978420d04bcae0b810ae680c84ff
```


**禁止为消红而改哈希。** 任何 pin 失配都要求：先在本文档记录书面决定（动机、处置、对已完成结果的影响），再在**同一提交**里更新哈希与本文档；在结果已存在之后改哈希使既有结果作废，且新的运行**不得**称为第一次 OOS。
