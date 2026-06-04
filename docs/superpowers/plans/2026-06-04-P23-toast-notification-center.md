# P23. 前端实时通知中心 · Toast 浮层 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard 通过 Element Plus `ElNotification` / `ElMessage` 实时显示风控/跳过/审计事件，解决"必须刷新才能看到"痛点。

**Architecture:** 新增 `useNotificationStream` composable 复用现有 `useStatusStream` 的 WS 连接 → 解析 `trade_event` / `audit_log` → 按 `severity` 分级触发不同组件。节流 + 用户偏好 + 断线补齐。

**Tech Stack:** Vue 3 / TypeScript / Element Plus 2.8 (`ElNotification` / `ElMessage`) / Cypress 15

> **修订：** 前端无 vitest/jest（package.json 仅有 cypress）。单测逻辑全部合并到 Cypress e2e（任务 4）。

**前置阅读：**
- 母 spec §4.7 + §5.3
- 现有 `frontend/src/composables/useStatusStream.ts`
- 现有 `frontend/src/views/Dashboard.vue`
- `frontend/src/utils/labels.ts`（severity 标签映射）

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Create | `frontend/src/composables/useNotificationStream.ts` | 核心 composable：WS 订阅 + 事件分级 + 节流 + 用户偏好 |
| Modify | `frontend/src/views/Dashboard.vue` | 顶层 `useNotificationStream().enable()` |
| Modify | `frontend/src/components/NotificationSettings.vue` | 新增：用户偏好 UI（声音、CRITICAL 持久化开关） |
| Modify | `frontend/src/App.vue` | 设置入口 |
| Create | `frontend/cypress/e2e/notification_center.cy.ts` | E2E：覆盖所有 4 severity 渲染、节流、CRITICAL 持久化上限、用户偏好、断线补齐（**合并原 vitest 单测**） |
| Modify | `frontend/src/utils/constants.ts`（如未在 F 任务创建） | 事件类型 / severity 常量 |

---

## 任务 1: useNotificationStream composable 核心

**Files:**
- Create: `frontend/src/composables/useNotificationStream.ts`

### Step 1.1: 写 composable 骨架（先无功能，仅导出接口）

```typescript
// frontend/src/composables/useNotificationStream.ts
import { ref, onUnmounted } from 'vue'
import { ElNotification, ElMessage } from 'element-plus'
import { useStatusStream } from './useStatusStream'  // 复用现有 WS
import type { TradeEvent, AuditLog } from '@/types'

export type NotificationSeverity = 'INFO' | 'WARNING' | 'CRITICAL'

interface UserPreferences {
    soundEnabled: boolean
    criticalPersistMaxPerMinute: number
}

const STORAGE_KEY = 'notification.preferences.v1'
const THROTTLE_MS = 1000
const PERSISTENT_LIMIT_WINDOW_MS = 60_000

function loadPreferences(): UserPreferences {
    try {
        const raw = localStorage.getItem(STORAGE_KEY)
        if (!raw) return defaultPreferences()
        return { ...defaultPreferences(), ...JSON.parse(raw) }
    } catch {
        return defaultPreferences()
    }
}

function defaultPreferences(): UserPreferences {
    return {
        soundEnabled: true,
        criticalPersistMaxPerMinute: 5,
    }
}

function savePreferences(prefs: UserPreferences) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs))
}

export function useNotificationStream() {
    const { messages } = useStatusStream()
    const prefs = ref<UserPreferences>(loadPreferences())
    const lastEmittedAt = new Map<string, number>()  // key = `${type}:${detailHash}`
    const criticalCount = { count: 0, windowStart: Date.now() }

    function handleEvent(msg: TradeEvent | AuditLog) {
        // ... 后续步骤实现
    }

    function enable() {
        // 后续步骤实现
    }

    function disable() {
        // 后续步骤实现
    }

    function updatePreferences(patch: Partial<UserPreferences>) {
        prefs.value = { ...prefs.value, ...patch }
        savePreferences(prefs.value)
    }

    onUnmounted(() => disable())

    return { enable, disable, prefs, updatePreferences }
}
```

- [ ] **Step 1.1**: 写骨架

### Step 1.2: 跑 type-check 确认通过

```bash
cd frontend && npm run type-check
```

预期：exit 0（仅空函数体）。

- [ ] **Step 1.2**: 验证

---

## 任务 2: 实现事件分级 + 节流 + 用户偏好

**Files:**
- Modify: `frontend/src/composables/useNotificationStream.ts`

### Step 2.1: 实现 handleEvent + enable + disable

> **修订：** 单测在任务 4 合并到 Cypress e2e（前端无 vitest）。

```typescript
// frontend/src/composables/useNotificationStream.ts（替换 handleEvent）

function detailHash(detail: unknown): string {
    return JSON.stringify(detail ?? {})
}

function emitBySeverity(severity: NotificationSeverity, evt: any) {
    const title = evt.action ?? evt.event_type ?? 'Notification'
    const message = JSON.stringify(evt.detail ?? {})

    if (severity === 'CRITICAL') {
        ElNotification({
            title: `🚨 ${title}`,
            message,
            type: 'error',
            position: 'top-right',
            duration: 0,  // 不自动关
            customClass: 'notification-critical',
        })
        if (prefs.value.soundEnabled) playSound('critical')
    } else if (severity === 'WARNING') {
        ElNotification({
            title: `⚠️ ${title}`,
            message,
            type: 'warning',
            position: 'bottom-right',
            duration: 4000,
        })
    } else {
        ElMessage({
            message: `${title}: ${message}`,
            type: 'info',
            duration: 2000,
        })
    }
}

function playSound(_kind: string) {
    // 实际：用 Web Audio API 或 `<audio>` 元素
    // 简化：仅占位
}

function handleEvent(msg: TradeEvent | AuditLog) {
    const severity = (msg as any).severity as NotificationSeverity
    if (!severity) return

    // CRITICAL 持久化上限
    if (severity === 'CRITICAL') {
        const now = Date.now()
        if (now - criticalCount.windowStart > PERSISTENT_LIMIT_WINDOW_MS) {
            criticalCount.count = 0
            criticalCount.windowStart = now
        }
        if (criticalCount.count >= prefs.value.criticalPersistMaxPerMinute) {
            return  // 跳过
        }
        criticalCount.count += 1
    }

    // 节流
    const key = `${(msg as any).type}:${(msg as any).action ?? (msg as any).event_type}:${detailHash((msg as any).detail)}`
    const now = Date.now()
    const last = lastEmittedAt.get(key) ?? 0
    if (now - last < THROTTLE_MS) {
        return  // 节流：1s 内同事件不重复
    }
    lastEmittedAt.set(key, now)

    emitBySeverity(severity, msg)
}

let unsubscribe: (() => void) | null = null

function enable() {
    if (unsubscribe) return
    unsubscribe = messages.subscribe((msg) => {
        // msg 类型来自 useStatusStream；需映射
        if ((msg as any).type === 'trade_event' || (msg as any).type === 'audit_log') {
            handleEvent(msg as any)
        }
    })
}

function disable() {
    if (unsubscribe) {
        unsubscribe()
        unsubscribe = null
    }
}
```

> **注意：** `useStatusStream.messages` 的实际类型与订阅方式需参考 `frontend/src/composables/useStatusStream.ts` 现有实现并适配。

- [ ] **Step 2.1**: 实现

### Step 2.2: 跑 type-check + build

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

预期：exit 0。

- [ ] **Step 2.2**: 验证

---

## 任务 3: Dashboard 集成 + 设置 UI

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`
- Create: `frontend/src/components/NotificationSettings.vue`

### Step 3.1: 修改 Dashboard.vue 启用通知

```vue
<!-- frontend/src/views/Dashboard.vue（顶部 script 添加） -->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useNotificationStream } from '@/composables/useNotificationStream'

const notifications = useNotificationStream()
onMounted(() => {
    notifications.enable()
})
</script>
```

- [ ] **Step 3.1**: 启用通知

### Step 3.2: 创建 NotificationSettings.vue

```vue
<!-- frontend/src/components/NotificationSettings.vue -->
<script setup lang="ts">
import { ref } from 'vue'
import { useNotificationStream } from '@/composables/useNotificationStream'

const { prefs, updatePreferences } = useNotificationStream()
const localSound = ref(prefs.value.soundEnabled)
const localMax = ref(prefs.value.criticalPersistMaxPerMinute)

function onSave() {
    updatePreferences({
        soundEnabled: localSound.value,
        criticalPersistMaxPerMinute: localMax.value,
    })
    ElMessage.success('通知偏好已保存')
}
</script>

<template>
    <el-card data-testid="notification-settings">
        <template #header>通知偏好</template>
        <el-form>
            <el-form-item label="声音提醒">
                <el-switch v-model="localSound" />
            </el-form-item>
            <el-form-item label="CRITICAL 持久化上限（条/分钟）">
                <el-input-number v-model="localMax" :min="1" :max="20" />
            </el-form-item>
            <el-button @click="onSave" data-testid="notification-save-btn">保存</el-button>
        </el-form>
    </el-card>
</template>
```

- [ ] **Step 3.2**: 创建设置组件

### Step 3.3: 接入 App.vue 设置菜单

```vue
<!-- frontend/src/App.vue（设置菜单项添加） -->
<el-menu-item index="settings-notifications" data-testid="nav-notifications">
    通知偏好
</el-menu-item>
```

> 实际接入方式取决于 App.vue 现有结构（菜单/弹窗/独立路由等）。

- [ ] **Step 3.3**: 接入导航

### Step 3.4: 跑 type-check + build + Cypress

```bash
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：现有 spec 全绿。

- [ ] **Step 4.4**: 验证

---

## 任务 4: Cypress E2E

**Files:**
- Create: `frontend/cypress/e2e/notification_center.cy.ts`

### Step 4.1: 写 e2e

```typescript
// frontend/cypress/e2e/notification_center.cy.ts
describe('Notification Center (P23 Toast)', () => {
    beforeEach(() => {
        cy.stubApi()
        cy.visitApp()
        cy.visit('/#/dashboard')
    })

    it('CRITICAL 事件触发 ElNotification top-right 持久化', () => {
        cy.window().then((win) => {
            win.dispatchEvent(new MessageEvent('message', {
                data: JSON.stringify({
                    type: 'audit_log',
                    severity: 'CRITICAL',
                    action: 'KILL_SWITCH',
                    detail: { reason: 'test' },
                }),
            }))
        })
        cy.get('.el-notification').should('be.visible')
        cy.get('.el-notification').should('contain', 'KILL_SWITCH')
    })

    it('WARNING 事件触发 ElNotification bottom-right 4s', () => {
        cy.window().then((win) => {
            win.dispatchEvent(new MessageEvent('message', {
                data: JSON.stringify({
                    type: 'trade_event',
                    severity: 'WARNING',
                    event_type: 'ORDER_REJECTED',
                    detail: { reason: 'fee' },
                }),
            }))
        })
        cy.get('.el-notification').should('be.visible')
    })

    it('INFO 事件触发 ElMessage top', () => {
        cy.window().then((win) => {
            win.dispatchEvent(new MessageEvent('message', {
                data: JSON.stringify({
                    type: 'trade_event',
                    severity: 'INFO',
                    event_type: 'STRATEGY_UPDATED',
                    detail: {},
                }),
            }))
        })
        cy.get('.el-message').should('be.visible')
    })

    it('同事件 1s 内不重复', () => {
        cy.window().then((win) => {
            for (let i = 0; i < 5; i++) {
                win.dispatchEvent(new MessageEvent('message', {
                    data: JSON.stringify({
                        type: 'trade_event',
                        severity: 'WARNING',
                        event_type: 'ORDER_REJECTED',
                        detail: { reason: 'fee' },
                    }),
                }))
            }
        })
        cy.get('.el-notification').should('have.length', 1)
    })

    it('CRITICAL 持久化上限 5 条/分钟', () => {
        cy.window().then((win) => {
            for (let i = 0; i < 10; i++) {
                win.dispatchEvent(new MessageEvent('message', {
                    data: JSON.stringify({
                        type: 'audit_log',
                        severity: 'CRITICAL',
                        action: `KILL_${i}`,
                        detail: { i },
                    }),
                }))
            }
        })
        cy.get('.el-notification').should('have.length.at.most', 5)
    })
})
```

> 实际 WebSocket 注入方式：项目 `useStatusStream` 内部如何订阅 WS？需要适配（可能是 `new WebSocket()` 或 `EventTarget`）。具体参考 `frontend/src/composables/useStatusStream.ts` 现有实现。

- [ ] **Step 4.1**: 写 e2e

### Step 4.2: 跑 e2e

```bash
cd frontend && npm run cypress:run -- --spec "cypress/e2e/notification_center.cy.ts"
```

预期：5 个测试全 PASS。

- [ ] **Step 4.2**: 跑 e2e

### Step 4.3: 跑全栈

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 4.3**: 全栈验证

---

## 任务 5: 断线补齐（WS 重连后拉最近事件）

**Files:**
- Modify: `frontend/src/composables/useNotificationStream.ts`

### Step 5.1: 实现 fetchRecentEventsOnReconnect

```typescript
async function fetchRecentEventsOnReconnect() {
    try {
        const res = await fetch('/api/events?source=all&limit=20')
        if (!res.ok) return
        const data = await res.json()
        for (const evt of data.items ?? data) {
            // 不节流：补齐时全部显示
            emitBySeverity((evt as any).severity, evt)
        }
    } catch (err) {
        console.warn('fetchRecentEventsOnReconnect_failed', err)
    }
}

// 在 useStatusStream 的 onReconnect 回调中调用
```

> 实际：需要在 `useStatusStream` 暴露 `onReconnect` 钩子，或本 composable 自行监听 WS 状态。

- [ ] **Step 5.1**: 实现补齐

### Step 5.2: 写 e2e（断线补齐）

```typescript
it('WS 断线重连后补齐最近事件', () => {
    // Mock /api/events 返回 3 条事件
    cy.intercept('GET', '/api/events*', {
        statusCode: 200,
        body: { items: [
            { type: 'trade_event', severity: 'WARNING', event_type: 'A', detail: {} },
            { type: 'trade_event', severity: 'INFO', event_type: 'B', detail: {} },
            { type: 'audit_log', severity: 'CRITICAL', action: 'C', detail: {} },
        ]},
    }).as('getEvents')

    // 触发 WS 重连事件
    cy.window().then((win) => {
        win.dispatchEvent(new CustomEvent('ws-reconnect'))
    })

    cy.wait('@getEvents')
    cy.get('.el-notification, .el-message').should('have.length.at.least', 3)
})
```

- [ ] **Step 5.2**: 写 e2e

### Step 5.3: 跑 e2e + 全栈

```bash
cd frontend && npm run cypress:run
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：全绿。

- [ ] **Step 5.3**: 验证

---

## 任务 6: 写证据 + Commit

### Step 6.1: 写证据

```bash
cat > .sisyphus/evidence/task-P23-toast.txt << 'EOF'
Task P23: 前端实时通知中心 · Toast 浮层
完成时间: <YYYY-MM-DD>
新文件:
- frontend/src/composables/useNotificationStream.ts
- frontend/src/components/NotificationSettings.vue
- frontend/cypress/e2e/notification_center.cy.ts
- frontend/src/composables/__tests__/useNotificationStream.test.ts
修改:
- frontend/src/views/Dashboard.vue
- frontend/src/App.vue
测试: 6 个 cypress e2e 全绿（合并原 vitest 单测）
验证: 4 severity 分级、节流 1s、CRITICAL 5条/分钟、断线补齐、用户偏好
EOF
```

- [ ] **Step 6.1**: 写证据

### Step 6.2: Commit

```bash
git add frontend/src/ frontend/cypress/ .sisyphus/evidence/task-P23-toast.txt
git commit -m "feat(frontend): real-time notification center with toast (P23)"
```

> ⚠️ **不主动 commit**。agent 输出 "Task P23 complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 6 个 cypress e2e 全绿（合并原 vitest 单测场景）
- [ ] 现有 80+ Cypress spec 无回归
- [ ] `vue-tsc` 0 errors
- [ ] `npm run build` exit 0
- [ ] 浏览器手测：4 severity 渲染 + 节流 + 用户偏好
- [ ] 证据文件已写
- [ ] **不主动 commit**

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| WS 消息格式与预期不符 | 适配 useStatusStream 现有消息结构；不破坏 StatusStream 本身 |
| 通知风暴淹没主 UI | 节流 + CRITICAL 持久化上限 + 关闭按钮 |
| 断线补齐时通知重复 | 补齐时不节流但去重（按 event_id） |
| 声音打扰用户 | 偏好默认 soundEnabled=true；用户可关 |
| Vue 3 / Element Plus 类型不匹配 | 严格 ts-prune 验证；不引入新依赖 |

## 范围外（YAGNI）

- ❌ 修改 `useStatusStream`（已就位，不破坏）
- ❌ 修改 `DecisionTimeline.vue`（独立视图）
- ❌ 修改 `TradeEventService` 写路径
- ❌ 修改 WebSocket 服务端
- ❌ 创建新的全局通知组件
- ❌ 引入新依赖（用现有 Element Plus）

---

**Plan P23 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.7 + §5.3。**
