# 后续迭代路线图 (2026-05-31) — 已演进至 2026-06-01

> **⚠️ 文档状态：已被主 [Roadmap.md](../../Roadmap.md) 取代。** 本文保留作为 2026-05-31 当时的规划快照；P13~P18 全部已在 5/31~6/1 期间交付，实际交付细节以主 Roadmap.md 为准。
>
> **原始基线（2026-05-31 写作时）：** `pytest 621 passed`，`basedpyright` 0/0，17 个 Cypress specs，P1~P12 全部交付。
>
> **当前基线（2026-06-01 整理时）：** `pytest 715 passed`，`basedpyright` 0/0，Cypress 80+ specs，P1~P22 全部交付。
>
> **下一建议（与主 Roadmap.md 一致）：** P23 前端实时通知中心（2~3 天）+ P24 多标的自动交易评估（需单独立项）。

---

## 迭代优先级排序（原始规划）

按 **交易价值 > 研究能力 > UX > 技术债** 的原则排列。下表保留原始排序，附加实际交付记录供追溯：

| 顺序 | 代号 | 主题 | 依赖 | 价值说明（原始） | 预估 | 实际交付 |
|------|------|------|------|----------|------|----------|
| **1** | **P13** | 加仓 + 成本锚定 LLM | 无 | 核心交易能力：允许持仓中追加买入，LLM 获得真实持仓上下文 | 2~3 天 | ✅ 2026-05-31（commit `a9e3ce5` + `d63ddd3` + `c14a9f9` + `89c65ef`） |
| **2** | **P14** | 保证金下单量 (Buying Power Order Sizing) | 无 | 核心交易能力：从券商获取真实保证金购买力，替代现金估算 | 1~2 天 | ✅ 2026-05-31（commit `0780f8a` + `77112c7` + `f1600db`），实施为 `margin_safety_factor` 配置化方案 |
| **3** | **P15** | Dashboard & 配置性能优化 | 无 | UX：页面加载从秒级降到亚秒级，减少券商 API 调用 | 1~2 天 | ✅ 2026-05-31（commit `e6279c1`） |
| **4** | **P16** | 策略实验与验证平台 (Phase 1: 批量回测 + 排行榜) | 无 | 研究能力：参数网格批量回测 + 排行榜比较 | 2~3 天 | ✅ 2026-05-31（commit `8168da2`，与 P17 合并提交） |
| **5** | **P17** | 策略实验与验证平台 (Phase 2: LLM 评分 + 导出) | P16 | 研究能力：LLM 建议事后评分 + CSV/JSON 导出 + Strategy 草稿带回 | 2 天 | ✅ 2026-05-31（`llm_recommendation_evaluator.py` 在 commit `084c0aa` 首次 commit；export 端点 + draft 带回随 `8168da2` 提交） |
| **6** | **P18** | 技术债清理 | 无 | 可靠性：P8' 类型清理、P5' SDK disconnect 回调、测试加固 | 1 天 | ✅ 2026-06-01（commit `084c0aa`：`basedpyright` 全量清零；P5' SDK disconnect / 测试加固仍开放） |

### 排序理由（历史记录）

> 以下排序理由为 2026-05-31 写作时的思考过程；P13~P18 已在 5/31~6/1 期间完成交付。

1. **P13 加仓** — 当前引擎是单仓位状态机，持仓时不能加仓。这是交易能力的直接提升，且 LLM 已经有了持仓上下文字段但没有真实数据。两者一起做，收益最大化。
2. **P14 保证金下单量** — 当前用 `available_cash * 0.98` 估算下单量，严重低估保证金账户的购买力。长桥 SDK 已有 `estimate_max_purchase_quantity`，只需包装调用。
3. **P15 Dashboard 性能** — 已有完整设计文档，属于纯 UX 优化，不影响交易逻辑，风险低。
4. **P16/P17 实验平台** — 研究方向，P9~P12 已建立丰富的数据基础，现在需要收敛为可比较、可导出的实验工具。分两阶段降低单次交付风险。
5. **P18 技术债** — 持续改善但非阻塞。

### 显式暂不做（5/31 决策，与 2026-06-02 整理后保持一致）

> 注：LLM Intelligent Interval 6/2 重写版的处理决定已统一移至本文"下一建议"段，此处不再重复。

- **多标的自动交易** — 超出当前架构范围。
- **节假日历** — 显式 YAGNI。
- **API 鉴权收紧 (P2)** — owner 决策不实施。
- **PWA 离线支持** — 移动端适配已完成基础版（P6），PWA 属于锦上添花。

---

## P13: 加仓 + 成本锚定 LLM

> **状态：✅ 已交付 2026-05-31**（commit `a9e3ce5` + `d63ddd3` + `c14a9f9` + `89c65ef` + `a60ae0d`，合并入 `f1600db`）
>
> 以下为原始规划细节，保留作为历史参考。

> **规格文档：** `docs/superpowers/specs/2026-05-29-add-on-buy-cost-anchored-llm-design.md`
> **预估工时：** 2~3 天
> **前置条件：** 无新依赖，基于现有引擎和 LLM 架构

### 目标

1. 允许 LONG 状态下 `price <= buy_low` 触发追加买入（加仓），状态保持 LONG
2. LLM prompt 注入真实持仓数据（数量、平均成本、浮盈%）
3. LLM 区间建议改为成本锚定（不再仅跟随当前价格）
4. 加仓与风控、费用门槛、冷却机制完整集成

### 范围

**引擎层（engine.py）：**
- `LONG` 状态增加 `price <= buy_low` → `BUY` + 保持 `LONG`（加仓）
- `SELL` 优先判断（`price >= sell_high`）不变
- 60s 冷却对加仓同样生效

**执行层（trade_execution_service.py）：**
- 加仓复用 `_entry_quantity_from_margin_power` 计算下单量（若 P14 已交付则用保证金接口，否则用现有逻辑）
- 加仓后 `_record_entry_price` 更新加权平均成本（已有逻辑）
- 加仓触发风控检查（日亏损、连损、kill switch、费用门槛）

**LLM 层（llm_advisor_service.py + context_module.py）：**
- `analyze()` 内部从 `tracked_entries` 获取真实持仓数据
- `ContextModule.render()` 输出持仓成本区块
- `SystemModule` 增加成本锚定引导规则（LONG 时 buy_low 考虑成本、sell_high 不低于成本）

**API 层：**
- 无新端点，复用现有 `/api/strategy/llm-interval/analyze` 和 `/api/strategy/llm-interval/preview`

**前端：**
- Dashboard 最近动作增加 `ADD_ON_BUY` 标签（可选，或复用 `BUY` 标签）
- Strategy LLM 卡片展示真实持仓上下文

### 测试

- `test_engine.py` 补 LONG 状态加仓触发、SELL 优先级、冷却
- `test_trade_execution_service.py` 补加仓 → tracked_entries 更新
- `test_llm_advisor.py` 补 analyze 注入真实持仓数据
- `test_interval_application.py` 补成本锚定规则

### 验证

- [ ] `pytest` 全绿，新增 ≥20 项
- [ ] `basedpyright` 0/0
- [ ] `npm run type-check` + `build` 通过

---

## P14: 保证金下单量 (Buying Power Order Sizing)

> **状态：✅ 已交付 2026-05-31**（commit `0780f8a` + `77112c7`，合并入 `f1600db`）
>
> 实际实现路径为 `margin_safety_factor` 配置化方案（在 `_entry_quantity_from_margin_power` 上叠加可配置安全系数 0~1），与本节描述的"包装 SDK `estimate_max_purchase_quantity`"一致 —— plan 的 "Current State Analysis" 已正确说明 wrapper 已存在。
>
> 以下为原始规划细节，保留作为历史参考。

> **规格文档：** `docs/superpowers/specs/2026-05-20-buying-power-order-sizing-design.md`
> **预估工时：** 1~2 天

### 目标

用长桥 SDK `estimate_max_purchase_quantity` 替代现金估算，保证金账户下单量更准确。

### 范围

**Broker 层（broker.py）：**
- 新增 `estimate_max_purchase_quantity(symbol, side, price, currency)` → `Decimal`
- 封装 `TradeContext.estimate_max_purchase_quantity()`

**执行层（trade_execution_service.py）：**
- `BUY` / `SELL_SHORT` 改用 `margin_max_qty * 0.9` 下单
- `SELL` / `BUY_TO_COVER` 保持使用实际持仓量（不变）
- 估算为零时跳过下单并记录

**配置层（config.py）：**
- 新增 `AUTO_TRADE_MARGIN_SAFETY_FACTOR`（默认 0.9）

### 测试

- `test_broker.py` 补保证金估算解析
- `test_trade_execution_service.py` 补 BUY/SELL_SHORT 用保证金、SELL 不变、零估算跳过

### 验证

- [ ] `pytest` 全绿，新增 ≥10 项
- [ ] `basedpyright` 0/0

---

## P15: Dashboard & 配置性能优化

> **状态：✅ 已交付 2026-05-31**（commit `e6279c1`）
>
> 以下为原始规划细节，保留作为历史参考。

> **规格文档：** `docs/superpowers/specs/2026-05-20-dashboard-config-performance-design.md`
> **预估工时：** 1~2 天

### 目标

Dashboard 页面加载从秒级降到亚秒级，减少不必要的券商 API 调用。

### 范围

**前端：**
- 页面级 loading overlay → 分区骨架屏
- `useDashboardData.load()` 拆分为独立并行请求
- polling / account refresh 增加 request-in-flight guard
- Strategy / Credentials 页面增加渐进加载状态

**后端：**
- `GET /api/account` 批量 quote（P6' 已部分实现，确认完整）
- `GET /api/account` 增加轻量模式（`?lite=1`）仅返回摘要
- `PUT /api/strategy` / `PUT /api/credentials` save → 立即返回 → 后台 reload runner

### 测试

- `test_trade_api.py` 补轻量模式
- Cypress 补骨架屏渲染 + 并行加载验证

### 验证

- [ ] Dashboard 首次可交互时间 < 1s（mock broker）
- [ ] `npm run type-check` + `build` 通过

---

## P16: 策略实验平台 Phase 1 — 批量回测 + 排行榜

> **状态：✅ 已交付 2026-05-31**（commit `8168da2`，与 P17 合并提交）
>
> 以下为原始规划细节，保留作为历史参考。

> **规格文档：** `docs/superpowers/specs/2026-05-29-strategy-experiment-validation-platform-design.md`
> **预估工时：** 2~3 天

### 目标

在同一份历史数据上批量运行多组参数，排行榜展示结果，支持排序和分页。

### 范围

**后端核心：**
- `ExperimentGridService`：参数网格生成器（固定值/列表/范围，组合数上限 1000）
- `StrategyExperimentService`：实验 CRUD + 批量执行 + 排序分页
- 新表：`strategy_experiments` / `strategy_experiment_runs`
- API：`POST /api/experiments` / `GET /api/experiments/{id}` / `POST /api/experiments/{id}/run` / `GET /api/experiments/{id}/runs`

**前端：**
- Experiments 页面：创建实验表单 + 运行进度 + 排行榜表格
- 排行榜支持按总收益/最大回撤/胜率排序

### 测试

- `test_experiment_grid_service.py`、`test_strategy_experiment_service.py`、`test_strategy_experiments_api.py`
- Cypress `experiments.cy.ts`

---

## P17: 策略实验平台 Phase 2 — LLM 评分 + 导出

> **状态：✅ 已交付 2026-05-31**（`llm_recommendation_evaluator.py` 在 commit `084c0aa` 首次 commit；export 端点 + draft 带回随 `8168da2` 提交）
>
> 注意：P17 的 LLM 评分代码与 P18 `basedpyright` 清理在同一 commit (`084c0aa`) 中提交，commit message 简称 P18。这是历史合并交付，不再拆分。
>
> 以下为原始规划细节，保留作为历史参考。

> **依赖：** P16
> **预估工时：** 2 天

### 范围

- `LLMRecommendationEvaluator`：6 类评分标签（EFFECTIVE/INEFFECTIVE/TOO_EARLY/TOO_LATE/RISKY/INSUFFICIENT_DATA）
- 实验结果 CSV/JSON 导出
- 最佳参数带回 Strategy 草稿（预填不保存）
- 前端：LLM 评分页面 + 导出按钮 + 带回操作

---

## P18: 技术债清理

> **状态：✅ 部分交付 2026-06-01**（commit `084c0aa`：`basedpyright` 全量清零）
>
> P5' SDK disconnect 回调、测试加固（时区边界等）仍开放，未来若需要可单独立项。
>
> 以下为原始规划细节，保留作为历史参考。

> **预估工时：** 1 天

### 范围

- `basedpyright` 全量清零确认（3 处 coerce → 显式 isinstance）
- P5' SDK disconnect 回调接入
- 测试偶现 flaky 修复（时区边界等）
- 死代码最终清理

---

## 执行节奏建议

| 周 | 迭代 | 说明 |
|----|------|------|
| W1 (6/2~6/6) | P13 加仓 + 成本锚定 LLM | 核心交易能力，最高优先级 |
| W1 (6/2~6/6) | P14 保证金下单量 | 可与 P13 部分并行（Broker 层独立） |
| W2 (6/9~6/13) | P15 Dashboard 性能 | UX 优化，低风险 |
| W2 (6/9~6/13) | P18 技术债清理 | 随时可以插入 |
| W3 (6/16~6/20) | P16 实验平台 Phase 1 | 研究能力 |
| W4 (6/23~6/27) | P17 实验平台 Phase 2 | 依赖 P16 |

### 实际执行节奏

实际交付集中在 2026-05-31 单日完成，节奏比规划更紧凑：

- **2026-05-31 上午**：P14 保证金下单量安全系数配置化（`0780f8a`、`77112c7`）
- **2026-05-31 下午**：P13 加仓 + 成本锚定 LLM（`a9e3ce5`、`d63ddd3`、`c14a9f9`、`89c65ef`、`a60ae0d`、`f1600db`）
- **2026-05-31 晚间**：P15 Dashboard 性能（`e6279c1`）+ P16/P17 策略实验平台两个 Phase 合并提交（`8168da2`）
- **2026-05-31 晚间**：P19 A/B prompt variant 集成（`f183131`）
- **2026-06-01 上午**：P18 `basedpyright` 清零（`084c0aa`，与 P17 评估器代码一起提交）
- **2026-06-01**：P20 Sharpe/Profit Factor（`0081dde`）
- **2026-06-01**：P21 CI 质量门禁 + P22 LLM 波动率触发（`02ff712`）

---

## 演进记录（5/31 路线图后未在原规划中的新增迭代）

| 代号 | 主题 | 实际交付 | commit |
|------|------|----------|--------|
| **P19** | A/B Testing 集成：LLM Prompt 变体实验 | 2026-05-31 | `f183131` |
| **P20** | 策略实验平台扩展指标：Sharpe / Profit Factor / 盈亏比 | 2026-06-01 | `0081dde` |
| **P21** | CI 质量门禁：测试/type-check 阻断坏提交 | 2026-06-01 | `02ff712` |
| **P22** | LLM 波动率触发补全 | 2026-06-01 | `02ff712` |

---

## 下一建议（与主 Roadmap.md 一致）

> **P23：前端实时通知中心（建议优先）**
>
> 价值：Dashboard 通过 WebSocket/轮询实时接收风控/跳过/审计事件，解决当前必须刷新才能看到的问题。预估 2~3 天。
>
> **P24：多标的自动交易扩展（评估）**
>
> 价值：Watchlist 现有观察能力扩展为单标的自动交易轮换。架构风险高，需单独立项评估。

**显式暂不做（与 5/31 决策保持一致）：**
- LLM Intelligent Interval 6/2 重写版 — 核心 LLM 区间能力已通过 P0/P9/P11 交付；新设计的触发机制（波动率触发、定时触发）和当前 cron 已重叠，**暂不推进**。相关 spec+plan 保留作为设计探索性文档。
- 多标的自动交易 — 超出当前架构范围。
- 节假日历 — 显式 YAGNI。
- API 鉴权收紧 (P2) — owner 决策不实施。
- PWA 离线支持 — 移动端适配已完成基础版（P6），PWA 属于锦上添花。

---

## 交付标准（所有迭代通用）

1. 后端 `pytest` 新增测试全部通过，无回归
2. `basedpyright` 0 errors / 0 warnings
3. `npm run type-check` + `npm run build` 通过
4. 新增 Cypress E2E 覆盖主流程（如适用）
5. 更新 Roadmap.md 标记完成状态
