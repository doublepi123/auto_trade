# P6 移动端与应急操作体验 Implementation Plan

> **For agentic workers:** 按任务并行推进，每个任务含明确验收标准。视觉/UI 类工作优先参考现有 Element Plus 组件规范。

**Goal:** 让 Auto Trade 在移动浏览器上具备可靠的查看状态和紧急操作体验，核心目标是"Kill Switch 可达性"。

**Architecture:** 前端响应式改造为主，后端零变更（仅在 P7 复盘工作台才需新 API）。依赖现有 Element Plus 的响应式断点和 CSS Media Queries。

**Tech Stack:** Vue 3 + TypeScript + Element Plus + Vite + Cypress。

**Baseline (2026-05-28):** `pytest 487 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

**Estimated Effort:** 4–5 天（前端主导，Cypress 视口测试穿插进行）。

**Target Completion:** 2026-06-04

---

## 迭代目标 (Sprint Goal)

> 移动端下 Dashboard 的关键控制（暂停/恢复/Kill Switch）和核心信息（价格/持仓/盈亏/PnL）可在不横向滚动的情况下完整查看和操作；移动端视口 Cypress 冒烟测试覆盖。

**完成定义 (Definition of Done):**
1. Chrome DevTools 模拟 iPhone 12 Pro (390×844) 与 Pixel 7 (412×915) 下，App.vue 导航、Dashboard.vue、Strategy.vue、History.vue、DecisionTimeline.vue 无横向滚动。
2. Dashboard 的 Pause/Resume/Kill Switch 按钮在移动端可单指触控（最小 44×44 CSS px）。
3. PriceChart 与 PnLChart 在移动端默认进入折叠状态或高度自适应，不溢出容器。
4. Cypress 新增 `mobile_smoke.cy.ts` 通过（至少覆盖 Dashboard 打开、Kill Switch 按钮存在）。
5. `npm run type-check` 0 errors，`npm run build` 通过。

---

## Task 拆分

### T1：移动端基础布局改造 (1.5 天)

**目标：** 让 App.vue 导航和 Dashboard.vue 在移动端可用。

**Files:**
- Modify: `frontend/src/App.vue`（顶部导航 → 紧凑模式或底部 Tab）
- Modify: `frontend/src/views/Dashboard.vue`（关键信息优先，图表折叠/精简）
- Modify: `frontend/src/components/PriceChart.vue`、`frontend/src/components/PnLChart.vue`（响应式折叠或高度自适应）
- Test: `frontend/cypress/e2e/mobile_smoke.cy.ts`（新增）

#### T1.1 — 顶部导航移动端适配

- 当前 `App.vue` 为侧边栏导航（`el-aside`）。移动端（max-width: 768px）下改为顶部紧凑 `el-header` + `el-menu` 或底部 Tab 栏。
- 底部 Tab 栏方案（推荐，更符合移动端习惯）：
  - 底部固定高度 60px，`el-row` 4 等分：Dashboard / 策略 / 历史 / 更多（含 Kill Switch）
  - 隐藏侧边栏图标与文字，改用图标 + 短标签
- 桌面端保持现有侧边栏，通过 `window.innerWidth` 或 CSS Media Queries 切换。

**验收:** Chrome DevTools 移动视口下侧边栏消失，底部 Tab 可见；桌面视口侧边栏保持。

#### T1.2 — Dashboard 移动端布局重排

- **移除/折叠元素：**
  - 图表（PriceChart / PnLChart）默认折叠为"展开"按钮，节省纵向空间。
  - 非必要字段（如"下次 LLM 分析时间"）在移动端隐藏。
- **优先展示（从上到下）：**
  1. 当前价格 + 涨跌幅（大号字体）
  2. 持仓方向 + 数量 + 入场均价
  3. 当前盈亏（颜色区分绿/红）
  4. Pause / Resume / Kill Switch 按钮行（等宽并排，高度 ≥ 44px）
  5. 最近 3 条订单（精简表：时间/方向/价格/状态）
  6. 操作日志（最近 5 条，折叠）
- 使用 Element Plus 的 `el-col :xs="24" :sm="12" :md="8"` 实现流式布局。

**验收:** iPhone 12 Pro 视口下 Dashboard 无横向滚动；Kill Switch 按钮可见且可点击。

#### T1.3 — 图表组件响应式适配

- PriceChart / PnLChart：
  - 添加 `responsive` 选项：移动端（`< 768px`）高度从 300px 降至 180px。
  - 若空间不足（容器宽度 `< 360px`），自动隐藏 legend 或坐标轴标签。
  - Dashboard 下默认折叠（`v-if="!isMobile || chartExpanded"`），用户手动展开。

**验收:** iPhone 12 Pro 视口下图表区域高度 ≤ 200px 或不溢出；无横向滚动。

#### T1.4 — Cypress 移动端冒烟测试

新建 `frontend/cypress/e2e/mobile_smoke.cy.ts`：

```ts
describe('Mobile smoke tests', { viewportWidth: 390, viewportHeight: 844 }, () => {
  beforeEach(() => {
    cy.viewport(390, 844);
  });

  it('Dashboard loads without horizontal overflow', () => {
    cy.visit('/');
    cy.get('body').then($body => {
      expect($body[0].scrollWidth).to.be.lte($body[0].clientWidth);
    });
  });

  it('Kill Switch button is visible and clickable', () => {
    cy.visit('/');
    cy.contains('Kill Switch').should('be.visible').click();
    cy.contains('确认').click(); // 确认对话框
  });

  it('Navigation switches to bottom tabs on mobile', () => {
    cy.visit('/');
    cy.get('[data-testid="bottom-nav"]').should('be.visible');
    cy.get('[data-testid="side-nav"]').should('not.be.visible');
  });
});
```

**验收:** Cypress 通过。

---

### T2：Strategy / Credentials / Timeline 移动端适配 (1.5 天)

**目标：** 确保参数配置、凭证管理、历史查询在手机上也能够不溢出、可操作。

**Files:**
- Modify: `frontend/src/views/Strategy.vue`
- Modify: `frontend/src/views/Credentials.vue`
- Modify: `frontend/src/views/TradeHistory.vue`（或 History.vue）
- Modify: `frontend/src/views/DecisionTimeline.vue`
- Test: 扩展 `frontend/cypress/e2e/mobile_smoke.cy.ts`

#### T2.1 — Strategy.vue 移动端单列布局

- 表单使用 `el-form` + `el-col :xs="24"` 保证移动端每行一个字段。
- 输入框高度 ≥ 44px，增加触摸友好性。
- 区间配置卡片（buy_low / sell_high / quantity）在移动端改为垂直堆叠而非左右两列。
- LLM 智能区间卡片：隐藏"下次分析时间"提示（保留开关和置信度）。

**验收:** iPhone 12 Pro 视口下 Strategy 页面无横向滚动，所有输入框可点击。

#### T2.2 — Credentials.vue 移动端单列布局

- 通知渠道列表：移动端改为垂直卡片而非表格。
- Webhook URL 输入框在移动端全宽。
- sct_key 和 Longbridge 凭证输入框加 `show-password` 保持安全，但在移动端需确保右侧眼睛图标不遮挡触控。

**验收:** iPhone 12 Pro 视口下 Credentials 页面无横向滚动。

#### T2.3 — TradeHistory.vue / DecisionTimeline.vue 移动端精简

- History 交易历史表格：移动端默认隐藏"手续费"列，时间格式缩短为"HH:mm"。
- Timeline 事件列表：移动端每条卡片减少 padding，action/severity/source 标签换行排列。

**验收:** iPhone 12 Pro 视口下 History / Timeline 页面无横向滚动。

---

### T3：PWA 基础支持（可选，延后到 P7-P8 之间）

**目标：** 让页面可添加到手机主屏幕，离线时显示基础壳体。

**Files:**
- Modify: `frontend/index.html`（增加 `manifest.json` 链接）
- Create: `frontend/public/manifest.json`
- Modify: `frontend/vite.config.ts`（可选：不引入 vite-plugin-pwa，手写 Service Worker 基础缓存）

**范围控制（最小可行）：**
- manifest.json 含 `name`, `short_name`, `icons`, `start_url`, `display: standalone`。
- 不引入 vite-plugin-pwa（避免构建复杂度），仅保证"添加到主屏幕"行为可用。
- 离线时浏览器本身缓存上次加载的 JS/CSS，API 调用失败由 axios interceptors 提示"网络不可用"。

**暂不实施原因：** 纯前端工作，依赖后端不多，可并入 P7 复盘工作台或 P8 观察列表的前端大改版时一起做。

---

### T4：测试与验证 (1 天)

#### T4.1 — 全视口冒烟矩阵

在 Chrome DevTools 模拟以下设备，记录滚动条与触控可用性：

| 设备 | 视口 | App 导航 | Dashboard | Strategy | Credentials | History | Timeline |
|------|------|----------|-----------|----------|-------------|---------|----------|
| iPhone 12 Pro | 390×844 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| Pixel 7 | 412×915 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| iPad Mini (竖屏) | 768×1024 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 桌面 (参考) | 1920×1080 | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

#### T4.2 — CI 验证

- `cd frontend && npm run type-check` → 0 errors。
- `cd frontend && npm run build` → 成功。
- Cypress `mobile_smoke.cy.ts` + 原有 14 个 spec 全绿。

#### T4.3 — 交付 Commit

```bash
GIT_MASTER=1 git add -A
GIT_MASTER=1 git commit -m "feat(p6): mobile responsive layout with bottom nav and Cypress smoke tests"
```

---

## 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| Element Plus 响应式断点与自定义 Media Queries 冲突 | T2 延迟 | 尽量使用 Element Plus 的 `el-col` props（`:xs`/`sm`/`md`），少写自定义 CSS。
| Kill Switch 对话框在移动端被软键盘遮挡 | 可用性下降 | 对话框使用 `el-dialog` 的 `top="10vh"` + `width="90%"`；Kill Switch 确认按钮使用 `type="danger"` 且高度 ≥ 44px。 |
| PWA 相关改动引入新的 build 问题 | 额外时间 | 本次迭代不强制做 PWA，视 T1/T2 完成后的余量再决定。 |

---

## 建议执行顺序

| 天 | 任务 | 产出 |
|---|---|---|
| 1 | T1.1 + T1.2（导航 + Dashboard 重排） | App.vue 底部导航可用，Dashboard 移动端无溢出 |
| 2 | T1.3 + T1.4（图表折叠 + Cypress 冒烟） | 图表响应式正常，mobile_smoke.cy.ts 通过 |
| 3 | T2.1 + T2.2（Strategy + Credentials 单列） | 两页移动端可用 |
| 4 | T2.3 + T4（History/Timeline 精简 + 全视口矩阵验证） | 所有页面通过移动视口验证 |
| 5 | 缓冲 / PWA 可选 / PR 准备 | commit + 更新 Roadmap.md |

---

## 附录：当前 UI 架构速查

- `frontend/src/App.vue`: 侧边栏导航（`el-aside` + `el-menu`）
- `frontend/src/views/Dashboard.vue`: 价格/盈亏/图表/控制按钮
- `frontend/src/components/PriceChart.vue`: 基于 SVG 的轻量折线图
- `frontend/src/components/PnLChart.vue`: 基于 SVG 的面积图
- `frontend/src/views/Strategy.vue`: 区间策略配置表单
- 移动端检测参考：`const isMobile = ref(window.innerWidth <= 768)`（或 CSS Media Queries）
