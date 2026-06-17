# P64-P68 Trade Analytics Frontend（5 轮自主迭代）

> 用户指令："自主进行5轮feature迭代"；用户批准方向后要求 5 轮结束前不再提问，由 agent 自主决策。

## 目标

把已经存在的后端只读端点 `/api/trades/analytics/*` 接入前端 Trade History 页面，让用户能直接看到往返成交的时间维度和盈亏分布洞察。

## 选型

选择低风险前端补齐而非触碰 runner/order/risk：

- 后端 `trade_analytics_service.py`、schemas 和 `/api/trades/analytics/{calendar,hold-duration,pnl-distribution,monthly,weekday}` 已存在。
- 前端尚未接入这些端点；补齐 UI 可以释放已有后端价值。
- 本批只读、无新表、无 broker 调用、无下单路径变更。

## 5 轮范围

| 代号 | 主题 | 前端呈现 |
|---|---|---|
| P64 | 交易日历 | 每个平仓日：交易数、净 PnL、涉及标的 |
| P65 | 持仓时长分布 | `<5m / 5m-1h / 1h-1d / 1d-1w / >=1w` 桶的交易数、胜率、净 PnL |
| P66 | 盈亏分布 | 亏损/打平/盈利区间的交易数和净 PnL |
| P67 | 月度汇总 | 每月交易数、胜率、累计 PnL、回撤 |
| P68 | 星期归因 | 周一到周日交易数、胜率、净 PnL |

## UI 设计

- TradeHistory 保持现有主流程：订单表仍在上方，已实现成交折叠区不移动。
- 在已实现成交区后新增折叠区「交易分析（只读）」；默认折叠，避免挤占主要订单列表。
- 折叠区内使用 5 个轻量卡片，复用现有 `.stats-card` / `.analytics-card` 的浅色、低阴影风格。
- 不引入图表库；用表格、进度条和简短条形视觉表达，延续项目“纯 SVG/轻量视图”原则。

## 数据流

1. `frontend/src/api/trades.ts` 增加 5 个 GET 方法，复用 `api.get`。
2. `frontend/src/types/index.ts` 增加与后端 schema 同名的 TypeScript interface。
3. `TradeHistory.vue` 在 `onMounted` 中加载分析数据；失败吞掉并仅保留页面可用性，因为分析是辅助信息。
4. 日期筛选复用已实现成交的 `rtFromDate/rtToDate`，点击「拉取」时同时刷新成交和分析。

## 非目标

- 不改后端聚合算法。
- 不新增 DB 表或迁移。
- 不新增全局导航页；本批只在 Trade History 中补齐。
- 不做高级图表、拖拽、导出或实时 WebSocket 推送。

## 验收

- Cypress `history.cy.ts` 覆盖 5 个分析卡片可见和代表性数据。
- `npm run type-check` 通过。
- `npm run build` 通过。
- 工作树不包含密钥、数据库或本地配置。
