# P129–P138 · 运营健康与数据可信度 (Operational Health & Data Trust)

> 自主 feature 迭代第 14 批（10 轮）。延续低风险路线：**全部纯前端**，不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径。

## 背景

交易系统最大的运维风险是「信任过期/断连的数据」。后端已追踪行情推送存活（`_resubscribe_quotes_if_silent` ~90s）、连接状态、`/api/calendar/session`、`/api/diagnostics`，但前端只把**连接状态** (`realtimeStatus`) 暴露在 Dashboard 顶部标签里，且该 WS 连接的生命周期绑定在 Dashboard 组件上——一旦离开 Dashboard，连接关闭，其他页面既看不到连接状态、也看不到数据新鲜度。

本批把「数据可信度」显式化、全局化：一个常驻 App shell 的连接健康单例驱动全局徽标、数据年龄、过期水印、断连/恢复反馈与会话感知横幅，并补齐若干共享 UX 基础件（DataState / 数字格式 / 剪贴板 / 刷新时间戳），统一各页空/错/加载/新鲜度表现。

## 范围与约束

- **纯前端**：复用既有 `/api/status`、`/api/calendar/session`、`/api/diagnostics` 等只读响应；不发写请求、不触发 analyze/preview/enable/disable、不下单。
- **连接上提**：把 WS 连接所有权从 Dashboard 组件上提到 `useConnectionHealth` 模块单例，在 App shell 启动，全页常驻；Dashboard 改为消费该单例。保留 Cypress `polling` 短路与 3s 轮询回退、`realtimeStatus` 枚举值不变。
- **不双连**：全局与 Dashboard 共享同一连接；不允许多个 WS。

## 10 轮

| 代号 | 主题 | 关键交付 |
|------|------|----------|
| **P129** | 连接健康单例 + 全局 header 健康徽标 | `useConnectionHealth`（WS + 轮询 + `status` + `realtimeStatus` + `lastDataAt` + `ageSeconds` + `reconnectNow`）；App.vue 常驻徽标；Dashboard 消费单例；删除 `useStatusStream.ts` |
| **P130** | 相对时间工具 + 徽标显示数据年龄 | `utils/time.ts` `relativeTime(ms)`；徽标显示「实时 · 3s前」，>10s 琥珀、>30s 红 |
| **P131** | Dashboard 过期数据水印 | 状态年龄超阈值(~15s) 时价格/状态条显示琥珀「数据 Xs 前」徽标，点击重连 |
| **P132** | 交易时段感知横幅 | 非 RTH(pre/post/lunch/closed) 时全局可关闭 `el-alert`；`useMarketSession` 60s 轮询 `/api/calendar/session`；dismiss 存 localStorage |
| **P133** | 共享 `<DataState>` 组件 | loading/empty/error/stale 槽位；接入 Watchlist + NotificationCenter |
| **P134** | 数字/百分比格式工具 + `<MetricStat>` | `formatPercent`/`formatSigned`/`formatCompact`/`signedCurrency`；`<MetricStat>` 组件；接入若干原始数字面板 |
| **P135** | 剪贴板工具 + `<CopyButton>` | `utils/clipboard.ts`（`navigator.clipboard` + `execCommand` 回退）；接入 TradeHistory broker 订单号 + DecisionTimeline 订单/事件 ID |
| **P136** | 断连/恢复反馈 toast | `realtimeStatus` 转换 watcher：掉线 warning、恢复 success（防抖） |
| **P137** | 各页「更新于」刷新时间戳 | Dashboard/Watchlist/Reports 显示最近成功刷新时间 |
| **P138** | 复制健康快照（报障）按钮 | 健康弹窗一键复制 连接/年龄/时段/运行器 摘要到剪贴板 |

## 设计要点

- **单例连接**：模块级 refs + `ensureStarted()` 幂等守卫；不在组件 `onUnmounted` 关闭（App 生命周期）。Cypress 下短路为 `polling`，与原 `useStatusStream` 行为一致。
- **年龄计算**：`ageSeconds` 由 1s ticker 从 `lastDataAt`（WS 消息或成功轮询时刷新）派生；P130/P131/P138 共用。
- **会话横幅与 SessionClockPanel 区分**：后者展示下次开盘倒计时；前者仅在非 RTH 提示「行情可能延迟」，可关闭。
- **共享基础件复用**：`DataState`/`MetricStat`/`CopyButton` 为受控展示件，不引入新依赖；Element Plus 自动按需装配已就绪。
- **剪贴板降级**：`navigator.clipboard` 不可用时回退 `document.execCommand('copy')`，Promise 失败不抛。
- **toast 防抖**：仅在状态真正转换时触发，避免轮询抖动反复弹。

## 验证

- `npm run type-check` 0 errors；`npm run build` 通过；chunk 预算不退步（`build:check-chunks`）。
- 为每轮新增 Cypress spec（本机无法 headless 运行，按既有约定仅类型/构建校验，spec 交 CI）。
- 后端 `pytest` 不受影响（纯前端）。

## 显式 YAGNI 未做

- WS 消息级 QoS / 丢帧检测、按渠道分别健康、健康历史落库、PWA 离线健康缓存、跨设备健康同步。
