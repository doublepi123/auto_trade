# Auto Trade 迭代计划 (Iteration Roadmap)

> 本文档根据项目当前状态、近期完成工作、以及原始设计规格中的非目标/限制项制定。每次迭代聚焦单一主题，确保可交付、可验证、可回滚。

---

## 项目当前状态快照

| 维度 | 状态 |
|------|------|
| **核心交易能力** | ✅ 就绪。区间交易策略引擎、长桥SDK集成、风控系统、订单执行全部就位。 |
| **Web UI** | ✅ 就绪。Dashboard、Strategy、Credentials、Trade History 四个页面，Vue 3 + Element Plus。 |
| **API 覆盖** | ✅ 完备。策略配置、凭证管理、订单查询、状态获取、运行时控制（启停/暂停/Kill Switch）。 |
| **WebSocket 推送** | ✅ 就绪。实时状态同步。 |
| **本地部署** | ✅ 就绪。Docker Compose 一键启动。 |
| **测试** | ✅ 就绪。Backend pytest 单元测试、Frontend Cypress E2E 测试。 |
| **凭证安全** | ✅ 就绪。主密钥 + AES-GCM 加密存储，前端不回显明文。 |
| **数据库** | ✅ 就位。SQLite，含 Alembic 迁移。 |

---

## 近期已完成迭代 (2026-05-17)

- **可维护性重构 (Maintainability Refactor)**
  - 提取 `TradeExecutionService`：将订单执行、状态查询、通知、PnL 从 `AppRunner` 中解耦。
  - 提取 `RuntimeStateService`：将引擎与风控状态的加载/持久化解耦。
  - 重构 `AppRunner`：成为纯粹的生命周期协调器（订阅行情、路由事件、后台保活）。
  - 前端 API 层拆分为按域模块 (`client.ts`, `strategy.ts`, `credentials.ts`, `trade.ts`)。
  - 引入 Vue Composables (`useDashboardData`, `useStatusStream`, `useAccountRefresh`, `useFormState`) 清理页面级逻辑。
  - 补充 E2E 测试（Cypress 全页面导航、控制、策略、凭证、历史），Dashboard 可用性区分不可用与零值。

---

## 迭代目标

在保持现有功能稳定的基础上，逐步消除 README 中列举的"限制"项，并向生产级运维体验演进。当前计划覆盖 5 个迭代，主题分别为：**LLM智能区间调整**、**回测系统**、**实时图表与监控增强**、**日志审计与报警**、**响应式与移动端适配**。LLM 智能区间调整作为当前最高优先级，因为它直接提升策略的核心交易决策质量。

---

## 迭代 0：LLM 智能区间调整 (LLM Intelligent Interval)

> **目标：** 通过 DeepSeek LLM 分析市场行情，自动为策略推荐买入/卖出价格区间，减少人工频繁调整的依赖。策略运行期间持续优化区间，实现动态自适应交易。
> **价值：** 核心交易决策智能化，显著降低人工调参成本，提升策略对不同市场环境的适应能力。
> **设计文档：** [docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md](docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md)

### 任务

#### 0.1 数据聚合与 LLM 顾问服务

- 新建文件：
  - `backend/app/services/data_aggregator.py`：聚合长桥历史行情、实时价格、持仓状态、最近成交记录，构建 LLM Prompt。
  - `backend/app/services/llm_advisor_service.py`：调用 DeepSeek API，解析 JSON 响应，防抖控制（30 分钟窗口）。
- 实现细节：
  - 输入数据：加权组合（7 天日 K 权重 60% + 24 小时分钟 K 权重 40%），附带 ATR、布林带指标。
  - Prompt 要求 LLM 输出 `suggested_buy_low`, `suggested_sell_high`, `confidence_score`, `analysis`。
  - 失败处理：API 超时 30s，重试一次，失败时通过 Server酱通知 "LLM 区间分析失败"。

#### 0.2 渐进式平滑过渡策略

- 新建文件：
  - `backend/app/services/interval_application_service.py`：核心规则引擎。
- 实现细节：
  - **FLAT（空仓）**：LLM 建议立即生效。
  - **LONG（持多）**：只能上调 sell_high（取 max(old, new)），buy_low 被忽略。
  - **SHORT（持空）**：只能下调 buy_low（取 min(old, new)），sell_high 被忽略。
  - **风控兜底**：置信度 < 0.7 拒绝；sell_high < price * 1.05 拒绝；buy_low > price * 0.95 拒绝；区间宽度 > 20% 拒绝。

#### 0.3 定时触发与手动触发

- 新建文件：
  - `backend/app/crontabs/interval_analysis_cron.py`：APScheduler 定时任务（每 4 小时）。
- 新增 API：`POST /api/strategy/llm-interval/analyze`（手动触发），`GET /api/strategy/llm-interval/status`（状态查询）。

#### 0.4 前端 UI 集成

- 修改文件：
  - `frontend/src/views/Strategy.vue`：增加 LLM 智能区间卡片（开关、置信度显示、建议区间、上次应用原因）。
  - `frontend/src/views/Dashboard.vue`：状态栏增加 LLM 下次分析时间指示器。
- 新增 API Client：`frontend/src/api/llm_advisor.ts`。

#### 0.5 数据库迁移

- Alembic 迁移：为 `strategy_config` 表新增 `auto_interval_enabled`, `llm_suggested_buy_low`, `llm_suggested_sell_high`, `llm_confidence_score`, `llm_analysis`, `llm_last_analysis_at`, `llm_next_analysis_at`, `llm_applied_buy_low`, `llm_applied_sell_high`, `llm_applied_at`, `llm_reject_reason`。

#### 验证

- [ ] 后端 pytest：覆盖渐进式过渡规则、风控兜底、LLM 解析、防抖限制。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `strategy_llm_*.cy.ts` 和 `dashboard_llm_indicator.cy.ts`。
- [ ] Docker Compose 启动后，手动触发分析返回正确结构；30 分钟内重复触发被限制。

---

## 迭代 1：回测系统 (Backtesting)

> **目标：** 消除 README 中"暂不支持回测系统"的限制。实现策略的历史数据回测验证，帮助用户在实盘运行前验证参数合理性。
> **价值：** 显著降低盲目上线风险，是量化交易策略上线前的核心验证环节。

### 任务

#### 1.1 设计回测数据接口与数据层

- 新建文件：
  - `backend/app/core/backtest.py`：回测引擎（纯数据驱动的策略推演）。
  - `backend/app/api/backtest.py`：FastAPI 路由：`POST /api/backtest/run`。
  - `backend/app/schemas.py`：新增 `BacktestResult`, `BacktestParams`, `BacktestTradeLog`。
  - `backend/tests/test_backtest.py`：单元测试。
- 实现细节：
  - `BacktestEngine` 不依赖实时的长桥行情，而是接收历史价格序列和策略参数，逐 K 线推演状态机（flat -> long -> flat 或 flat -> short -> flat），输出每笔虚拟成交。
  - 价格序列由用户提供（CSV 上传）或长桥历史行情接口获取（二期可选；一期先用用户上传）。
  - 回测需要模拟风控逻辑（日亏损、连续亏损），但不触发真实通知和真实下单。

#### 1.2 前端回测页面

- 新建/修改文件：
  - `frontend/src/views/Backtest.vue`：策略参数选择与历史数据上传界面。
  - `frontend/src/api/backtest.ts`：`runBacktest` API 调用。
  - `frontend/src/router/index.ts`：新增 `/backtest` 路由，导航栏加入"回测"入口。
- 实现细节：
  - 支持配置回测参数（与实盘策略参数共用 `StrategyConfig`，但可选覆盖）。
  - 上传 CSV（格式：`timestamp,open,high,low,close,volume`）。
  - 提交后端生成回测结果，前端以表格和图表展示。

#### 1.3 回测结果可视化

- 前端新增 `frontend/src/components/BacktestChart.vue`：
  - 采用折线图展示标的收盘价与买卖信号点。
  - 采用柱状图展示每日盈亏（PnL）。
  - 表格展示每笔虚拟成交记录（时间、动作、价格、数量、盈亏）。
  - 汇总指标：总收益率、最大回撤、夏普比率、胜率、盈亏比。

#### 验证

- [ ] 后端 `test_backtest.py` 通过（至少覆盖 flat-long-flat 与 flat-short-flat 两条主路径）。
- [ ] 前端 `npm run build` 通过。
- [ ] Cypress 新增 `backtest.cy.ts` 覆盖页面交互。
- [ ] Docker Compose 全栈启动后，`curl -X POST http://localhost:8000/api/backtest/run` 返回正确结构。

---

## 迭代 2：实时图表与监控增强 (Dashboard Visualization)

> **目标：** Dashboard 从纯文本仪表盘升级为带实时折线图、盈亏曲线的监控中心。
> **价值：** 将离散的价格/盈亏数字转化为时间序列图形，帮助用户快速判断策略当前状态和历史走势。

### 任务

#### 2.1 后端增加历史状态查询接口

- 新建/修改文件：
  - `backend/app/api/status.py`：新增 `GET /api/status/history?from=...&to=...`，按区间查询历史 `RuntimeState` 快照。
  - `backend/app/services/runtime_state_service.py`：补充 `query_history` 方法。
- 实现细节：
  - 基于 `orders` 表和 `runtime_state` 表的数据，按时间范围聚合返回价格/盈亏序列。

#### 2.2 Dashboard 实时价格与盈亏曲线

- 新建/修改文件：
  - `frontend/src/components/PriceChart.vue`：实时价格折线图，随 WebSocket 消息更新。
  - `frontend/src/components/PnLChart.vue`：盈亏柱状图/面积图，随 WebSocket 消息更新。
  - `frontend/src/views/Dashboard.vue`：引入图表组件，重新排布布局。
- 实现细节：
  - 图表库选择：优先使用 `vue-echarts`（轻量、与 Vue 3 生态兼容），替代重量级图表库。
  - 数据点缓存限制：前端保留最近 200 个数据点，保持内存和渲染性能。
  - 图表需要区分"历史加载"（页面打开时 REST 查询）和"实时追加"（WebSocket 推送）。

#### 2.3 交易信号标记

- 图表上标记策略触发的买入/卖出点位，以箭头和颜色区分。
- 数据来源：页面打开时加载 `GET /api/orders` 的最近成交记录，并在 WebSocket 收到新的风控通过+下单事件时实时追加标记。

#### 验证

- [ ] 前端 `npm run build` 通过。
- [ ] 新页面打开后图表能正确加载历史数据并随 WebSocket 实时更新。
- [ ] Cypress 新增 `dashboard_charts.cy.ts` 页面校验图表元素渲染。

---

## 迭代 3：日志审计与报警系统 (Audit Logging & Alerting)

> **目标：** 由当前的 Server酱单一通知渠道，升级为支持自定义 Webhook、Email 的多渠道报警系统；并增加操作审计日志。
> **价值：** 生产环境运维需要更灵活的报警通道（如企业微信、钉钉、Slack）；操作审计满足合规与排障需求。

### 任务

#### 3.1 操作审计日志

- 新建/修改文件：
  - `backend/app/models.py`：新表模型：`AuditLog`（`id`, `action`, `actor`, `detail`, `ip`, `created_at`）。
  - `backend/app/core/audit.py`：`AuditLogger`，封装审计日志记录逻辑。
  - `backend/app/api/*.py`：核心操作（启动/停止/暂停/Kill Switch/策略修改/凭证修改）调用 `AuditLogger.record(...)`。
  - `backend/alembic/versions/`：生成数据库迁移脚本。
- 实现细节：
  - `action` 枚举：`START`, `STOP`, `PAUSE`, `RESUME`, `KILL_SWITCH`, `STRATEGY_UPDATE`, `CREDENTIALS_UPDATE`。  
  - `actor`：从请求头中读取 `X-API-Key` 的哈希（不存明文）。
  - `ip`：读取请求 `X-Forwarded-For` 或 `client.host`。

#### 3.2 通知渠道扩展

- 新建/修改文件：
  - `backend/app/core/notify.py`：重构为策略模式：`NotifierInterface`。
  - `backend/app/core/notifiers/`：新目录，包含：
    - `serverchan.py`：现有 Server酱实现。
    - `webhook.py`：通用 Webhook POST 通知（允许用户配置任意 URL 和模板）。
- 实现细节：
  - 凭证配置中新增 `notification_channel` 字段（`serverchan` | `webhook`），后端根据配置实例化对应 Notifier。
  - 前端 Credentials 页面增加通知渠道选择表单。

#### 3.3 风控事件报警分级

- 修改：
  - `backend/app/core/risk.py`：不同级别事件调用不同的通知方法（如风控暂停 vs Kill Switch 可分别指定通知级别）。
- 实现细节：
  - `notify_risk_event` 增加 `severity` 参数（`WARNING`, `CRITICAL`）。
  - `CRITICAL` 级别同时触发所有配置的通知渠道（Server酱 + Webhook），确保不遗漏。

#### 验证

- [ ] 后端新增 `test_audit.py` 和 `test_notifiers.py`，测试覆盖日志写入与通知发送。
- [ ] Docker Compose 启动后，修改策略能触发日志写入；触发风控能收到 Webhook 消息。
- [ ] Cypress 凭证页面可切换通知渠道。

---

## 迭代 4：响应式与移动端适配 (Responsive & Mobile)

> **目标：** 让前端在移动浏览器上有可用的操作体验。
> **价值：** 用户需要随时随地查看策略状态或执行 Kill Switch，移动端是高频场景。

### 任务

#### 4.1 前端响应式布局

- 修改文件：
  - `frontend/src/App.vue`：侧边栏导航改为可折叠或底部 Tab 栏（移动端）。
  - `frontend/src/views/Dashboard.vue`：关键指标以卡片流排布，图表高度自适应，控制按钮增大触控区域。
  - `frontend/src/views/Strategy.vue`：表单增加移动端单列布局。
  - `frontend/src/views/Credentials.vue`：同策略页面。
- 实现细节：
  - 使用 CSS Media Queries 与 Element Plus 的 `el-col` 响应式断点。
  - 隐藏非必要的复杂图表（如回测图表在移动端可折叠）。

#### 4.2 PWA 基础支持

- 修改文件：
  - `frontend/index.html`：增加 `manifest.json` 链接。
  - `frontend/public/manifest.json`：Web App Manifest。
  - `frontend/vite.config.ts`：使用 `vite-plugin-pwa` 配置 Service Worker（离线缓存静态资源和基础页面）。
- 实现细节：
  - 离线时页面仍然可以打开（前端缓存），但 API 调用提示"网络不可用"。
  - 目标不是完全离线可用，而是保证页面壳体和已缓存数据的离线访问。

#### 4.3 移动端安全优化

- `nginx.conf`：增加移动设备相关的响应头优化（`X-Content-Type-Options`, `Referrer-Policy`）。
- 登录/凭证页面：移动端自动隐藏密码输入框的保存提示，防止凭证泄露到设备键盘记忆。

#### 验证

- [ ] 使用 Chrome DevTools 模拟 iPhone 14 Pro、Pixel 7 设备，页面无横向滚动、按钮可点击、文字不溢出。
- [ ] 在移动 Safari 上测试 PWA "添加到主屏幕" 后图标和启动画面正确。
- [ ] Cypress 增加移动端视口尺寸的烟雾测试（`cy.viewport('iphone-x')`）。

---

## 迭代优先级总结

| 迭代 | 主题 | 价值 | 难度 | 建议顺序 |
|------|------|------|------|----------|
| 1 | 回测系统 | 极高（消除核心限制，降低实盘风险） | 中等 | **第一个** |
| 2 | 实时图表与监控增强 | 高（提升日常监控体验） | 较低 | 第二个 |
| 3 | 日志审计与报警 | 高（满足生产合规需求） | 中等 | 第三个 |
| 4 | 响应式与移动端适配 | 中高（扩展使用场景） | 较低 | 第四个 |

---

## 实施建议

1. **一次只做一条主线。** 每个迭代内可以并行开发互不影响的子任务（如前后端可并行），但不要在同一个代码基上交叉进行两个迭代的开发，避免功能碎片化。
2. **每个迭代产出物：**
   - 一个保存在 `docs/superpowers/plans/` 的实施计划文件（可参考已有格式）。
   - 完成后更新此 Roadmap，标记对应迭代完成状态。
3. **回测系统是当前最高优先级**，因为它直接消除 README 中列出的最大限制，且独立性好，不会与现有运行时逻辑产生深度耦合。
4. **测试纪律：** 每个新增功能必须有对应测试（后端 unit test + 前端 Cypress E2E），覆盖率不低于现有的 80% 水平。

---

## 附录：原始设计中的非目标与当前状态对照

| 非目标（原始） | 当前状态 | 计划 |
|---|---|---|
| 多标的组合策略 | ❌ 仍不支持 | 超出当前 Roadmap，需更大架构升级 |
| 高频交易 | ❌ 仍不支持 | 设计限制，暂不改变 |
| 复杂择时指标 | ❌ 仍不支持 | 暂不改变 |
| 回测系统 | ❌ -> ✅ 计划中 | **迭代 1** 实现 |
| 量化研究平台 | ❌ 仍不支持 | 超出范围 |
| 代客理财/公开策略分发 | ❌ 仍不支持 | 法律合规限制，永久不在计划中 |

