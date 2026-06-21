<template>
  <el-container class="app-container">
    <!-- 桌面端顶部导航 -->
    <el-header v-if="!isMobile" class="app-header" data-testid="desktop-nav">
      <h2>Auto Trade</h2>
      <nav class="app-menu" aria-label="主导航">
        <router-link to="/" class="app-menu-link" :class="{ active: route.path === '/' }">仪表盘</router-link>
        <router-link to="/watchlist" class="app-menu-link" :class="{ active: route.path === '/watchlist' }">观察列表</router-link>
        <router-link to="/review" class="app-menu-link" :class="{ active: route.path === '/review' }">复盘</router-link>
        <router-link to="/reports" class="app-menu-link" :class="{ active: route.path === '/reports' }">交易报告</router-link>
        <router-link to="/backtest" class="app-menu-link" :class="{ active: route.path === '/backtest' }">回测</router-link>
        <router-link to="/experiments" class="app-menu-link" :class="{ active: route.path === '/experiments' }">策略实验</router-link>
        <router-link to="/strategy" class="app-menu-link" :class="{ active: route.path === '/strategy' }">策略配置</router-link>
        <router-link to="/credentials" class="app-menu-link" :class="{ active: route.path === '/credentials' }">凭证设置</router-link>
        <router-link to="/history" class="app-menu-link" :class="{ active: route.path === '/history' }">交易历史</router-link>
        <router-link to="/events" class="app-menu-link" :class="{ active: route.path === '/events' }">决策时间线</router-link>
        <router-link to="/alerts" class="app-menu-link" :class="{ active: route.path === '/alerts' }">告警规则</router-link>
        <el-badge :value="unreadCount" :max="99" :hidden="unreadCount === 0" data-testid="nav-notif-badge">
          <router-link to="/notifications" class="app-menu-link" :class="{ active: route.path === '/notifications' }">通知中心</router-link>
        </el-badge>
        <router-link to="/lab" class="app-menu-link" :class="{ active: route.path === '/lab' }">优化工作台</router-link>
      </nav>
      <div class="header-actions">
        <el-button
          size="small"
          text
          data-testid="nav-command-palette"
          title="命令面板 (Cmd/Ctrl+K)"
          @click="palette.openPalette()"
          >⌘K</el-button
        >
        <el-popover placement="bottom" :width="260" trigger="click" data-testid="nav-health-popover">
          <template #reference>
            <el-button size="small" text data-testid="nav-health">
              <span class="health-dot" :class="healthDotClass" />
              {{ healthLabel }}<span
                v-if="healthAgeSuffix"
                class="health-age"
                :class="healthAgeClass"
                data-testid="nav-health-age"
                >{{ healthAgeSuffix }}</span
              >
            </el-button>
          </template>
          <div class="health-panel" data-testid="health-panel">
            <MetricStat spread label="实时连接" :value="health.connectionLabel.value" />
            <MetricStat
              spread
              label="数据年龄"
              :value="healthAgeLabel"
              :value-class="healthAgeClass"
              value-testid="health-age"
            />
            <MetricStat
              spread
              label="运行器"
              :value="health.status.value.runner_running ? '运行中' : '未启动'"
            />
            <MetricStat
              spread
              label="引擎状态"
              :value="engineStateLabel(health.status.value.engine_state)"
            />
            <div class="health-actions">
              <el-button
                size="small"
                type="primary"
                plain
                data-testid="health-reconnect"
                @click="health.reconnectNow()"
                >重新连接</el-button
              >
              <el-button
                size="small"
                plain
                data-testid="health-copy-snapshot"
                @click="copyHealthSnapshot"
                >复制健康快照</el-button
              >
            </div>
          </div>
        </el-popover>
        <el-button size="small" text @click="dialogVisible = true" data-testid="nav-notification-settings"
          >通知偏好</el-button
        >
        <el-button size="small" text data-testid="nav-shortcuts" @click="shortcutsVisible = true">快捷键</el-button>
        <el-button size="small" text data-testid="nav-theme-toggle" @click="toggleDark">
          {{ isDark ? '☀️ 亮色' : '🌙 深色' }}
        </el-button>
      </div>
    </el-header>
    <el-dialog v-model="shortcutsVisible" title="键盘快捷键" width="420px" data-testid="shortcuts-dialog">
      <p class="shortcut-hint">在非输入框聚焦时按下字母键即可切换页面（按 <kbd>?</kbd> 也可打开此帮助）。</p>
      <ul class="shortcut-list">
        <li v-for="s in shortcutList" :key="s.key"><kbd>{{ s.key.toUpperCase() }}</kbd> — {{ s.label }}</li>
      </ul>
    </el-dialog>
    <el-dialog v-model="dialogVisible" title="通知偏好" width="400px">
      <NotificationSettings />
    </el-dialog>

    <CommandPalette />

    <!-- 主内容区 -->
    <el-main :class="{ 'mobile-main': isMobile }">
      <el-alert
        v-if="sessionBannerVisible"
        class="session-banner"
        data-testid="session-banner"
        type="warning"
        show-icon
        :closable="true"
        :title="sessionBannerTitle"
        @close="dismissSession()"
      >
        <span class="session-banner-desc">
          当前处于{{ sessionPhaseLabel }}时段，行情推送与策略动作可能受限（仅含常规 RTH 窗口，不含节假日历）。
        </span>
      </el-alert>
      <router-view />
    </el-main>

    <!-- 移动端底部导航 -->
    <nav v-if="isMobile" class="bottom-nav" data-testid="bottom-nav">
      <router-link to="/" class="nav-item" :class="{ active: route.path === '/' }">
        <el-icon><Odometer /></el-icon>
        <span>驾驶舱</span>
      </router-link>
      <router-link to="/review" class="nav-item" :class="{ active: route.path === '/review' }">
        <el-icon><TrendCharts /></el-icon>
        <span>复盘</span>
      </router-link>
      <router-link to="/strategy" class="nav-item" :class="{ active: route.path === '/strategy' }">
        <el-icon><Setting /></el-icon>
        <span>策略</span>
      </router-link>
      <router-link to="/history" class="nav-item" :class="{ active: route.path === '/history' }">
        <el-icon><List /></el-icon>
        <span>历史</span>
      </router-link>
      <router-link to="/events" class="nav-item" :class="{ active: route.path === '/events' }">
        <el-icon><Clock /></el-icon>
        <span>时间线</span>
      </router-link>
      <router-link to="/credentials" class="nav-item" :class="{ active: route.path === '/credentials' }">
        <el-icon><Key /></el-icon>
        <span>设置</span>
      </router-link>
    </nav>
  </el-container>
</template>

<script setup lang="ts">
import { defineAsyncComponent, computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Odometer, Setting, List, Clock, Key, TrendCharts } from '@element-plus/icons-vue'
import { useNotificationStream } from './composables/useNotificationStream'
import { useNotificationBadge } from './composables/useNotificationBadge'
import { useConnectionHealth } from './composables/useConnectionHealth'
import { useMarketSession } from './composables/useMarketSession'
import { useCommandPalette } from './composables/useCommandPalette'
import { useRecentPages } from './composables/useRecentPages'
import MetricStat from './components/MetricStat.vue'
import CommandPalette from './components/CommandPalette.vue'
import { engineStateLabel } from './utils/labels'
import { ageFreshnessClass, relativeAgeLabel } from './utils/time'
import { copyText } from './utils/clipboard'

const NotificationSettings = defineAsyncComponent(() => import('./components/NotificationSettings.vue'))

const route = useRoute()
const router = useRouter()
const dialogVisible = ref(false)
const notifications = useNotificationStream()
const { unreadCount } = useNotificationBadge()
// Boot the app-wide realtime connection here so the health badge is accurate
// on every page, not only when the Dashboard is mounted.
const health = useConnectionHealth()
const palette = useCommandPalette()
const { recordVisit } = useRecentPages()
// Track page visits for the command palette's "recent" ordering.
watch(
  () => route.path,
  (path) => recordVisit(path),
  { immediate: true },
)

// Short badge label + coloured dot derived from the shared connection state.
const healthLabel = computed(() => {
  switch (health.realtimeStatus.value) {
    case 'connected':
      return '实时'
    case 'reconnecting':
      return '重连中'
    case 'polling':
      return '轮询'
    default:
      return '连接中'
  }
})
const healthDotClass = computed(() => {
  switch (health.realtimeStatus.value) {
    case 'connected':
      return 'health-dot-ok'
    case 'reconnecting':
      return 'health-dot-warn'
    default:
      return 'health-dot-info'
  }
})

// Data-age signal: only meaningful once we've actually heard from the server,
// and suppressed while (re)connecting so the badge doesn't flash a stale age.
const hasFreshData = computed(() => health.lastDataAt.value > 0)
const showAge = computed(() => {
  if (!hasFreshData.value) return false
  const st = health.realtimeStatus.value
  return st === 'connected' || st === 'polling'
})
const healthAgeSuffix = computed(() =>
  showAge.value ? ` · ${relativeAgeLabel(health.ageSeconds.value)}` : '',
)
const healthAgeLabel = computed(() =>
  hasFreshData.value ? relativeAgeLabel(health.ageSeconds.value) : '—',
)
const healthAgeClass = computed(() => `health-age-${ageFreshnessClass(health.ageSeconds.value)}`)

// Reconnection feedback: warn the user when the realtime stream drops to
// 'reconnecting' and confirm when it recovers to 'connected'. Only fires on
// genuine state transitions (debounced by the watcher itself), and only
// reports recovery after an actual drop — not the initial connect. 'polling'
// is a steady degraded state, not a transient drop, so it is left silent.
let prevHealthState = health.realtimeStatus.value
let healthDropped = false
watch(health.realtimeStatus, (next) => {
  if (next === 'reconnecting' && prevHealthState !== 'reconnecting') {
    healthDropped = true
    ElMessage.warning({ message: '实时连接断开，正在重连…', duration: 3000 })
  } else if (next === 'connected' && prevHealthState !== 'connected' && healthDropped) {
    ElMessage.success({ message: '实时连接已恢复', duration: 2000 })
    healthDropped = false
  }
  prevHealthState = next
})

// Global market-session awareness banner: surfaces a dismissible warning when
// the primary symbol's market is outside regular trading hours, so the user
// knows quote pushes / strategy actions may be limited. Distinct from the
// Dashboard SessionClockPanel (which shows the next-open countdown).
const {
  showBanner: sessionBannerVisible,
  phaseLabel: sessionPhaseLabel,
  session: sessionData,
  dismiss: dismissSession,
} = useMarketSession()
const sessionBannerTitle = computed(() => `当前为${sessionPhaseLabel.value}，非常规交易时段`)

// One-click bug-report helper: copy a plain-text snapshot of the live
// connection / runner / engine / session state so it can be pasted into a
// support ticket. Reads the same shared signals the badge and popover show.
async function copyHealthSnapshot(): Promise<void> {
  const lines = [
    'Auto Trade 健康快照',
    `时间: ${new Date().toLocaleString()}`,
    `实时连接: ${health.connectionLabel.value}`,
    `数据年龄: ${healthAgeLabel.value}`,
    `运行器: ${health.status.value.runner_running ? '运行中' : '未启动'}`,
    `引擎状态: ${engineStateLabel(health.status.value.engine_state)}`,
  ]
  const sess = sessionData.value
  if (sess) {
    lines.push(`交易时段: ${sessionPhaseLabel.value || sess.status}`)
    lines.push(`标的: ${sess.symbol || '-'} (${sess.market})`)
  }
  const ok = await copyText(lines.join('\n'))
  if (ok) {
    ElMessage.success('健康快照已复制')
  } else {
    ElMessage.error('复制失败，请手动选择文本')
  }
}
const MOBILE_BREAKPOINT = 768
const isMobile = ref(window.innerWidth <= MOBILE_BREAKPOINT)

// Dark mode persisted to localStorage; Element Plus dark CSS vars are imported
// in main.ts and recolor when <html class="dark"> is present.
const DARK_KEY = 'auto_trade.theme.dark'
const isDark = ref(false)
function applyDark(value: boolean) {
  isDark.value = value
  document.documentElement.classList.toggle('dark', value)
  try { localStorage.setItem(DARK_KEY, value ? '1' : '0') } catch { /* ignore */ }
}
function toggleDark() { applyDark(!isDark.value) }

// Single-letter keyboard shortcuts → route. Only active when NOT typing in an
// input/textarea/select/contenteditable, and only with no Ctrl/Meta/Alt held.
const shortcutList = [
  { key: 'd', path: '/', label: '仪表盘' },
  { key: 'w', path: '/watchlist', label: '观察列表' },
  { key: 'r', path: '/review', label: '复盘' },
  { key: 'p', path: '/reports', label: '交易报告' },
  { key: 'b', path: '/backtest', label: '回测' },
  { key: 'x', path: '/experiments', label: '策略实验' },
  { key: 's', path: '/strategy', label: '策略配置' },
  { key: 'c', path: '/credentials', label: '凭证设置' },
  { key: 'h', path: '/history', label: '交易历史' },
  { key: 'e', path: '/events', label: '决策时间线' },
  { key: 'a', path: '/alerts', label: '告警规则' },
  { key: 'n', path: '/notifications', label: '通知中心' },
  { key: 'l', path: '/lab', label: '优化工作台' },
]
const shortcutMap = new Map(shortcutList.map((s) => [s.key, s.path]))
const shortcutsVisible = ref(false)

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  const tag = el.tagName.toLowerCase()
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true
  if (el.isContentEditable) return true
  return false
}

function handleKeydown(ev: KeyboardEvent) {
  // Command palette: Cmd/Ctrl+K opens it anywhere. Handled before the
  // modifier-guard below so the combo isn't silently dropped.
  if ((ev.metaKey || ev.ctrlKey) && !ev.shiftKey && !ev.altKey && ev.key.toLowerCase() === 'k') {
    ev.preventDefault()
    palette.openPalette()
    return
  }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return
  if (isTypingTarget(ev.target)) return
  const key = ev.key.toLowerCase()
  if (key === '?') {
    ev.preventDefault()
    shortcutsVisible.value = true
    return
  }
  const path = shortcutMap.get(key)
  if (path) {
    ev.preventDefault()
    router.push(path)
  }
}

function handleResize() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
  window.addEventListener('keydown', handleKeydown)
  try {
    applyDark(localStorage.getItem(DARK_KEY) === '1')
  } catch {
    applyDark(false)
  }
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.app-container {
  min-height: 100vh;
}

.app-header {
  /* Element Plus pins el-header to 60px; allow it to grow so wrapped buttons
     are not covered by el-main on narrower viewports. */
  height: auto !important;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 16px;
}

.app-menu {
  display: flex;
  align-items: center;
  gap: 14px;
  flex: 1 1 auto;
  min-width: 0;
}

/* Keep the nav menu and the action buttons from overlapping on narrow widths:
   the menu takes the available row, the buttons wrap onto a new line rather
   than covering the last menu link. */
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}

.app-header h2 {
  flex: 0 0 auto;
  margin: 0;
  white-space: nowrap;
}

.app-menu-link {
  color: #6b7280;
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.app-menu-link.active {
  color: #409eff;
}

/* 移动端主内容区增加底部 padding 给导航栏留空间 */
.mobile-main {
  padding-bottom: 72px;
}

/* 底部导航栏 */
.bottom-nav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: space-around;
  height: 60px;
  border-top: 1px solid #e1e7f0;
  background: #fff;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06);
}

.nav-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 20%;
  height: 100%;
  color: #6b7280;
  font-size: 11px;
  text-decoration: none;
  transition: color 0.2s;
}

.nav-item .el-icon {
  font-size: 20px;
}

.nav-item.active {
  color: #409eff;
}

.nav-item:active {
  opacity: 0.7;
}

.shortcut-hint {
  margin: 0 0 12px;
  color: #6b7280;
  font-size: 13px;
}

.shortcut-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 16px;
}

.shortcut-list li {
  color: #4b5563;
  font-size: 13px;
}

.shortcut-list kbd,
.shortcut-hint kbd {
  display: inline-block;
  min-width: 18px;
  padding: 1px 6px;
  margin-right: 6px;
  background: #f5f7fa;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}

.health-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 50%;
  background: #909399;
}
.health-dot-ok {
  background: #67c23a;
}
.health-dot-warn {
  background: #e6a23c;
}
.health-dot-info {
  background: #409eff;
}

/* Data-age freshness colour coding (ok < 10s, warn 10–30s, danger > 30s). */
.health-age {
  margin-left: 2px;
  font-size: 12px;
  color: #909399;
}
.health-age-ok {
  color: #909399;
}
.health-age-warn {
  color: #e6a23c;
}
.health-age-danger {
  color: #f56c6c;
}

.health-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}
.health-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.health-row span {
  color: #6b7280;
}
.health-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

.session-banner {
  margin-bottom: 12px;
}
.session-banner-desc {
  font-size: 12px;
  color: #6b7280;
}
</style>
