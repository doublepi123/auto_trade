# P139–P148 · 运维效率与个性化 (Power-user Productivity & Customization)

> 自主 feature 迭代第 15 批（10 轮）。延续低风险路线：**全部纯前端**，不新增后端端点、不新增表、不触碰 broker/order/runner/risk 写路径。承接 P129–P138 的运营健康基础（复用 `useConnectionHealth`、`utils/clipboard.ts`、`MetricStat` 等）。

## 背景

应用已扩展到 13 个页面 + 大量只读面板。键盘可达性仅限于 P125 的单字母切路由。本批补齐「高级用户效率层」：一个全局命令面板作为中枢，叠加密度/列显隐/行内键盘导航/固定标的/统一帮助等可持久化的个性化能力，让日常运维（巡检、跳转、紧急操作）更快、更可定制。

## 范围与约束

- **纯前端**：复用既有 API（控制动作走既有 `/api/control/*`、watchlist、strategy）；无新端点。
- **命令面板**：模块单例 `useCommandPalette` + `CommandPalette.vue`；`Cmd/Ctrl+K` 全局打开；纯前端 fuzzy（无新依赖）；命令分组（导航 / 操作 / 设置）；最近使用持久化 localStorage。
- **个性化持久化**：密度、列显隐、固定标的、最近页面、最近命令 → 统一存 `localStorage`（`auto_trade.*` 命名空间，与既有 `auto_trade.theme.dark` 一致）。
- **键盘不破坏既有**：面板与 `/`、`j/k` 等仅在非输入框聚焦时生效（复用 App.vue 既有的 `isTypingTarget` 思路）；不覆盖浏览器保留组合键之外的拦截除非显式 `preventDefault`。

## 10 轮

| 代号 | 主题 | 关键交付 |
|------|------|----------|
| **P139** | 命令面板 shell（Cmd/Ctrl+K） | `CommandPalette.vue` + `useCommandPalette`；分组命令、fuzzy 过滤、↑↓/Enter/Esc 键盘导航、最近使用持久化；导航/控制/主题命令 |
| **P140** | 面板：标的速跳 | 索引 strategy + watchlist 标的；键入符号 → 「在仪表盘查看 AAPL.US」跳 Dashboard 并切换图表标的 |
| **P141** | 面板：最近访问页面 | 跟踪路由访问，面板置顶最近 N 页 |
| **P142** | 全局密度切换 | `el-config-provider` 接管 `size`（small/default/large），header 按钮，持久化 |
| **P143** | NotificationCenter 列显隐 | 列复选框（severity/success/symbol/source/time），持久化 |
| **P144** | DecisionTimeline 行键盘导航 | 表格聚焦时 ↑↓/j/k 移动高亮行，Enter 打开 LLM 详情 |
| **P145** | `/` 聚焦搜索快捷键 | 跨视图聚焦 `data-testid="view-search"`；Esc 失焦 |
| **P146** | 固定标的速跳栏 | Dashboard 顶部固定标的 chip 行（来自 watchlist），点击切图表标的，持久化 |
| **P147** | 面板「刷新当前页」命令 | provide/inject 刷新契约；Dashboard/Watchlist/Reports 注入；面板命令触发 |
| **P148** | 统一帮助抽屉 | 合并键盘快捷键 + 各页 tips；`?` 打开（取代旧 shortcuts dialog） |

## 设计要点

- **面板 fuzzy**：简单子串评分（连续匹配 + 起始位置加权），无新依赖；命令 `keywords` 数组扩大命中面。
- **命令注册**：导航命令复用 `router`；控制命令复用既有控制函数（需 App/Dashboard 暴露或在面板内直接调 API）；避免重复实现业务逻辑。
- **密度**：根 `el-config-provider :size` 影响所有 Element Plus 组件；不破坏既有 `size="small"` 显式覆盖处。
- **列显隐**：el-table 用 `v-if` 控制列；配置存 localStorage，键含视图名避免冲突。
- **键盘守卫**：所有全局键绑定先判 `isTypingTarget` + 修饰键；面板打开时拦截 `Cmd+K`/Esc/方向键。
- **刷新契约**：`provide('view-refresh', fn)`；面板 `inject` 取当前注入；未注入视图该命令禁用。

## 验证

- `npm run type-check` 0 errors；`npm run build` 通过；`build:check-chunks` / `build:check-element-plus` 不退步。
- 为每轮新增/扩展 Cypress spec（本机无法 headless 运行，仅类型/构建校验，spec 交 CI）。
- 后端 `pytest` 不受影响（纯前端）。

## 显式 YAGNI 未做

- 自定义快捷键绑定、命令面板插件化、i18n、服务端用户偏好同步、列拖拽重排、面板命令历史撤销、图表主题随密度联动。
