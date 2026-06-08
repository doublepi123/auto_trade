# P41 交易报告与增强复盘设计

## 背景

当前工作区已有一组未提交的 Reports 草稿文件：后端 `reports` API、`ReportService`、前端 `Reports.vue`、`frontend/src/api/reports.ts` 和 Cypress spec。但功能尚未完整接入：后端 router、schema、前端 route/nav/type 等仍有缺口。下一步迭代选择在现有草稿基础上完成并增强交易报告能力。

## 目标

本迭代采用分阶段交付，先建立可信报表基础，再逐步增强交易归因与每日明细 drill-down。

### Phase 1：可信报表基础

- 接通当前 Reports 功能：后端 router、schema、前端 route、导航、类型。
- 修正基础统计：总 PnL、交易次数、胜率、均笔盈亏、最大单笔盈利、最大单笔亏损、盈亏比。
- 增加收益/风险曲线基础数据：每日 PnL、累计 PnL、最大回撤。
- 支持 JSON/CSV 导出。
- 补后端服务/API 测试、前端类型检查、Cypress。

### Phase 2：交易归因

- 按交易方向/动作拆分：`BUY`、`SELL`、`SELL_SHORT`、`BUY_TO_COVER`。
- 按 LLM 相关 vs 非 LLM 相关拆分：基于 `LLMInteraction.order_id` 与 `OrderRecord.broker_order_id` 关联。
- 输出归因摘要：交易数、PnL、胜率、占比。
- 前端增加归因卡片或表格。

### Phase 3：每日 drill-down 明细

- 每个交易日可展开订单明细。
- 展示订单 ID、side、数量、成交价、状态、成交时间、估算/实际 PnL。
- 导出 CSV 增加明细模式。
- Cypress 覆盖展开、空状态和导出按钮。

## 非目标

- 不新增数据库表；优先基于现有 `orders`、`llm_interactions`、`trade_events`、`DailyPnlService`。
- 不做复杂组合回测。
- 不做税务或手续费精算。
- 不接入外部 BI。
- 不做定时报表邮件或通知推送。

## API 设计

### 主查询接口

`GET /api/reports/range`

参数：

- `symbol`: 股票代码，例如 `AAPL.US`。
- `from_date`: 开始日期，格式 `YYYY-MM-DD`。
- `to_date`: 结束日期，格式 `YYYY-MM-DD`。

返回完整报表数据：指标、每日曲线、归因、明细。Phase 1 中归因和明细可为空数组。

### 快捷接口

保留以下端点作为薄包装：

- `GET /api/reports/daily`
- `GET /api/reports/weekly`
- `GET /api/reports/monthly`
- `GET /api/reports/export`

`export` 支持 `format=json|csv`。非法格式返回 400。

## 数据口径

- 时间范围：`from_date` 到 `to_date`，包含首尾日期。
- 交易日：Phase 1 沿用 `DailyPnlService.calculate(trade_day=..., symbol=...)`，避免重新实现 PnL；如需更严格市场日边界，后续单独增强。
- 总 PnL：每日 realized PnL 汇总。
- 交易次数：以 `DailyPnlService` 返回的已实现交易条目为准，不直接数订单，避免 BUY/SELL 配对误差。
- 胜率：盈利交易数 / 总交易数。
- 盈亏比：平均盈利 / 平均亏损绝对值；无亏损时返回 `0.0`。
- 最大单笔盈利/亏损：从 realized trade PnL 中取 max/min；无交易时均为 `0.0`。
- 累计收益曲线：按日期累加每日 PnL。
- 最大回撤：基于累计收益曲线计算 peak-to-trough drawdown。

## LLM 指标口径

- 建议次数：范围内 `LLMInteraction` 数量。
- 采纳次数：`applied == true`。
- 采纳率：采纳次数 / 建议次数。
- 盈利采纳次数：当前 `LLMInteraction` 模型不依赖 `was_profitable` 持久字段；Phase 1 不新增数据库列，因此该值返回 `0`。
- LLM 准确率：Phase 1 返回 `0.0`；Phase 2 通过 `LLMInteraction.order_id` 与 `OrderRecord.broker_order_id` 关联后计算盈利采纳次数 / 可归因采纳次数。

## 响应结构

`ReportResponse` 包含：

- `period_type`
- `symbol`
- `start_date`
- `end_date`
- `metrics`
- `daily_points`
- `attribution`
- `details`

`metrics` 包含：

- `total_pnl`
- `total_trades`
- `win_count`
- `loss_count`
- `win_rate`
- `profit_loss_ratio`
- `avg_pnl_per_trade`
- `max_profit`
- `max_loss`
- `max_drawdown`
- `llm_suggestions_count`
- `llm_applied_count`
- `llm_apply_rate`
- `llm_profitable_count`
- `llm_accuracy_rate`

`daily_points` 每项包含：

- `date`
- `pnl`
- `cumulative_pnl`
- `drawdown`
- `trade_count`
- `win_count`

`attribution` 每项包含：

- `key`
- `label`
- `trade_count`
- `pnl`
- `win_rate`
- `share`

`details` 每项包含：

- `date`
- `orders`

`orders` 每项包含：

- `broker_order_id`
- `side`
- `quantity`
- `executed_price`
- `status`
- `filled_at`
- `pnl`

Phase 1 可返回空 `attribution` 和空 `details`，以便前端类型稳定。

## 前端设计

### 页面入口

- 路由：`#/reports`
- 导航菜单：新增“交易报告”入口。
- 页面标题：`交易报告`。
- 副标题：`按时间段汇总交易表现、风险曲线与 LLM 建议效果`。

### 筛选区

- 股票代码。
- 开始日期。
- 结束日期。
- 查询按钮。
- 导出 JSON。
- 导出 CSV。

增强行为：

- `to_date` 默认今天。
- `from_date` 默认近 30 天。
- 前端校验 symbol、from、to 必填。
- 前端校验 `from_date <= to_date`。
- 查询失败显示 `ElMessage.error`。
- 无数据时显示清晰空状态。

### 指标卡片

第一行核心指标：

- 总盈亏。
- 总交易次数。
- 胜率。
- 均笔盈亏。

第二行风险/LLM 指标：

- 最大单笔盈利。
- 最大单笔亏损。
- 最大回撤。
- LLM 采纳率 / 准确率。

### 图表

遵循项目“不引入图表库”的约束，继续使用纯 SVG。

Phase 1：

- 每日 PnL 柱状图。
- 累计 PnL 折线。
- 最大回撤先作为指标卡展示。

Phase 2：

- 归因表格优先，不做复杂饼图。

Phase 3：

- 每日明细使用 `el-table` 展开行。

### 表格

Phase 1 每日汇总表：

- 日期。
- 当日 PnL。
- 累计 PnL。
- 回撤。
- 交易次数。
- 盈利次数。
- 日胜率。

Phase 2 归因摘要表：

- 归因类型。
- 交易数。
- PnL。
- 胜率。
- 占比。

Phase 3 每日明细表：

- 订单 ID。
- side。
- 数量。
- 成交价。
- 状态。
- 成交时间。
- PnL。

## 测试设计

### 后端测试

新增或补充 `backend/tests/test_report_service.py`：

- 空数据范围返回 0 指标和空曲线。
- 多日 PnL 汇总正确。
- 累计 PnL 正确。
- 最大回撤正确。
- 最大单笔盈利/亏损正确。
- 盈亏比正确。
- LLM 建议/采纳/准确率统计正确。
- range 首尾日期包含正确。

新增或补充 `backend/tests/test_reports_api.py`：

- `/api/reports/range` 返回 schema 正确。
- `daily`、`weekly`、`monthly` 快捷端点参数校验。
- `export?format=json`。
- `export?format=csv`。
- 非法日期或非法 format 返回 400。

### 前端测试

- 补齐 `ReportResponse`、`ReportMetrics`、`ReportDailyPoint` 类型。
- `npm run type-check` 通过。
- Cypress 覆盖：
  - 导航到 `#/reports`。
  - 空数据状态。
  - 指标卡片渲染。
  - 每日 PnL / 累计 PnL 图表存在。
  - 日期必填与范围校验。
  - JSON/CSV 导出按钮触发请求。

## 验收命令

Phase 1 完成时至少运行：

```bash
cd backend && python3 -m pytest tests/test_report_service.py tests/test_reports_api.py -v
cd frontend && npm run type-check
cd frontend && npm run cypress:run -- --spec cypress/e2e/reports.cy.ts
```

如改动触及共享类型、API 注册或生产构建，追加：

```bash
cd backend && python3 -m basedpyright
cd frontend && npm run build
```

## 验收标准

- Reports 页面可从导航进入。
- 输入 symbol 和日期范围后可展示报表。
- 核心指标、每日曲线、导出功能可用。
- 无数据、错误、非法日期都有明确反馈。
- Phase 2/3 字段在 schema 上预留，不阻塞 Phase 1。
