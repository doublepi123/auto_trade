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

    <!-- 主内容区 -->
    <el-main :class="{ 'mobile-main': isMobile }">
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
import { defineAsyncComponent, ref, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Odometer, Setting, List, Clock, Key, TrendCharts } from '@element-plus/icons-vue'
import { useNotificationStream } from './composables/useNotificationStream'
import { useNotificationBadge } from './composables/useNotificationBadge'

const NotificationSettings = defineAsyncComponent(() => import('./components/NotificationSettings.vue'))

const route = useRoute()
const router = useRouter()
const dialogVisible = ref(false)
const notifications = useNotificationStream()
const { unreadCount } = useNotificationBadge()
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
</style>
