# P74-P78 LLM 运维可观测性前端化（5 轮自主迭代）

> 用户指令："自主再进行5轮迭代"。本批选择 Roadmap 点名的「多标的 LLM 结果前端化」方向，限定为只读前端增强。

## 目标

在 Lab「LLM 优化工作台」新增「运行状态」页签，让运维能直接看到 LLM 区间分析是否启用、预算是否耗尽、各 symbol 下次分析时间/跳过原因、最近交互结果和健康提示。

## 选型

选择低风险只读 UI：

- 后端已有 `GET /api/strategy/llm-interval/status`，返回 `enabled`、last/next 时间、current suggestion、budget、symbol_statuses。
- 后端已有 `GET /api/strategy/llm-interval/interactions`，返回最近交互列表。
- 前端已有 `llm_advisor.ts` typed client 和 Lab 页面；本批不新增后端端点、不改 runner、不开启/关闭 LLM。

## 5 轮范围

| 代号 | 主题 | 交付 |
|---|---|---|
| P74 | LLM 状态总览 | enabled、最近/下次分析、当前建议 |
| P75 | 预算卡片 | 每小时预算、已用次数、剩余额度、重置时间 |
| P76 | Symbol 状态表 | 每个 symbol 的 last/next、last_status、last_skip_reason |
| P77 | 最近交互表 | 最近 LLMInteraction 的类型、symbol、成功/应用、动作、时间 |
| P78 | 健康提示 | 从 status + budget + symbol statuses 派生 warnings/info |

## UI 设计

- Lab 新增 `el-tab-pane label="运行状态" name="runtime"`。
- 进入页签时 lazy-load，避免 Lab 首屏额外请求。
- 提供「刷新」按钮；失败只提示，不影响其他 Lab 页签。
- 使用 Element Plus card/table/tag，不引入图表库。

## 数据流

1. `Lab.vue` 复用 `getLLMIntervalStatus()` 与 `getLLMInteractions()`。
2. 新增 `runtimeStatus` / `runtimeInteractions` / `runtimeLoading` state。
3. `watch(activeTab)` 在切到 runtime 且尚未加载时调用 `loadRuntimeStatus()`。
4. 健康提示用 computed 从已有响应派生。

## 非目标

- 不新增后端 API。
- 不触发 analyze/preview/enable/disable 等写操作。
- 不做 WebSocket 实时推送。
- 不做 prompt diff 或重新执行。
- 不修改 LLM 调度预算算法。

## 验收

- Cypress `lab.cy.ts` 覆盖运行状态页签、总览、预算、symbol 表、交互表、健康提示。
- `npm run type-check` 通过。
- `npm run build` 通过。
- @oracle 复审无 Critical/Important。
