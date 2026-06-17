# P69-P73 Reports 只读增强（5 轮自主迭代）

> 用户指令："自主再进行5轮迭代"。本批继续在 `feature/p64-p68-trade-analytics-frontend` 分支上推进，选择低风险 Reports 前端增强线。

## 目标

把现有 Reports 页面从「基础指标 + 每日表」增强为更可用的复盘入口：快捷区间、归因、每日订单 drill-down、报告洞察摘要、导出/空状态 polish。

## 选型

本批选择 Reports 前端只读增强，而不是多标的 runner/risk 修复：

- `GET /api/reports/range` 已返回 `metrics`、`daily_points`、`attribution`、`details`。
- 前端 `Reports.vue` 尚未充分呈现 `attribution` 和 `details`。
- 本批不改后端、不新增表、不触碰 broker/order path；所有新增能力均从既有响应派生。

## 5 轮范围

| 代号 | 主题 | 交付 |
|---|---|---|
| P69 | 快捷区间 | Reports 过滤区增加 7/30/90 天快捷按钮，一键设定日期并查询 |
| P70 | 归因表 | 展示 `reportData.attribution`：标签、交易数、PnL、胜率、占比 |
| P71 | 每日 drill-down | 每日明细表可展开 `reportData.details` 中的订单行 |
| P72 | 报告洞察摘要 | 从 `daily_points` 派生最佳日、最差日、盈利日数、亏损日数、最大回撤日 |
| P73 | 导出/空状态 polish | 显示当前查询范围、导出文件名预览、无归因/无明细的明确说明 |

## UI 设计

- 保持 Reports 现有页面和路由不变。
- 快捷按钮放在过滤表单中，减少重复选择日期。
- 归因表放在趋势图下方、每日明细上方；没有数据时显示轻量空状态。
- 每日明细使用 Element Plus `el-table` expandable row；展开后展示订单 ID、side、数量、成交价、状态、成交时间、PnL。
- 洞察摘要使用小卡片，不引入图表库。

## 数据流

1. `Reports.vue` 继续调用 `getRangeReport()`。
2. 新增 UI 均消费 `reportData` 内已有字段。
3. 派生数据用 `computed`，无额外 API 请求。
4. Cypress 通过 `reports.cy.ts` 的 stub payload 覆盖快捷区间、归因、drill-down、洞察和导出说明。

## 非目标

- 不改 `ReportService` 聚合口径。
- 不扩展 CSV 明细模式；本批只展示现有导出入口和文件名预览。
- 不新增独立 Reports 子页面。
- 不引入图表库。
- 不做税务、手续费精算或外部 BI。

## 验收

- `frontend/cypress/e2e/reports.cy.ts` 覆盖新增 5 项 UI 行为。
- `npm run type-check` 通过。
- `npm run build` 通过。
- focused Cypress reports spec 通过。
- @oracle 复审无 Critical/Important。
