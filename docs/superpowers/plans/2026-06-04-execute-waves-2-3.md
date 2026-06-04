# P24 Waves 2-3 执行计划：质量清扫 + P23 Toast 通知中心

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Wave 1（后端韧性）已合入的代码基上，完成 Wave 2 质量清扫（死代码 + ai-slop + 测试加固收尾）和 Wave 3 前端实时通知中心 + 分支清理，保持 `pytest`/`basedpyright`/`vue-tsc`/Cypress 全绿。

**Architecture:** 按串行波次推进。Wave 2 内部 C/D/F 可并行 subagent（独立文件集），Wave 3 内部 P23 和 E 可并行。每波结束强制跑全量质量门禁。

**Tech Stack:** Python 3.11 / pytest / basedpyright / vulture / pyflakes / Vue 3 / TypeScript / Element Plus / Cypress

**前置状态：**
- Wave 1 已 commit（`dba6852`），`pytest 750 passed` / `basedpyright` 0/0/0 / `vue-tsc` clean / `npm run build` 通过
- 7 个子 plan 已就位（`docs/superpowers/plans/2026-06-04-{A,B,C,D,E,F,P23}-*.md`）
- 本计划是母计划；C/D/F/E/P23 的详细步骤在子 plan 中，本计划负责编排顺序和波间门禁

---

## Task 0: 更新 Roadmap 与基线确认

**Files:**
- Modify: `docs/Roadmap.md`

- [ ] **Step 0.1: 确认当前代码基干净且全绿**

```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -q
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
```

Expected: pytest 750 passed / basedpyright 0 errors / vue-tsc exit 0 / build exit 0

- [ ] **Step 0.2: 标记 Roadmap 状态**

在 `docs/Roadmap.md` 中确认 P24 Wave 1 行已标记 ✅，Waves 2-3 表格已存在。

---

## Wave 2: 质量清扫

> Wave 2 内部 Task 1~3 可并行（独立文件集），但必须全部完成后才进入 Wave 3。

### Task 1: C — 死代码清理

**详细步骤见子 plan:** `docs/superpowers/plans/2026-06-04-C-dead-code-cleanup.md`

**入口指令：**

- [ ] **Step 1.1: 扫描死代码候选**

```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pip install vulture pyflakes --quiet
.venv/bin/python -m vulture app --min-confidence=80 > /tmp/vulture-out.txt
.venv/bin/python -m pyflakes app > /tmp/pyflakes-out.txt
```

- [ ] **Step 1.2: 双向 grep 确认每个候选**

对 `vulture` 和 `pyflakes` 输出的每个符号，运行：

```bash
grep -r "SYMBOL_NAME" app/ tests/  # 确认无引用后再删除
```

- [ ] **Step 1.3: 删除已确认的死代码**

范围（按子 plan §4.3）：
- 私有函数（`_` 开头）无引用 → 可删
- 未用 import / 未用 schema 字段 → 可删
- 未用常量 → 可删
- ❌ 不删公共 API（router 中注册的 endpoint）
- ❌ 不删 `__init__.py` 中 export

- [ ] **Step 1.4: 跑全量质量门禁**

```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m basedpyright
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
```

Expected: 全绿。

- [ ] **Step 1.5: Commit**

```bash
git add -A
git commit -m "chore(backend): dead code cleanup (Task C)

- Remove unused private functions/imports/constants per vulture+pyflakes
- Verified by bidirectional grep across app/ and tests/"
```

---

### Task 2: D — 测试加固收尾

**详细步骤见子 plan:** `docs/superpowers/plans/2026-06-04-D-test-hardening.md`

**入口指令：**

> 注意：D 的核心加固（freezegun 集成、DST 边界、并发测试）已在 Wave 1 交付。本任务仅做剩余收尾。

- [ ] **Step 2.1: 识别剩余 flaky 测试**

```bash
cd /home/lcy/code/auto_trade/backend
# 跑 10 次统计稳定性
.venv/bin/python -m pytest tests/ -q --count=10 2>&1 | grep -E "FAILED|passed|failed" | tail -5
```

- [ ] **Step 2.2: 修复任何新发现的 flaky**

策略（按子 plan §4.4）：
- 用 `freezegun.freeze_time()` 替代 wall-clock 断言
- mock 时间注入 `_recent_quotes` 时间窗、90s 静默阈值
- ❌ 不删任何测试

- [ ] **Step 2.3: 跑全量质量门禁**

同 Step 1.4。Expected: 全绿。

- [ ] **Step 2.4: Commit**

```bash
git add -A
git commit -m "test(backend): harden remaining flaky tests with freeze_time (Task D)

- Replace wall-clock dependencies in time-sensitive assertions
- Inject mock clock for quote windows and silence thresholds"
```

---

### Task 3: F — 前端 ai-slop 清理

**详细步骤见子 plan:** `docs/superpowers/plans/2026-06-04-F-frontend-ai-slop.md`

**入口指令：**

- [ ] **Step 3.1: 扫描前端异味**

检查清单（按子 plan §4.6）：
1. 重复组件逻辑（≥2 处类似代码块 → 提取 composable）
2. 魔术字符串（硬编码 status、event_type → 提取常量到 `utils/constants.ts`）
3. 缺失 `data-testid`（关键交互元素）
4. 未使用 ref / computed
5. 过度复杂 `<script setup>`（> 200 行考虑拆分）
6. Vue 3 反模式（mutating props / 在 setup 外修改 reactive state）

```bash
cd /home/lcy/code/auto_trade/frontend
# 手动审查以下高频文件（已知异味区域）：
# src/views/Dashboard.vue
# src/views/Strategy.vue
# src/views/DecisionTimeline.vue
# src/composables/useDashboardData.ts
# src/composables/useStatusStream.ts
```

- [ ] **Step 3.2: 执行清理（不改功能）**

边界（按子 plan §4.6）：
- ✅ 重命名、提取、删除未用代码
- ❌ 不重写组件逻辑
- ❌ 不修改 props / events / 状态结构
- ❌ 不修改 UI 视觉

- [ ] **Step 3.3: 跑前端质量门禁**

```bash
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
npm run cypress:run
```

Expected: type-check 0 errors / build exit 0 / Cypress 80+ spec 全绿。

- [ ] **Step 3.4: Commit**

```bash
git add -A
git commit -m "refactor(frontend): ai-slop cleanup (Task F)

- Extract repeated logic into composables
- Replace magic strings with constants
- Add missing data-testid attributes
- Remove unused refs/computed"
```

---

## Wave 2 波间门禁（必须在此通过后进入 Wave 3）

- [ ] **Wave 2 Gate: 全量回归**

```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m basedpyright
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
npm run cypress:run
```

Expected: pytest ≥750 passed / basedpyright 0/0/0 / vue-tsc clean / build pass / Cypress 全绿。

---

## Wave 3: 体验 + 合流

> Wave 3 内部 Task 4~5 可并行（P23 改 frontend/，E 只改 git 分支）。

### Task 4: P23 — 前端实时通知中心 · Toast 浮层

**详细步骤见子 plan:** `docs/superpowers/plans/2026-06-04-P23-toast-notification-center.md`

**入口指令摘要（子 plan 的 6 个任务）：**

- [ ] **Step 4.1: 创建 `useNotificationStream` composable 骨架**

File: `frontend/src/composables/useNotificationStream.ts`

```typescript
import { ref, onUnmounted } from 'vue'
import { ElNotification, ElMessage } from 'element-plus'
import { useStatusStream } from './useStatusStream'
import type { TradeEvent, AuditLog } from '@/types'

export type NotificationSeverity = 'INFO' | 'WARNING' | 'CRITICAL'

interface UserPreferences {
    soundEnabled: boolean
    criticalPersistMaxPerMinute: number
}

const STORAGE_KEY = 'notification.preferences.v1'
const THROTTLE_MS = 1000
const PERSISTENT_LIMIT_WINDOW_MS = 60_000

function loadPreferences(): UserPreferences { /* ... */ }
function defaultPreferences(): UserPreferences { /* ... */ }
function savePreferences(prefs: UserPreferences) { /* ... */ }

export function useNotificationStream() {
    const { messages } = useStatusStream()
    const prefs = ref<UserPreferences>(loadPreferences())
    const lastEmittedAt = new Map<string, number>()
    const criticalCount = { count: 0, windowStart: Date.now() }

    function handleEvent(msg: TradeEvent | AuditLog) { /* ... */ }
    function enable() { /* ... */ }
    function disable() { /* ... */ }
    function updatePreferences(patch: Partial<UserPreferences>) { /* ... */ }

    onUnmounted(() => disable())
    return { enable, disable, prefs, updatePreferences }
}
```

- [ ] **Step 4.2: 实现事件分级 + 节流 + 用户偏好**

按子 plan §Step 2.1 实现 `handleEvent`、`emitBySeverity`、`enable`、`disable`。

Severity 映射：
| severity | 组件 | 位置 | 持续 | 声音 |
|----------|------|------|------|------|
| CRITICAL | `ElNotification` | top-right | 0（不自动关） | ✓ |
| WARNING  | `ElNotification` | bottom-right | 4000ms | ✗ |
| INFO     | `ElMessage` | top | 2000ms | ✗ |

- [ ] **Step 4.3: Dashboard 集成 + 设置 UI**

Files:
- Modify: `frontend/src/views/Dashboard.vue` → 顶层 `useNotificationStream().enable()`
- Create: `frontend/src/components/NotificationSettings.vue`
- Modify: `frontend/src/App.vue` → 设置入口

- [ ] **Step 4.4: 断线补齐（WS 重连后拉最近事件）**

Modify: `frontend/src/composables/useNotificationStream.ts`

```typescript
async function fetchRecentEventsOnReconnect() {
    try {
        const res = await fetch('/api/events?source=all&limit=20')
        if (!res.ok) return
        const data = await res.json()
        for (const evt of data.items ?? data) {
            emitBySeverity((evt as any).severity, evt)
        }
    } catch (err) {
        console.warn('fetchRecentEventsOnReconnect_failed', err)
    }
}
```

- [ ] **Step 4.5: Cypress E2E**

Create: `frontend/cypress/e2e/notification_center.cy.ts`

覆盖：
1. CRITICAL 事件触发 ElNotification top-right 持久化
2. WARNING 事件触发 ElNotification bottom-right 4s
3. INFO 事件触发 ElMessage top
4. 同事件 1s 内不重复
5. CRITICAL 持久化上限 5 条/分钟
6. WS 断线重连后补齐最近事件

- [ ] **Step 4.6: 跑全量质量门禁**

```bash
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m basedpyright
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
npm run cypress:run
```

Expected: pytest ≥750 passed / basedpyright 0/0/0 / vue-tsc clean / build pass / Cypress 全绿（含新增 notification_center.cy.ts）。

- [ ] **Step 4.7: Commit**

```bash
git add frontend/src/ frontend/cypress/
git commit -m "feat(frontend): real-time notification center with Toast (P23)

- Add useNotificationStream composable (severity分级/节流/用户偏好/断线补齐)
- Integrate into Dashboard.vue with NotificationSettings.vue
- Cypress E2E: 6 scenarios covering CRITICAL/WARNING/INFO/节流/上限/断线补齐"
```

---

### Task 5: E — 删除过时分支

**详细步骤见子 plan:** `docs/superpowers/plans/2026-06-04-E-merge-4-branches.md`

**关键调整（相比原 plan）：**

原 plan 假设需要 rebase + squash + 合并 4 个分支。经验证，这些分支的功能已存在于 main 中：
- `feature/addon-buy-margin-sizing` → P14 margin safety factor（已在 main）
- `feature/dashboard-config-performance` → P15 Dashboard 性能（已在 main）
- `p5-plus-audit-notifications` → P5+ 审计通知（已在 main）
- `refactor/maintainability-frontend` → 前端重构（已在 main）

因此 Task 5 简化为：**安全删除过时本地分支**。

- [ ] **Step 5.1: 再次确认分支内容已存在于 main**

对每个分支，抽查其 diff 中的核心符号是否已在 main：

```bash
cd /home/lcy/code/auto_trade
# 示例：验证 margin_safety_factor
git grep -q "margin_safety_factor" backend/app/models.py && echo "OK"
# 验证 audit_logs
git grep -q "audit_logs" backend/app/models.py && echo "OK"
# 验证 useDashboardData
git grep -q "useDashboardData" frontend/src/composables/useDashboardData.ts && echo "OK"
```

- [ ] **Step 5.2: 安全删除本地分支**

```bash
cd /home/lcy/code/auto_trade
# 先确保不在要删除的分支上
git checkout main
# 删除本地分支（远程分支保留作为备份）
git branch -D feature/addon-buy-margin-sizing
 git branch -D feature/dashboard-config-performance
 git branch -D p5-plus-audit-notifications
 git branch -D refactor/maintainability-frontend
```

- [ ] **Step 5.3: 验证 main 仍为默认分支且无残留**

```bash
git branch -a
```

Expected: 仅剩 `main` 本地分支 + `remotes/*`。

- [ ] **Step 5.4: Commit（如无需代码变更，则只记录日志）**

本任务无代码变更，无需 commit。在 `.sisyphus/evidence/` 记录分支清理日志：

```bash
mkdir -p .sisyphus/evidence
cat > .sisyphus/evidence/task-E-branch-cleanup.txt <<'EOF'
Task E: 删除过时本地分支
完成时间: 2026-06-04
删除分支:
- feature/addon-buy-margin-sizing (功能已在 main 作为 P14 交付)
- feature/dashboard-config-performance (功能已在 main 作为 P15 交付)
- p5-plus-audit-notifications (功能已在 main 作为 P5+ 交付)
- refactor/maintainability-frontend (功能已在 main 作为可维护性重构交付)
验证: git grep 确认核心符号存在于 main
EOF
```

---

## Wave 3 波间门禁（最终验收）

- [ ] **Wave 3 Gate: 全量回归 + 手工验证**

```bash
# 后端
cd /home/lcy/code/auto_trade/backend
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m basedpyright

# 前端
cd /home/lcy/code/auto_trade/frontend
npm run type-check
npm run build
npm run cypress:run

# Docker 全栈
cd /home/lcy/code/auto_trade
docker compose up --build -d
curl -fsS http://localhost:8080/api/health
```

Expected:
- pytest ≥750 passed
- basedpyright 0 errors, 0 warnings, 0 notes
- vue-tsc clean
- build pass
- Cypress 全绿（含新增 P23 spec）
- `/api/health` 200

- [ ] **Wave 3 Gate: 浏览器手工验证 P23**

1. 打开 Dashboard
2. 触发一个风控事件（如暂停策略）
3. 断言：看到 Toast 通知弹出
4. 检查通知偏好设置页可正常开关

---

## 执行选项

**Plan complete and saved to `docs/superpowers/plans/2026-06-04-execute-waves-2-3.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task (C, D, F, P23, E), review between tasks, fast iteration. REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints for review.

**Which approach?**
